"""Часовые свечи Dukascopy (BID; ASK - раз в квартал, для спреда) -> data/duka/{SYM}/{side}_{YYYY}_{MM}.bi5 (кеш, докачка).

Формат файла: lzma, записи по 24 байта big-endian: секунда от начала месяца, O, C, L, H (int), объём (float).
Месяц в URL - с нуля. Свечи с нулевым объёмом - заполнение выходных, отбрасываются при сборке.
datafeed.dukascopy.com отвечает по http (https из некоторых сетей не проходит рукопожатие), каждый
запрос ~10 с до первого байта, поэтому качаем в много потоков: python download_fx.py 24.
"""
import os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

FX = """EURUSD GBPUSD USDJPY USDCHF AUDUSD NZDUSD USDCAD EURGBP EURJPY EURCHF EURAUD EURNZD EURCAD GBPJPY
GBPCHF GBPAUD GBPNZD GBPCAD AUDJPY AUDCHF AUDNZD AUDCAD NZDJPY NZDCHF NZDCAD CADJPY CADCHF CHFJPY
USDTRY EURTRY TRYJPY USDZAR EURZAR ZARJPY USDMXN EURMXN MXNJPY USDHUF EURHUF USDPLN EURPLN GBPPLN CHFPLN
USDCZK EURCZK USDSEK EURSEK USDNOK EURNOK NOKSEK SEKJPY NOKJPY USDDKK USDILS""".split()
METALS = ["XAUUSD", "XAGUSD"]
INDICES = """USA500IDXUSD USATECHIDXUSD USA30IDXUSD DEUIDXEUR GBRIDXGBP JPNIDXJPY FRAIDXEUR EUSIDXEUR
AUSIDXAUD CHEIDXCHF HKGIDXHKD""".split()
SYMS = FX + METALS + INDICES
Y0, Y1, M1 = 2015, 2026, 9          # по октябрь 2026 включительно (месяц с нуля)
URL = "http://datafeed.dukascopy.com/datafeed/{s}/{y}/{m:02d}/{side}_candles_hour_1.bi5"


def fetch(s, y, m, side):
    path = f"data/duka/{s}/{side}_{y}_{m:02d}.bi5"
    if os.path.exists(path):
        return 0
    url = URL.format(s=s, y=y, m=m, side=side)
    for a in range(8):
        try:
            b = urllib.request.urlopen(url, timeout=60).read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                b = b""
                break
            time.sleep(1 + a)
        except Exception:
            time.sleep(1 + a)
    else:
        print("FAIL", url, file=sys.stderr)
        return -1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:      # пустой файл - месяца нет (404), чтобы не спрашивать повторно
        f.write(b)
    return len(b)


if __name__ == "__main__":
    # ASK нужен только для оценки спреда - берём первый месяц квартала
    jobs = [(s, y, m, side) for s in SYMS for y in range(Y0, Y1 + 1) for m in range(12)
            if (y, m) <= (Y1, M1) for side in ("BID", "ASK") if side == "BID" or m % 3 == 0]
    if "rev" in sys.argv:            # второй процесс идёт с конца списка навстречу первому
        jobs = jobs[::-1]
    t0 = time.time(); done = 0; fails = 0
    with ThreadPoolExecutor(int(sys.argv[1]) if len(sys.argv) > 1 else 6) as ex:
        for r in ex.map(lambda j: fetch(*j), jobs):
            done += 1; fails += r < 0
            if done % 1000 == 0:
                print(done, "/", len(jobs), f"{time.time()-t0:.0f}s", "ошибок", fails, flush=True)
    print("done", time.time() - t0, "ошибок", fails)
