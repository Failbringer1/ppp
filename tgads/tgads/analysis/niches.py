"""Ad creative clustering and niche scoring (module 1)."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from .text import jaccard, shingles


@dataclass
class AdPost:
    key: object            # post id
    host: str              # channel that published the ad
    text: str
    targets: set[str]      # promoted channels
    date: datetime
    views: int = 0


@dataclass
class CreativeCluster:
    representative: AdPost
    posts: list[AdPost] = field(default_factory=list)
    _sh: set[str] = field(default_factory=set, repr=False)

    @property
    def hosts(self) -> set[str]:
        return {p.host for p in self.posts}

    @property
    def targets(self) -> set[str]:
        out: set[str] = set()
        for p in self.posts:
            out |= p.targets
        return out

    @property
    def days_span(self) -> float:
        ds = [p.date for p in self.posts]
        return (max(ds) - min(ds)).total_seconds() / 86400

    @property
    def total_views(self) -> int:
        return sum(p.views for p in self.posts)

    @property
    def score(self) -> float:
        """Creatives bought again and again, on many hosts and over weeks, pay off."""
        hosts = len(self.hosts)
        extra = max(0, len(self.posts) - hosts)
        return round(hosts * (1 + math.log1p(extra)) * (1 + min(self.days_span, 60) / 14), 2)


def cluster_creatives(ads: list[AdPost], threshold: float = 0.6) -> list[CreativeCluster]:
    """Greedy clustering by shingle Jaccard similarity.

    Posts promoting the same target are compared first, which keeps it close to
    linear on real data (most clusters share a target channel).
    """
    clusters: list[CreativeCluster] = []
    by_target: dict[str, list[CreativeCluster]] = {}
    for ad in sorted(ads, key=lambda a: a.date):
        sh = shingles(ad.text)
        if not sh:
            continue
        candidates: list[CreativeCluster] = []
        for t in ad.targets:
            candidates.extend(by_target.get(t, []))
        if not ad.targets:
            candidates = clusters
        best, best_sim = None, 0.0
        seen = set()
        for c in candidates:
            if id(c) in seen:
                continue
            seen.add(id(c))
            sim = jaccard(sh, c._sh)
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= threshold:
            best.posts.append(ad)
        else:
            best = CreativeCluster(representative=ad, posts=[ad], _sh=sh)
            clusters.append(best)
        for t in ad.targets:
            lst = by_target.setdefault(t, [])
            if best not in lst:
                lst.append(best)
    return sorted(clusters, key=lambda c: c.score, reverse=True)


@dataclass
class ChannelRow:
    topic: str
    subscribers: int
    avg_views: float
    err: float | None
    ad_share: float | None


@dataclass
class NicheRow:
    topic: str
    channels: int
    total_reach: float
    median_reach: float
    median_err: float | None
    top3_share: float
    ad_share: float | None
    incoming_ads: int
    score: float = 0.0


def niche_table(channels: list[ChannelRow], incoming_ads: dict[str, int]) -> list[NicheRow]:
    """Aggregate channels by topic and rank topics by opportunity.

    demand = reach of the niche + how much others pay to advertise into it
    competition = number of channels + how concentrated the reach is in the top-3
    """
    by_topic: dict[str, list[ChannelRow]] = {}
    for c in channels:
        by_topic.setdefault(c.topic or "прочее", []).append(c)
    rows: list[NicheRow] = []
    for topic, cs in by_topic.items():
        reaches = sorted((c.avg_views or 0 for c in cs), reverse=True)
        total = sum(reaches)
        errs = [c.err for c in cs if c.err]
        ads = [c.ad_share for c in cs if c.ad_share is not None]
        rows.append(NicheRow(
            topic=topic,
            channels=len(cs),
            total_reach=total,
            median_reach=statistics.median(reaches) if reaches else 0,
            median_err=statistics.median(errs) if errs else None,
            top3_share=(sum(reaches[:3]) / total) if total else 0,
            ad_share=statistics.mean(ads) if ads else None,
            incoming_ads=incoming_ads.get(topic, 0),
        ))
    raw = []
    for r in rows:
        demand = math.log1p(r.total_reach) + 1.5 * math.log1p(r.incoming_ads) + 4 * (r.median_err or 0)
        competition = math.log1p(r.channels) + 2 * r.top3_share
        raw.append(demand / (1 + competition))
    top = max(raw, default=0) or 1
    for r, v in zip(rows, raw):
        r.score = round(100 * v / top, 1)
    return sorted(rows, key=lambda r: r.score, reverse=True)


TOPICS = [
    "новости", "юмор", "футбол/спорт", "авто", "работа/вакансии", "образование/IELTS",
    "скидки/маркетплейсы", "кино/сериалы", "рецепты", "бизнес/деньги", "крипта/трейдинг",
    "религия", "здоровье/красота", "мамы/дети", "путешествия", "психология/мотивация",
    "техника/IT", "недвижимость", "музыка", "игры", "женское", "мужское", "прочее",
]
