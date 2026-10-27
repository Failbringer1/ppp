import sys
import numpy as np
from run import run, panel
P = panel()
start = sys.argv[1]; kw = eval("dict(" + (sys.argv[2] if len(sys.argv) > 2 else "") + ")")
r = run(start=start, **kw)
st = r["st"]; end = r["blown"] if r["blown"] > 0 else len(r["eq"]) - 1
eq = r["eq"]
print("blown", r["hours"][r["blown"]] if r["blown"] > 0 else None)
step = max(24, (end - st) // 40 // 24 * 24)
for t in list(range(st, end, step)) + [end]:
    e = eq[t]
    print(f"{r['hours'][t]}  eq {e:8.3f}  worst {r['eqw'][t]:8.3f}  long {r['lng'][t]/max(e,1e-9):6.2f}x  short {r['sht'][t]/max(e,1e-9):6.2f}x  "
          f"npos {int(r['npos'][t]):3d}  full {int(r['nfull'][t]):3d}")
pk = np.nanargmax(np.where(np.arange(len(eq)) <= end, eq, np.nan))
print("peak", r["hours"][pk], eq[pk])
