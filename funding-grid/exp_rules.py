"""Новые правила: фандинг в прибыли лота при частичном закрытии (fund_tp) и фильтр одностороннего движения
(drift_k: годовой фандинг должен покрывать >= 75% годового темпа движения цены против нас).

python3 exp_rules.py tradfi   - TradFi-перпетуалы Binance (металлы, сырьё, индексы, акции США)
python3 exp_rules.py crypto   - длинная история криптоперпов (полигон для проверки правил)
"""
import sys
import numpy as np
from run import run, metrics, panel

which = sys.argv[1] if len(sys.argv) > 1 else "tradfi"
D = 24  # баров в сутках на часовых свечах
RULES = [
    ("Как в идее", {}),
    ("+ фандинг в тейке", dict(fund_tp=1)),
    ("+ фильтр тренда, окно 30 дн", dict(drift_k=0.75, drift_win=30 * D)),
    ("+ фильтр тренда, окно 90 дн", dict(drift_k=0.75, drift_win=90 * D)),
    ("+ оба правила (окно 30 дн)", dict(fund_tp=1, drift_k=0.75, drift_win=30 * D)),
    ("+ оба правила (окно 90 дн)", dict(fund_tp=1, drift_k=0.75, drift_win=90 * D)),
]

if which == "tradfi":
    P = panel("tradfi"); SYMS = list(P["syms"]); U0 = P["U"].copy()
    groups = {"Металлы и сырьё (6)": SYMS[:6], "Все 15": SYMS}
    for step in (0.03, 0.02):
        for g, members in groups.items():
            P["U"] = U0 & np.isin(np.array(SYMS), members)[None, :]
            print(f"=== шаг {step*100:.0f}%, {g} ===")
            for name, kw in RULES:
                r = run(start="2025-12-01", res="tradfi", step_fix=step, **kw)
                st = r["st"]; eq = r["eq"][st:]; ok = np.isfinite(eq) & (eq > 0); e = eq[ok]
                dd = (e / np.maximum.accumulate(e) - 1).min()
                final = e[-1] if r["blown"] < 0 else 0.0
                print(f"{name:32s} итог {(final-1)*100:+6.1f}%  просадка {dd*100:6.1f}%  фандинг {r['fund'].sum()*100:+5.1f}%  "
                      f"цена {(r['real'].sum()+r['unreal'].sum())*100:+6.1f}%  входов {int(r['cnt'][0]):4d}  тейков {int(r['cnt'][2]):5d}")
    P["U"] = U0
else:
    STARTS = ["2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01", "2025-01-01"]
    FIX = dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0)

    def cell(m):
        if m["blown"]:
            return f"💥 {m['blown'][5:7]}.{m['blown'][:4]}"
        return f"{m['cagr']*100:+.0f}% / {m['maxdd']*100:.0f}%"
    for title, extra in [("Как в идее: 0.5% → 5%, без стопа", {}), ("Размер /5 + стоп 0.5% + плечо ≤ 1x", FIX)]:
        print(f"=== {title} ===")
        for name, kw in RULES:
            cells = [cell(metrics(run(start=s, **extra, **kw))) for s in STARTS]
            print(f"| {name} | " + " | ".join(cells) + " |", flush=True)
