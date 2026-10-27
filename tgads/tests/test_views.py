from datetime import datetime, timezone

from tgads.analysis.views import (
    CHECKPOINTS, DAY, Curve, PostStat, analyze_post, build_curve, channel_verdict,
    forecast_24h, next_checkpoint, static_metrics, views_at,
)

PRIOR = Curve.prior()
NOON_UTC = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)  # 12:00 Tashkent


def organic(v24: int, speed: float = 1.0) -> list[tuple[int, int, int]]:
    """Samples of an organic post; speed > 1 means a faster-than-prior audience."""
    out = []
    for t in CHECKPOINTS:
        share = min(PRIOR.share(t) * speed, PRIOR.share(t) if t >= DAY else 0.99)
        out.append((t, int(v24 * share), int(v24 * share * 0.01)))
    return out


def boosted_late(v24_honest: int, extra: int, from_t: int = 10_800) -> list[tuple[int, int, int]]:
    out = []
    for t, v, f in organic(v24_honest):
        out.append((t, v + (extra if t >= from_t else 0), f))
    return out


def test_curve_is_monotonic_and_interpolates():
    q_prev = 0
    for t in range(30, DAY, 997):
        s = PRIOR.share(t)
        assert s >= q_prev - 1e-9
        q_prev = s
    assert PRIOR.share(DAY) == 1.0
    p25, p50, p75 = PRIOR.quantiles(600)
    assert p25 < p50 < p75


def test_views_at_interpolation_bounds():
    s = [(60, 100), (600, 400)]
    assert views_at(s, 30) is None
    assert views_at(s, 700) is None
    assert views_at(s, 60) == 100
    mid = views_at(s, 200)
    assert 100 < mid < 400


def test_forecast_on_organic_post_is_close():
    samples = organic(10_000)
    fc = forecast_24h(samples, PRIOR)
    assert fc is not None and fc.t_used == 1800
    assert 9_000 <= fc.est <= 11_000
    assert fc.low < fc.est < fc.high


def test_organic_post_has_no_flags():
    res = analyze_post(organic(10_000), subscribers=40_000, curve=PRIOR, published_at=NOON_UTC)
    assert res.flags == []
    assert res.complete


def test_late_boost_detected():
    # honest 3000 views/day, +9000 bot views from the 3rd hour
    res = analyze_post(boosted_late(3_000, 9_000), subscribers=40_000, curve=PRIOR, published_at=NOON_UTC)
    assert "late_boost" in res.flags
    assert res.ratio and res.ratio > 2.2


def test_dead_start_detected():
    samples = [(30, 0, 0), (60, 1, 0), (300, 5, 0), (600, 10, 0), (1800, 30, 0),
               (3600, 60, 0), (10_800, 4000, 0), (DAY, 9000, 0)]
    res = analyze_post(samples, subscribers=30_000, curve=PRIOR, published_at=NOON_UTC)
    assert "dead_start" in res.flags


def test_instant_dump_detected():
    samples = [(30, 2000, 0), (120, 5000, 0), (600, 5500, 0), (1800, 6000, 0), (3600, 6500, 0),
               (10_800, 7000, 0), (DAY, 8000, 0)]
    res = analyze_post(samples, subscribers=30_000, curve=PRIOR, published_at=NOON_UTC)
    assert "instant_dump" in res.flags


def test_rate_spike_without_forwards():
    samples = organic(10_000)
    # +6000 views between 6h and 8h without forwards growth
    f6 = dict((t, f) for t, _, f in samples)[21_600]
    spiked = []
    for t, v, f in samples:
        if t >= 28_800:
            v += 6000
            f = f6
        spiked.append((t, v, f))
    res = analyze_post(spiked, subscribers=40_000, curve=PRIOR, published_at=NOON_UTC)
    assert "rate_spike" in res.flags


def test_build_curve_learns_faster_audience():
    posts = [organic(5_000 + i * 100, speed=1.5) for i in range(30)]
    learned = build_curve(posts)
    assert learned.n_posts == 30
    assert learned.share(600) > PRIOR.share(600)
    assert learned.share(DAY) == 1.0


def test_channel_verdicts():
    curve = PRIOR
    clean = [analyze_post(organic(8_000 + i * 50), 30_000, curve, published_at=NOON_UTC) for i in range(5)]
    v = channel_verdict(clean, None)
    assert v.verdict == "clean"
    assert v.honest_reach and 7_500 < v.honest_reach < 9_000

    bad = [analyze_post(boosted_late(3_000, 9_000), 30_000, curve, published_at=NOON_UTC) for _ in range(4)]
    v = channel_verdict(bad, None)
    assert v.verdict == "boosted"
    assert v.score < 50
    # honest reach comes from early forecasts, not from inflated totals
    assert v.honest_reach and v.honest_reach < 4_000

    few = channel_verdict(clean[:1], None)
    assert few.verdict == "unknown"


def test_static_metrics_flags_uniform_and_low_err():
    posts = [PostStat(views=1000, forwards=0, reactions=0, age_hours=72) for _ in range(20)]
    m = static_metrics(posts, subscribers=100_000)
    assert "uniform_views" in m.flags
    assert "low_err" in m.flags

    import random
    rnd = random.Random(1)
    posts = [PostStat(views=rnd.randint(5000, 15000), forwards=rnd.randint(10, 80), reactions=rnd.randint(20, 200),
                      age_hours=72) for _ in range(20)]
    m = static_metrics(posts, subscribers=40_000)
    assert m.flags == []
    assert 0.1 < m.err < 0.5


def test_next_checkpoint():
    assert next_checkpoint(0) == 30
    assert next_checkpoint(31) == 60
    assert next_checkpoint(200_000) is None
