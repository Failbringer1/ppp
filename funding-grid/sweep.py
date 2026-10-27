"""Чувствительность «подстрахованной» версии к размеру, стопу и лимиту плеча (полный период 2021-01..2026-09).

python3 sweep.py [1h|15m] [0|1]   - размер свечи и путь внутри свечи (0 = А, 1 = Б)
"""
import itertools, json, sys
import numpy as np
from run import run, metrics

res = sys.argv[1] if len(sys.argv) > 1 else "1h"
pm = int(sys.argv[2]) if len(sys.argv) > 2 else 0
rows = []
for lot, sl, cap in itertools.product([0.0005, 0.001, 0.002], [0.0025, 0.005, 0.01], [0.5, 1.0, 2.0]):
    m = metrics(run(start="2021-01-01", res=res, path_mode=pm, lot_pct=lot, sl_eq=sl, gross_cap=cap))
    rows.append(dict(lot=lot, sl=sl, cap=cap, final=m["final"], cagr=m["cagr"], dd=m["maxdd"], sharpe=m["sharpe"],
                     blown=m["blown"], years=m["years"]))
    print(f"лот {lot*100:.2f}% (макс {lot*1000:.1f}%)  стоп {sl*100:.2f}%  плечо ≤ {cap:.1f}x  →  итог {m['final']:6.2f}  "
          f"CAGR {m['cagr']*100:+6.1f}%  DD {m['maxdd']*100:5.0f}%  Sh {m['sharpe']:5.2f}  "
          f"{'ЛИКВИДАЦИЯ '+m['blown'][:10] if m['blown'] else ''}", flush=True)
c = np.array([r["cagr"] for r in rows]); d = np.array([r["dd"] for r in rows])
print(f"медиана CAGR {np.median(c)*100:+.1f}%, диапазон {c.min()*100:+.1f}..{c.max()*100:+.1f}%; "
      f"медиана DD {np.median(d)*100:.0f}%")
json.dump(rows, open(f"sweep_{res}_{pm}.json", "w"))
