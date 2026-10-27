"""View-dynamics model: reference curves, 24h forecast and bot-inflation detection.

Core idea (module 3 of the spec): organic posts gain views along a fairly stable
curve S(t) = V(t) / V(24h). Bots are rarely added in the first minutes, so a
forecast made from the first ~30 minutes is an "honest" estimate. When the real
24h result is far above that forecast, views were added later - inflated.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

DAY = 86_400
# Seconds after publication at which the monitor samples views.

# Median share of 24h views reached at each checkpoint for a typical channel.
# Only a starting point: build_curve() replaces it with medians from real data.
PRIOR_MEDIAN: dict[int, float] = {
    30: 0.010, 60: 0.020, 120: 0.035, 180: 0.048, 300: 0.070, 600: 0.110,
    900: 0.140, 1800: 0.200, 2700: 0.240, 3600: 0.280, 7200: 0.380,
    10800: 0.450, 14400: 0.510, 21600: 0.600, 28800: 0.675, 43200: 0.780,
    57600: 0.860, 86400: 1.000, 172800: 1.120,
}
# Seconds after publication at which the monitor samples views.
CHECKPOINTS: tuple[int, ...] = tuple(sorted(PRIOR_MEDIAN))

TASHKENT = timezone(timedelta(hours=5))
NIGHT_HOURS = range(0, 8)  # local hours with a slow start


Sample = tuple[int, int] | tuple[int, int, int]  # (t_sec, views[, forwards])


@dataclass
class Curve:
    """Quantiles (p25, p50, p75) of the share of 24h views at each checkpoint."""

    points: dict[int, tuple[float, float, float]]
    n_posts: int = 0

    @classmethod
    def prior(cls) -> Curve:
        pts = {}
        for t, m in PRIOR_MEDIAN.items():
            if t == DAY:
                pts[t] = (1.0, 1.0, 1.0)
            elif t > DAY:
                pts[t] = (m - 0.05, m, m + 0.08)
            else:
                pts[t] = (m * 0.7, m, min(0.98, m * 1.35))
        return cls(pts, 0)

    def quantiles(self, t: float) -> tuple[float, float, float]:
        ts = sorted(self.points)
        if t <= ts[0]:
            # linear from (0, 0) to the first checkpoint
            k = max(t, 0) / ts[0]
            return tuple(v * k for v in self.points[ts[0]])  # type: ignore[return-value]
        if t >= ts[-1]:
            return self.points[ts[-1]]
        for a, b in zip(ts, ts[1:]):
            if a <= t <= b:
                w = (math.log(t) - math.log(a)) / (math.log(b) - math.log(a))
                pa, pb = self.points[a], self.points[b]
                return tuple(x + w * (y - x) for x, y in zip(pa, pb))  # type: ignore[return-value]
        raise AssertionError("unreachable")

    def share(self, t: float) -> float:
        return self.quantiles(t)[1]

    def to_json(self) -> dict:
        return {"points": {str(k): list(v) for k, v in self.points.items()}, "n_posts": self.n_posts}

    @classmethod
    def from_json(cls, data: dict | None) -> Curve:
        if not data:
            return cls.prior()
        return cls({int(k): tuple(v) for k, v in data["points"].items()}, data.get("n_posts", 0))


def _norm(samples: list[Sample]) -> list[tuple[int, int, int]]:
    out = []
    for s in samples:
        t, v = int(s[0]), int(s[1])
        f = int(s[2]) if len(s) > 2 and s[2] is not None else 0
        out.append((t, v, f))
    out.sort()
    return out


def views_at(samples: list[Sample], t: float) -> float | None:
    """Views at time t, interpolated in log-time. None outside the sampled range."""
    pts = _norm(samples)
    if not pts or t < pts[0][0] or t > pts[-1][0]:
        return None
    for (ta, va, _), (tb, vb, _) in zip(pts, pts[1:]):
        if ta <= t <= tb:
            if tb == ta:
                return float(vb)
            w = (math.log(max(t, 1)) - math.log(max(ta, 1))) / (math.log(max(tb, 1)) - math.log(max(ta, 1)) or 1)
            return va + w * (vb - va)
    return float(pts[-1][1])


def build_curve(posts: list[list[Sample]], min_posts: int = 5, prior_weight: int = 10,
                min_views_24h: int = 20) -> Curve:
    """Learn a curve from tracked posts that have data up to 24h.

    Medians are robust to the minority of inflated posts. The result is blended
    with the prior so a handful of posts cannot distort it.
    """
    prior = Curve.prior()
    shares: dict[int, list[float]] = {t: [] for t in CHECKPOINTS}
    used = 0
    for samples in posts:
        v24 = views_at(samples, DAY)
        if not v24 or v24 < min_views_24h:
            continue
        used += 1
        for t in CHECKPOINTS:
            v = views_at(samples, t)
            if v is not None:
                shares[t].append(v / v24)
    points: dict[int, tuple[float, float, float]] = {}
    for t in CHECKPOINTS:
        pr = prior.points[t]
        data = shares[t]
        if t == DAY or len(data) < min_posts:
            points[t] = pr
            continue
        q = statistics.quantiles(data, n=4, method="inclusive")
        w = len(data) / (len(data) + prior_weight)
        points[t] = tuple(w * e + (1 - w) * p for e, p in zip(q, pr))  # type: ignore[assignment]
    # enforce monotonic growth in t and p25 <= p50 <= p75
    run = [0.0, 0.0, 0.0]
    for t in CHECKPOINTS:
        vals = sorted(points[t])
        run = [max(r, v) for r, v in zip(run, vals)]
        points[t] = (run[0], run[1], run[2])
    return Curve(points, used)


@dataclass
class Forecast:
    est: float
    low: float
    high: float
    t_used: int
    views_used: int


def forecast_24h(samples: list[Sample], curve: Curve, max_t: int = 1800, min_t: int = 25) -> Forecast | None:
    """Forecast 24h views from the latest sample taken within the first max_t seconds."""
    early = [s for s in _norm(samples) if min_t <= s[0] <= max_t]
    if not early:
        return None
    t, v, _ = early[-1]
    p25, p50, p75 = curve.quantiles(t)
    if p50 <= 0:
        return None
    return Forecast(est=v / p50, low=v / p75, high=v / max(p25, 1e-6), t_used=t, views_used=v)


@dataclass
class Thresholds:
    late_boost_ratio: float = 2.2       # actual / early-forecast
    dead_start_early_share: float = 0.003  # of subscribers within 5 minutes
    dead_start_final_share: float = 0.15   # of subscribers by 24h
    instant_dump_share: float = 0.45    # of 24h views within 2 minutes
    spike_factor: float = 3.0           # view rate jump vs previous interval
    spike_min_views: int = 50
    min_early_views: int = 3

    @classmethod
    def from_dict(cls, data: dict | None) -> Thresholds:
        th = cls()
        for k, v in (data or {}).items():
            if hasattr(th, k):
                setattr(th, k, type(getattr(th, k))(v))
        return th


FLAG_TEXT = {
    "late_boost": "Просмотры через сутки намного выше прогноза по первым минутам — докрутка",
    "dead_start": "В первые минуты почти нет просмотров, а к суткам — много",
    "instant_dump": "Больше половины суточных просмотров залетело за 2 минуты",
    "rate_spike": "Резкий скачок скорости просмотров без роста пересылок",
}


@dataclass
class PostAnalysis:
    forecast: Forecast | None
    actual_24h: float | None
    late_t: int | None = None
    late_views: float | None = None
    expected_late: float | None = None
    ratio: float | None = None
    flags: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.actual_24h is not None

    @property
    def bad(self) -> bool:
        return bool({"late_boost", "dead_start", "instant_dump"} & set(self.flags))

    def to_json(self) -> dict:
        d = asdict(self)
        d["complete"] = self.complete
        return d


def _local_hour(published_at: datetime | None, t: int) -> int | None:
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return (published_at + timedelta(seconds=t)).astimezone(TASHKENT).hour


def analyze_post(samples: list[Sample], subscribers: int, curve: Curve,
                 th: Thresholds | None = None, published_at: datetime | None = None) -> PostAnalysis:
    th = th or Thresholds()
    pts = _norm(samples)
    fc = forecast_24h(pts, curve)
    actual = views_at(pts, DAY)
    res = PostAnalysis(forecast=fc, actual_24h=actual)
    if not pts:
        return res

    # 1. late boost: compare the latest point (>= 3h) with what the early forecast expects
    late = [p for p in pts if p[0] >= 10_800]
    if late:
        lt = min(late[-1][0], DAY)
        lv = views_at(pts, lt)
        res.late_t, res.late_views = lt, lv
        if fc and fc.views_used >= th.min_early_views and lv is not None:
            res.expected_late = fc.est * curve.share(lt)
            optimistic = fc.high * curve.share(lt)
            if res.expected_late > 0:
                res.ratio = lv / res.expected_late
                if res.ratio >= th.late_boost_ratio and lv > optimistic * 1.2:
                    res.flags.append("late_boost")
                    res.details.append(
                        f"через {lt // 3600} ч: {int(lv)} просмотров при прогнозе {int(res.expected_late)} "
                        f"(x{res.ratio:.1f})")

    # 2. dead start
    if subscribers > 0:
        v5 = views_at(pts, 300)
        if res.late_views is not None and res.late_t:
            final = actual if actual is not None else res.late_views / max(curve.share(res.late_t), 1e-6)
        else:
            final = actual
        if v5 is not None and final is not None:
            if v5 < th.dead_start_early_share * subscribers and final > th.dead_start_final_share * subscribers:
                res.flags.append("dead_start")
                res.details.append(
                    f"за 5 мин {int(v5)} просмотров ({v5 / subscribers:.2%} подписчиков), "
                    f"за сутки ~{int(final)} ({final / subscribers:.0%})")

    # 3. instant dump at the start
    if actual:
        v2 = views_at(pts, 120)
        if v2 is not None and v2 / actual > th.instant_dump_share:
            res.flags.append("instant_dump")
            res.details.append(f"за 2 мин {int(v2)} из {int(actual)} суточных ({v2 / actual:.0%})")

    # 4. rate spike after the first hour without matching forwards
    prev_rate = None
    for (ta, va, fa), (tb, vb, fb) in zip(pts, pts[1:]):
        if tb <= ta:
            continue
        rate = (vb - va) / (tb - ta)
        if ta >= 3600 and prev_rate is not None and prev_rate > 0:
            dv = vb - va
            morning = _local_hour(published_at, tb) in range(6, 11) and _local_hour(published_at, ta) in NIGHT_HOURS
            if (rate > th.spike_factor * prev_rate and dv >= max(th.spike_min_views, 0.1 * va)
                    and (fb - fa) <= max(2, 0.002 * dv) and not morning):
                if "rate_spike" not in res.flags:
                    res.flags.append("rate_spike")
                res.details.append(f"скачок {ta // 60}–{tb // 60} мин: +{dv} просмотров, x{rate / prev_rate:.1f} к темпу")
        prev_rate = rate
    return res


@dataclass
class PostStat:
    views: int
    forwards: int = 0
    reactions: int = 0
    age_hours: float = 0.0
    is_ad: bool = False


@dataclass
class StaticMetrics:
    subscribers: int
    n_posts: int
    median_views: float | None
    err: float | None
    cv: float | None
    fwd_per_1k: float | None
    react_per_1k: float | None
    flags: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def static_metrics(posts: list[PostStat], subscribers: int, min_age_h: float = 48) -> StaticMetrics:
    """Checks that need no real-time tracking: ERR, uniformity, engagement."""
    mature = [p for p in posts if p.age_hours >= min_age_h and p.views > 0]
    m = StaticMetrics(subscribers, len(mature), None, None, None, None, None)
    if len(mature) < 5:
        return m
    views = [p.views for p in mature]
    total_v = sum(views)
    m.median_views = statistics.median(views)
    m.cv = statistics.pstdev(views) / statistics.mean(views)
    m.fwd_per_1k = 1000 * sum(p.forwards for p in mature) / total_v
    m.react_per_1k = 1000 * sum(p.reactions for p in mature) / total_v
    if subscribers > 0:
        m.err = m.median_views / subscribers
        if m.err > 0.8 and m.fwd_per_1k < 1:
            m.flags.append("high_err")
            m.details.append(f"ERR {m.err:.0%} без пересылок — похоже на накрутку просмотров")
        if m.err < 0.03 and subscribers > 2000:
            m.flags.append("low_err")
            m.details.append(f"ERR {m.err:.1%} — мёртвые или накрученные подписчики")
    if m.cv < 0.08 and len(mature) >= 10:
        m.flags.append("uniform_views")
        m.details.append(f"Подозрительно ровные просмотры (разброс {m.cv:.0%})")
    if m.median_views >= 3000 and m.fwd_per_1k < 0.2 and m.react_per_1k < 0.5:
        m.flags.append("no_engagement")
        m.details.append("Большой охват, но нет ни пересылок, ни реакций")
    return m


@dataclass
class ChannelVerdict:
    verdict: str  # unknown | clean | suspicious | boosted
    score: float
    honest_reach: float | None
    n_dynamic: int
    reasons: list[str]


STATIC_PENALTY = {"high_err": 15, "low_err": 20, "uniform_views": 25, "no_engagement": 10}


def channel_verdict(analyses: list[PostAnalysis], static: StaticMetrics | None, min_posts: int = 3) -> ChannelVerdict:
    reasons: list[str] = []
    score = 100.0
    dyn = [a for a in analyses if a.forecast is not None and (a.late_t is not None or a.complete)]
    n = len(dyn)
    bad = [a for a in dyn if a.bad]
    spikes = [a for a in dyn if "rate_spike" in a.flags]
    if n:
        bad_share = len(bad) / n
        score -= 70 * bad_share + 15 * (len(spikes) / n)
        for flag in ("late_boost", "dead_start", "instant_dump", "rate_spike"):
            hits = [a for a in dyn if flag in a.flags]
            if hits:
                example = next((d for d in hits[0].details if d), "")
                reasons.append(f"{FLAG_TEXT[flag]} — {len(hits)} из {n} постов" + (f" (пример: {example})" if example else ""))
    if static:
        for f, d in zip(static.flags, static.details):
            score -= STATIC_PENALTY.get(f, 10)
            reasons.append(d)
    score = max(0.0, min(100.0, score))

    if n >= min_posts:
        if len(bad) >= 2 and len(bad) / n >= 0.34:
            verdict = "boosted"
        elif bad or score < 70:
            verdict = "suspicious"
        else:
            verdict = "clean"
    else:
        verdict = "suspicious" if static and static.flags else "unknown"
        if n < min_posts:
            reasons.append(f"Отслежено постов: {n} из {min_posts} нужных для вердикта")

    honest = None
    clean_actuals = [a.actual_24h for a in dyn if a.complete and not a.bad and a.actual_24h]
    if verdict == "clean" and len(clean_actuals) >= min_posts:
        honest = statistics.median(clean_actuals)
    elif dyn:
        honest = statistics.median(a.forecast.est for a in dyn if a.forecast)
    return ChannelVerdict(verdict, round(score, 1), honest, n, reasons)


def next_checkpoint(t_elapsed: float) -> int | None:
    for c in CHECKPOINTS:
        if c > t_elapsed + 5:
            return c
    return None


def is_night(published_at: datetime) -> bool:
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(TASHKENT).hour in NIGHT_HOURS
