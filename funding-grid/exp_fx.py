"""Валюты, металлы, индексы 2015-2026 (data/panel_fx.npz): форекс-брокер с наценкой на своп и фьючерсы IBKR.

Площадка задаёт своп и издержки:
  брокер, наценка m годовых с каждой стороны: лонг получает CARRY - m, шорт получает -CARRY - m;
      издержки - половина спреда Dukascopy + комиссия 0.003% на валюте (raw-счёт, $3 за лот за сторону);
  фьючерсы IBKR: наценки нет (перенос встроен в цену контракта), половина спреда + 0.002% комиссии.
Валютное правило (fair_k=0.75) действует только на валюты, фильтра тренда нет.
"""
import sys
import numpy as np
from run import run, panel, metrics

P = panel("fx")
SYMS = list(P["syms"]); KIND = P["kind"]
CARRY, FI, HS = P["CARRY"], P["FI"], P["HS"]
U0 = P["U"].copy()
VENUES = {
    "брокер, наценка 1%": (0.01, np.where(KIND == "fx", 0.00003, 0.0)),
    "брокер, наценка 2%": (0.02, np.where(KIND == "fx", 0.00003, 0.0)),
    "фьючерсы IBKR": (0.0, np.full(len(SYMS), 0.00002)),
}
GROUPS = {"валюты": KIND == "fx", "металлы и индексы": KIND != "fx", "всё": np.ones(len(SYMS), bool)}
COST = dict(maker=0.0, taker=0.0, slip=0.0001)      # издержки задаются через SPR
IDEA = dict(lot_pct=0.005, max_lots=10, step_fix=0.03, fair_k=0.75)
FIX = dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0)
VARIANTS = [
    ("Как в идее: 0.5% → 5%, шаг 3%", {}),
    ("Шаг 1%", dict(step_fix=0.01)),
    ("Шаг = дневная волатильность", dict(step_fix=0.0, step_k=1.0, step_min=0.002, step_max=0.05)),
    ("Шаг 1%, без правила 75%", dict(step_fix=0.01, fair_k=0.0)),
    ("Шаг 1%, /5 + стоп 0.5% + плечо ≤ 1x", dict(step_fix=0.01, **FIX)),
    ("Контроль к шагу 1%: против свопа", dict(step_fix=0.01, dir_mode=2, fair_k=0.0)),
    ("Контроль к шагу 1%: всегда лонг", dict(step_fix=0.01, dir_mode=1, fair_k=0.0)),
    ("Контроль к шагу 1%: всегда шорт", dict(step_fix=0.01, dir_mode=-1, fair_k=0.0)),
]
STARTS = ["2015-01-01", "2018-01-01", "2021-01-01", "2024-01-01"]


def set_venue(m, comm):
    k = FI / 8760.0
    P["FR"] = np.where(np.isfinite(CARRY), (m - CARRY) * k, np.nan).astype(np.float32)
    P["FS"] = np.where(np.isfinite(CARRY), (CARRY + m) * k, np.nan).astype(np.float32)
    P["SPR"] = (HS + comm[None, :]).astype(np.float32)


def cell(m):
    if m["blown"]:
        return f"💥 {m['blown'][5:7]}.{m['blown'][:4]}"
    return f"{m['cagr']*100:+.1f}% / {m['maxdd']*100:.0f}%"


def go(venues, groups):
    for vn in venues:
        set_venue(*VENUES[vn])
        for g in groups:
            P["U"] = U0 & GROUPS[g][None, :]
            print(f"\n=== {vn}, {g} === (годовых / макс. просадка)")
            print("| Вариант | " + " | ".join(f"старт {s[:4]}" for s in STARTS) + " |")
            for name, kw in VARIANTS:
                cfg = dict(COST); cfg.update(IDEA); cfg.update(kw)
                cells = []
                for s in STARTS:
                    r = run(start=s, res="fx", **cfg)
                    mt = metrics(r)
                    cells.append(cell(mt))
                    if s == STARTS[0]:
                        det = (f"    {s[:4]}: своп {r['fund'].sum()*100:+.1f}%  цена {(r['real'].sum()+r['unreal'].sum())*100:+.1f}%  "
                               f"издержки {r['fee'].sum()*100:.1f}%  позиция ср. {mt['gross_avg']:.2f} макс {mt['gross_max']:.2f} "
                               f"депозита  входов {int(r['cnt'][0])}  лучшие {mt['best']}  худшие {mt['worst'][:4]}")
                print(f"| {name} | " + " | ".join(cells) + " |", flush=True)
                print(det, flush=True)
    P["U"] = U0


if __name__ == "__main__":
    go(list(VENUES) if len(sys.argv) < 2 else [list(VENUES)[int(sys.argv[1])]], list(GROUPS))
