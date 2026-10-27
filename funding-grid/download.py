import io, re, json, zipfile, urllib.request, urllib.parse, time, sys
import numpy as np
from concurrent.futures import ThreadPoolExecutor
BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
START = "2020-12"
files = json.load(open("data/files.json"))

def fetch(key):
    url = BASE + urllib.parse.quote(key)
    for a in range(6):
        try:
            return urllib.request.urlopen(url, timeout=60).read()
        except Exception as e:
            if a == 5:
                print("FAIL", key, e, file=sys.stderr); return None
            time.sleep(2 ** a)

def rows(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    txt = z.read(z.namelist()[0]).decode()
    out = []
    for line in txt.splitlines():
        if not line or not line[0].isdigit():
            continue
        out.append(line.split(","))
    return out

def month_of(key):
    return re.search(r"-(\d{4}-\d{2})\.zip$", key).group(1)

def job(sym):
    v = files[sym]
    kl, fr = [], []
    for key in sorted(v["k"]):
        if month_of(key) < START: continue
        b = fetch(key)
        if b is None: continue
        for r in rows(b):
            t = int(r[0])
            if t > 10**14: t //= 1000
            kl.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])))
    for key in sorted(v["f"]):
        if month_of(key) < START: continue
        b = fetch(key)
        if b is None: continue
        for r in rows(b):
            t = int(r[0])
            if t > 10**14: t //= 1000
            if len(r) >= 3 and r[2] != "":
                fr.append((t, float(r[1]) if r[1] else 8.0, float(r[2])))
            else:
                fr.append((t, 8.0, float(r[1])))
    if not kl:
        return sym, 0, 0
    k = np.array(kl, dtype=np.float64); f = np.array(fr, dtype=np.float64).reshape(-1, 3)
    np.savez_compressed(f"data/raw/{sym}.npz", k=k, f=f)
    return sym, len(kl), len(fr)

syms = sorted(files)
t0 = time.time(); done = 0
with ThreadPoolExecutor(32) as ex:
    for sym, nk, nf in ex.map(job, syms):
        done += 1
        if done % 50 == 0:
            print(done, sym, nk, nf, f"{time.time()-t0:.0f}s", flush=True)
print("done", time.time() - t0)
