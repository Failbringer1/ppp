"""5-минутные свечи для проверки точности (только 2024-12..2026-09, символы из юниверса в m или m-1)."""
import io, zipfile, urllib.request, urllib.parse, time, sys, os
import numpy as np
from concurrent.futures import ThreadPoolExecutor
BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/monthly/klines/"
d = np.load("data/panel.npz")
U, months, syms = d["U"], d["months"], d["syms"]
need = U.copy()
need[1:] |= U[:-1]
jobs = [(str(syms[j]), str(months[m])[:7]) for m in range(len(months)) for j in np.flatnonzero(need[m])
        if str(months[m])[:7] >= "2024-12"]
print("files needed", len(jobs), flush=True)
os.makedirs("data/raw5", exist_ok=True)

def fetch(sym, ym):
    out = f"data/raw5/{sym}-{ym}.npy"
    if os.path.exists(out):
        return 1
    url = BASE + urllib.parse.quote(f"{sym}/5m/{sym}-5m-{ym}.zip")
    for a in range(6):
        try:
            blob = urllib.request.urlopen(url, timeout=60).read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 0
            time.sleep(2 ** a)
        except Exception as e:
            if a == 5:
                print("FAIL", sym, ym, e, file=sys.stderr); return 0
            time.sleep(2 ** a)
    z = zipfile.ZipFile(io.BytesIO(blob))
    rows = []
    for line in z.read(z.namelist()[0]).decode().splitlines():
        if not line or not line[0].isdigit():
            continue
        r = line.split(",")
        t = int(r[0])
        if t > 10**14: t //= 1000
        rows.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    np.save(out, np.array(rows, dtype=np.float64))
    return 1

t0 = time.time(); n = 0; ok = 0
with ThreadPoolExecutor(32) as ex:
    for r in ex.map(lambda a: fetch(*a), jobs):
        n += 1; ok += r
        if n % 500 == 0:
            print(n, ok, f"{time.time()-t0:.0f}s", flush=True)
print("done", n, ok, time.time() - t0, flush=True)
