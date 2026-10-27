import numpy as np
from sim import simulate

BASE = dict(
    lot_pct=0.005, max_lots=10,             # 0.5% старт, максимум 5% на актив
    step_fix=0.03, step_k=0.0, step_min=0.01, step_max=0.10,
    thr=0.0, alpha=1.0,                     # знак последнего фандинга
    exit_mode=1, unwind_h=24 * 7,
    sl_eq=0.0, cooldown_h=24 * 7,
    gross_cap=100.0, trail=0, side_mask=3,
    maker=0.0002, taker=0.0005, slip=0.0003, mmr=0.01, vol_win=24 * 30,
    dir_mode=0, rank_n=20, path_mode=0,
    fund_tp=0, drift_k=0.0, drift_win=24 * 90,
)
ORDER = ["lot_pct", "max_lots", "step_fix", "step_k", "step_min", "step_max", "thr", "alpha",
         "exit_mode", "unwind_h", "sl_eq", "cooldown_h", "gross_cap", "trail", "side_mask",
         "maker", "taker", "slip", "mmr", "vol_win", "dir_mode", "rank_n", "path_mode",
         "fund_tp", "drift_k", "drift_win"]

_P = {}
# имя панели -> (файл, баров в часе)
PANELS = {"1h": ("data/panel.npz", 1), "15m": ("data/panel15m.npz", 4), "5m": ("data/panel5m.npz", 12),
          "tradfi": ("data/panel_tradfi.npz", 1)}
BPH = {k: v[1] for k, v in PANELS.items()}


def panel(res="1h"):
    if res not in _P:
        d = np.load(PANELS[res][0])
        p = {k: d[k] for k in d.files}
        step = np.timedelta64(60 // BPH[res], "m")
        if "hours" in p:
            p["hours"] = p["hours"].astype("datetime64[m]")
        else:
            p["hours"] = np.datetime64("2020-12-01T00:00") + np.arange(p["O"].shape[0]) * step
        p["bph"] = BPH[res]
        _P[res] = p
    return _P[res]


def run(start="2021-01-01", end=None, dbg_j=-1, res="1h", **kw):
    P = panel(res)
    bph = P["bph"]
    cfg = dict(BASE); cfg.update(kw)
    for k in ("unwind_h", "cooldown_h", "vol_win", "drift_win"):
        cfg[k] = int(cfg[k] * bph)
    hours = P["hours"]
    st = int(np.searchsorted(hours, np.datetime64(start)))
    O, H, L, C, FR, FI, FC = P["O"], P["H"], P["L"], P["C"], P["FR"], P["FI"], P["force_close"]
    if end is not None:
        en = int(np.searchsorted(hours, np.datetime64(end)))
        O, H, L, C, FR, FI, FC = O[:en], H[:en], L[:en], C[:en], FR[:en], FI[:en], FC[:en].copy()
        FC[en - 1] = True
    args = [cfg[k] for k in ORDER]
    r = simulate(O, H, L, C, FR, FI, P["U"], P["month_idx"], FC, st, *args, bph, dbg_j)
    eq, eqw, gross, lng, sht, npos, nfull, fund, real, fee, unreal, cnt, pnl_asset, blown, dbg = r
    return dict(cfg=cfg, st=st, bph=bph, res=res, hours=hours[:len(eq)], eq=eq, eqw=eqw, gross=gross, lng=lng, sht=sht,
                npos=npos, nfull=nfull, fund=fund, real=real, fee=fee, unreal=unreal, cnt=cnt,
                pnl_asset=pnl_asset, blown=blown, dbg=dbg)


def metrics(r):
    P = panel(r["res"])
    bpd = 24 * r["bph"]
    st = r["st"]
    eq = r["eq"][st:]; eqw = r["eqw"][st:]; hrs = r["hours"][st:]
    ok = np.isfinite(eq) & (eq > 0)
    eq, eqw, hrs = eq[ok], eqw[ok], hrs[ok]
    yrs = (hrs[-1] - hrs[0]) / np.timedelta64(1, "h") / 8760
    final = eq[-1] if r["blown"] < 0 else 0.0
    cagr = final ** (1 / yrs) - 1 if final > 0 else -1
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1).min()
    ddw = (eqw / peak - 1).min()
    daily = eq[::bpd]
    rets = np.diff(daily) / daily[:-1]
    sharpe = rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else 0
    g = r["gross"][st:][ok] / eq
    years = {}
    ys = hrs.astype("datetime64[Y]")
    for y in np.unique(ys):
        m = ys == y
        e = eq[m]
        prev = eq[np.flatnonzero(m)[0] - 1] if np.flatnonzero(m)[0] > 0 else 1.0
        years[str(y)] = float(e[-1] / prev - 1)
    worst = np.argsort(r["pnl_asset"])[:8]; best = np.argsort(-r["pnl_asset"])[:5]
    syms = P["syms"]
    return dict(
        final=float(final), cagr=float(cagr), maxdd=float(dd), maxdd_intrabar=float(ddw),
        sharpe=float(sharpe), years=years,
        blown=str(r["hours"][r["blown"]]) if r["blown"] >= 0 else None,
        fund_long=float(r["fund"][0]), fund_short=float(r["fund"][1]),
        real_long=float(r["real"][0]), real_short=float(r["real"][1]),
        fee_long=float(r["fee"][0]), fee_short=float(r["fee"][1]),
        unreal_long=float(r["unreal"][0]), unreal_short=float(r["unreal"][1]),
        gross_avg=float(g.mean()), gross_max=float(g.max()),
        long_avg=float((r["lng"][st:][ok] / eq).mean()), short_avg=float((r["sht"][st:][ok] / eq).mean()),
        npos_avg=float(r["npos"][st:][ok].mean()), nfull_max=int(r["nfull"][st:][ok].max()),
        nfull_avg=float(r["nfull"][st:][ok].mean()),
        counts=dict(zip(["entries", "adds", "tps", "flip_exits", "stops", "delist", "timeouts", "be_exits"],
                        map(int, r["cnt"]))),
        worst=[(str(syms[i]), float(r["pnl_asset"][i])) for i in worst],
        best=[(str(syms[i]), float(r["pnl_asset"][i])) for i in best],
    )


def fmt(name, m):
    y = " ".join(f"{k}:{v*100:+.0f}%" for k, v in m["years"].items())
    return (f"{name:28s} final {m['final']:7.3f}  CAGR {m['cagr']*100:+6.1f}%  DD {m['maxdd']*100:6.1f}% "
            f"(intrabar {m['maxdd_intrabar']*100:6.1f}%)  Sh {m['sharpe']:5.2f}  gross avg {m['gross_avg']:.2f} "
            f"max {m['gross_max']:.2f}  L/S {m['long_avg']:.2f}/{m['short_avg']:.2f}  "
            f"{'BLOWN '+m['blown'] if m['blown'] else ''}\n{'':28s} {y}\n"
            f"{'':28s} fund L/S {m['fund_long']*100:+.1f}/{m['fund_short']*100:+.1f}%  "
            f"real L/S {m['real_long']*100:+.1f}/{m['real_short']*100:+.1f}%  "
            f"fee {-(m['fee_long']+m['fee_short'])*100:.1f}%  unreal {(m['unreal_long']+m['unreal_short'])*100:+.1f}%")
