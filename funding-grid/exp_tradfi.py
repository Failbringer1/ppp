"""Идея на TradFi-перпетуалах Binance (металлы, сырьё, индексы и акции США), декабрь 2025 - сентябрь 2026."""
import json
import numpy as np
from run import run, panel

P = panel("tradfi")
SYMS = list(P["syms"])
U0 = P["U"].copy()
GROUPS = {
    "Металлы и сырьё (6)": SYMS[:6],
    "Металлы, сырьё, индексы, акции (15)": SYMS,
}
CFGS = [
    ("Как в идее: 0.5% → 5%, шаг 3%", {}),
    ("Шаг 1%", dict(step_fix=0.01)),
    ("Шаг 2%", dict(step_fix=0.02)),
    ("Шаг 0.5 дневной волатильности", dict(step_fix=0.0, step_k=0.5, vol_win=24 * 14)),
    ("Шаг 1% + стоп 2% на актив", dict(step_fix=0.01, sl_eq=0.02)),
    ("Контроль: против фандинга", dict(dir_mode=2)),
    ("Контроль: всегда шорт", dict(dir_mode=-1)),
    ("Контроль: всегда лонг", dict(dir_mode=1)),
]
out = {}
for g, members in GROUPS.items():
    P["U"] = U0 & np.isin(np.array(SYMS), members)[None, :]
    print(f"=== {g} ===")
    for name, kw in CFGS:
        r = run(start="2025-12-01", res="tradfi", **kw)
        st = r["st"]; eq = r["eq"][st:]; ok = np.isfinite(eq) & (eq > 0)
        e = eq[ok]; peak = np.maximum.accumulate(e)
        dd = (e / peak - 1).min()
        final = e[-1] if r["blown"] < 0 else 0.0
        g_avg = np.nanmean(r["gross"][st:][ok] / e)
        fund = r["fund"].sum(); price = r["real"].sum() + r["unreal"].sum(); fee = r["fee"].sum()
        pa = r["pnl_asset"]
        mem = [j for j in range(len(SYMS)) if SYMS[j] in members]
        order = sorted(mem, key=lambda j: pa[j])
        worst = ", ".join(f"{SYMS[j][:-4]} {pa[j]*100:+.1f}%" for j in order[:3])
        best = ", ".join(f"{SYMS[j][:-4]} {pa[j]*100:+.1f}%" for j in order[::-1][:3])
        print(f"{name:34s} итог {(final-1)*100:+6.1f}%  просадка {dd*100:6.1f}%  позиция в ср. {g_avg:.2f}x  "
              f"фандинг {fund*100:+5.1f}%  цена {price*100:+6.1f}%  комиссии {-fee*100:5.1f}%  "
              f"{'ЛИКВИДАЦИЯ' if r['blown'] >= 0 else ''}")
        print(f"{'':34s} хуже всех: {worst} | лучше всех: {best}")
        out[f"{g}|{name}"] = dict(ret=float(final - 1), dd=float(dd), gross=float(g_avg), fund=float(fund),
                                  price=float(price), fee=float(fee), blown=bool(r["blown"] >= 0),
                                  pnl_asset={SYMS[j]: float(pa[j]) for j in range(len(SYMS))})
P["U"] = U0
json.dump(out, open("results_tradfi.json", "w"), ensure_ascii=False)
