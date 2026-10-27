import re, urllib.request, json
BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
def list_prefixes(prefix):
    out, marker = [], ""
    while True:
        url = f"{BASE}?delimiter=/&prefix={prefix}&max-keys=1000" + (f"&marker={marker}" if marker else "")
        x = urllib.request.urlopen(url, timeout=60).read().decode()
        ps = re.findall(r"<Prefix>([^<]+)</Prefix>", x)
        ps = [p for p in ps if p != prefix]
        out += ps
        if "<IsTruncated>true</IsTruncated>" in x:
            m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
            marker = m.group(1) if m else ps[-1]
        else:
            break
    return out
k = list_prefixes("data/futures/um/monthly/klines/")
f = list_prefixes("data/futures/um/monthly/fundingRate/")
ks = sorted(p.split("/")[-2] for p in k)
fs = sorted(p.split("/")[-2] for p in f)
print(len(ks), len(fs))
json.dump({"klines": ks, "funding": fs}, open("data/symbols.json", "w"))
usdt = [s for s in ks if s.endswith("USDT") and "_" not in s]
print("usdt perps (klines):", len(usdt), "with funding:", len(set(usdt) & set(fs)))
