"""Post-level analysis: ad detection and normalized "best post" scoring."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .text import extract_refs

# Explicit ad markers (RU / UZ latin / UZ cyrillic / EN).
AD_MARKERS = re.compile(
    r"(#реклама|#reklama|#ad\b|#ads\b|#промо|#promo|#homiy|\berid\b|реклама\b|reklama\b|"
    r"\bhomiy\b|ҳомий|рекламa|на правах рекламы|партн[её]рский пост|sponsored)",
    re.IGNORECASE,
)
# Calls to subscribe that are typical for channel ads.
CTA = re.compile(
    r"(подпис(ывай|ыв|ать|ись|ывайтесь)|переходи|жми|вступ(ай|ить)|obuna bo['ʻ’]?l|kanalga (o['ʻ’]?ting|qo['ʻ’]?shiling)|"
    r"a['ʻ’]?zo bo['ʻ’]?ling|обуна бўлинг|каналга|qo['ʻ’]?shiling|ulaning|kirish|subscribe|join)",
    re.IGNORECASE,
)


@dataclass
class AdCheck:
    is_ad: bool            # any advertising (excluded from content donors)
    promotes_channel: bool  # ad of another Telegram channel (used for creatives)
    score: float
    targets: set[str] = field(default_factory=set)  # usernames / +invite hashes promoted
    reasons: list[str] = field(default_factory=list)


def detect_ad(text: str, urls: list[str] | None = None, own_username: str | None = None) -> AdCheck:
    """Heuristic: does this post advertise another Telegram channel?"""
    text = text or ""
    refs = extract_refs(text, urls)
    own = (own_username or "").lower()
    targets = {u for u in refs["usernames"] if u != own} | {"+" + h for h in refs["invites"]}
    score = 0.0
    reasons = []
    marked = bool(AD_MARKERS.search(text))
    if marked:
        score += 0.6
        reasons.append("метка рекламы")
    if refs["invites"]:
        score += 0.35
        reasons.append("ссылка-приглашение t.me/+")
    if targets and CTA.search(text):
        score += 0.4
        reasons.append("призыв подписаться")
    if targets and len(text) < 700 and len(targets) <= 2:
        score += 0.1
    promotes = bool(targets) and score >= 0.5
    return AdCheck(is_ad=promotes or marked, promotes_channel=promotes, score=round(min(score, 1.0), 2),
                   targets=targets, reasons=reasons)


@dataclass
class ScoredPost:
    key: object
    views: int
    forwards: int
    reactions: int
    age_hours: float
    score: float = 0.0


def score_posts(posts: list[ScoredPost], min_age_h: float = 24) -> list[ScoredPost]:
    """Score posts of one channel relative to that channel's own medians.

    score = 0.4 * views/median_views + 0.35 * fwd_rate/median_fwd_rate
            + 0.25 * react_rate/median_react_rate
    Rates are per 1000 views. Young posts (< min_age_h) get no score.
    """
    mature = [p for p in posts if p.age_hours >= min_age_h and p.views > 0]
    if not mature:
        return []

    def med(vals: list[float]) -> float:
        vals = [v for v in vals if v > 0]
        return statistics.median(vals) if vals else 0.0

    mv = med([p.views for p in mature])
    mf = med([1000 * p.forwards / p.views for p in mature])
    mr = med([1000 * p.reactions / p.views for p in mature])
    for p in mature:
        fr = 1000 * p.forwards / p.views
        rr = 1000 * p.reactions / p.views
        p.score = round(
            0.4 * (p.views / mv if mv else 0)
            + 0.35 * (fr / mf if mf else (1.0 if fr else 0))
            + 0.25 * (rr / mr if mr else (1.0 if rr else 0)),
            3,
        )
    return sorted(mature, key=lambda p: p.score, reverse=True)
