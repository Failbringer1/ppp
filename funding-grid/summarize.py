"""Markdown-таблицы для README из results_all.json / results_15m.json / results_5m.json."""
import json, os
from experiments import EXPS, STARTS


def cell(m):
    if m["blown"]:
        y, mo = m["blown"][:4], m["blown"][5:7]
        return f"💥 {mo}.{y}"
    return f"{m['cagr']*100:+.0f}% / {m['maxdd']*100:.0f}%"


out = json.load(open("results_all.json"))
print("| Вариант | " + " | ".join(f"старт {s[:4]}" for s in STARTS) + " |")
print("|---|" + "---|" * len(STARTS))
for key, name, _ in EXPS:
    if f"{key}|{STARTS[0]}" not in out:
        continue
    print(f"| {name} | " + " | ".join(cell(out[f'{key}|{s}']) for s in STARTS) + " |")

if os.path.exists("results_15m.json"):
    r = json.load(open("results_15m.json"))
    print()
    names = {"base": "Как в идее", "s5": "Размер /5", "sl2": "Стоп 2% на актив", "fix": "/5 + стоп 0.5% + плечо ≤ 1x",
             "fixanti": "то же, против фандинга", "fixshort": "то же, всегда шорт", "fixlong": "то же, всегда лонг"}
    print("| Вариант | свечи | путь | " + " | ".join(f"старт {s[:4]}" for s in STARTS) + " |")
    print("|---|---|---|" + "---|" * len(STARTS))
    for key, name in names.items():
        for res in ["1h", "15m"]:
            for pm in [0, 1]:
                k0 = f"{key}|{res}|{pm}|{STARTS[0]}"
                if k0 not in r:
                    continue
                print(f"| {name} | {res} | {'А' if pm == 0 else 'Б'} | " +
                      " | ".join(cell(r[f'{key}|{res}|{pm}|{s}']) for s in STARTS) + " |")
