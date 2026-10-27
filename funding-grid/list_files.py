import re, json, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
syms = json.load(open("data/symbols.json"))
usdt = [s for s in syms["klines"] if s.endswith("USDT") and "_" not in s]
def list_keys(prefix):
    out, marker = [], ""
    while True:
        url = f"{BASE}?prefix={urllib.parse.quote(prefix)}&max-keys=1000" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
        for a in range(5):
            try:
                x = urllib.request.urlopen(url, timeout=60).read().decode(); break
            except Exception:
                if a == 4: raise
        ks = re.findall(r"<Key>([^<]+)</Key>", x)
        out += ks
        if "<IsTruncated>true</IsTruncated>" in x and ks:
            marker = ks[-1]
        else:
            break
    return [k for k in out if k.endswith(".zip")]
def job(s):
    return s, list_keys(f"data/futures/um/monthly/klines/{s}/1h/"), list_keys(f"data/futures/um/monthly/fundingRate/{s}/")
res = {}
with ThreadPoolExecutor(16) as ex:
    for s, k, f in ex.map(job, usdt):
        res[s] = {"k": k, "f": f}
json.dump(res, open("data/files.json", "w"))
nk = sum(len(v["k"]) for v in res.values()); nf = sum(len(v["f"]) for v in res.values())
print("symbols", len(res), "kline files", nk, "funding files", nf)
