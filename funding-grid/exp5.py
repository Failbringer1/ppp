"""Сходимость по размеру свечи: старт 2025-01-01, свечи 1h / 15m / 5m, путь внутри свечи А и Б."""
import json, time
from run import run, metrics

FIX = dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0)
CFGS = [
    ("base", "Как в идее", {}),
    ("sl2", "Стоп 2% на актив", dict(sl_eq=0.02)),
    ("fix", "/5 + стоп 0.5% + плечо ≤ 1x", FIX),
    ("fixanti", "то же, против фандинга", dict(FIX, dir_mode=2)),
    ("fixshort", "то же, всегда шорт", dict(FIX, dir_mode=-1)),
    ("fixlong", "то же, всегда лонг", dict(FIX, dir_mode=1)),
]
out = {}
for key, name, kw in CFGS:
    for res in ["1h", "15m", "5m"]:
        for pm in [0, 1]:
            t0 = time.time()
            m = metrics(run(start="2025-01-01", res=res, path_mode=pm, **kw))
            out[f"{key}|{res}|{pm}"] = {k: m[k] for k in ("final", "cagr", "maxdd", "sharpe", "blown", "years")}
            print(f"{name:30s} {res:>3s} путь {'А' if pm == 0 else 'Б'}  итог {m['final']:6.3f}  CAGR {m['cagr']*100:+6.1f}%  "
                  f"DD {m['maxdd']*100:5.0f}%  Sh {m['sharpe']:5.2f} {('ЛИКВИДАЦИЯ '+m['blown'][:10]) if m['blown'] else ''}"
                  f"  ({time.time()-t0:.0f}s)", flush=True)
json.dump(out, open("results_5m.json", "w"))
