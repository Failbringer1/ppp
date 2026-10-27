"""Ставки центробанков (BIS, WS_CBPOL, дневные) -> data/rates.npz: days, ccy, R (годовые, доли)."""
import csv, io, sys, zipfile, urllib.request
import numpy as np

URL = "https://data.bis.org/static/bulk/WS_CBPOL_csv_flat.zip"
# валюта -> код страны BIS
AREA = {"USD": "US", "EUR": "XM", "JPY": "JP", "GBP": "GB", "CHF": "CH", "AUD": "AU", "NZD": "NZ",
        "CAD": "CA", "TRY": "TR", "ZAR": "ZA", "MXN": "MX", "HUF": "HU", "PLN": "PL", "CZK": "CZ",
        "SEK": "SE", "NOK": "NO", "DKK": "DK", "HKD": "HK", "CNH": "CN", "ILS": "IL", "SGD": "SG"}
START = np.datetime64("2014-12-01")

blob = urllib.request.urlopen(URL, timeout=300).read()
z = zipfile.ZipFile(io.BytesIO(blob))
f = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig")
r = csv.reader(f)
hdr = next(r)
i_freq, i_area, i_t, i_v = (next(i for i, h in enumerate(hdr) if h.startswith(p))
                            for p in ("FREQ", "REF_AREA", "TIME_PERIOD", "OBS_VALUE"))
want = {v: k for k, v in AREA.items()}
obs = {k: {} for k in AREA}
for row in r:
    if not row[i_freq].startswith("D:"):
        continue
    a = row[i_area].split(":")[0]
    if a not in want or row[i_v] == "":
        continue
    d = np.datetime64(row[i_t][:10])
    if d >= START:
        obs[want[a]][d] = float(row[i_v]) / 100

end = max(max(v) for v in obs.values() if v)
days = np.arange(START, end + 1)
ccy = sorted(k for k in AREA if obs[k])
R = np.full((len(days), len(ccy)), np.nan)
for j, c in enumerate(ccy):
    for d, v in obs[c].items():
        R[(d - START).astype(int), j] = v
    # пропуски (выходные) - последнее известное значение
    for t in range(1, len(days)):
        if np.isnan(R[t, j]):
            R[t, j] = R[t - 1, j]
    print(c, AREA[c], len(obs[c]), "наблюдений, последнее", max(obs[c]), f"{R[-1, j]*100:.2f}%", file=sys.stderr)
missing = [k for k in AREA if not obs[k]]
if missing:
    print("нет данных:", missing, file=sys.stderr)
np.savez_compressed("data/rates.npz", days=days, ccy=np.array(ccy), R=R)
