"""Валюты, металлы, индексы (Dukascopy, 1h) + ставки ЦБ (BIS) -> data/panel_fx.npz.

CARRY - справедливый годовой перенос для лонга на барах начисления свопа, NaN на остальных:
  валюта  - ставка базовой валюты минус ставка котируемой;
  металлы - минус ставка доллара (ставка аренды металла не учитывается);
  индексы - дивидендная доходность минус ставка валюты индекса (DAX - индекс полной доходности, 0).
Своп начисляется на последнем баре каждого будня (UTC), в среду за три дня (FI = 72 ч).
FAIR - тот же перенос, протянутый на все бары, только для валют (для них действует правило 75%).
HS - половина спреда (медиана (ASK - BID) / BID за первый месяц квартала), действует весь квартал.
Площадку (наценку на своп и комиссию) задаёт exp_fx.py.
"""
import lzma, os, struct
import numpy as np
from download_fx import FX, METALS, INDICES, SYMS

T0 = np.datetime64("2015-01-01T00:00", "m")
T1 = np.datetime64("2026-11-01T00:00", "m")
hours = np.arange(T0, T1, np.timedelta64(60, "m"))
T, N = len(hours), len(SYMS)
# дивидендная доходность индексов, средняя за 2015-2026, приблизительно
DIV = {"USA500IDXUSD": 0.018, "USATECHIDXUSD": 0.009, "USA30IDXUSD": 0.021, "DEUIDXEUR": 0.0,
       "GBRIDXGBP": 0.038, "JPNIDXJPY": 0.018, "FRAIDXEUR": 0.029, "EUSIDXEUR": 0.032,
       "AUSIDXAUD": 0.042, "CHEIDXCHF": 0.029, "HKGIDXHKD": 0.035}


def read(sym, side, y, m):
    path = f"data/duka/{sym}/{side}_{y}_{m:02d}.bi5"
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    d = lzma.decompress(open(path, "rb").read())
    a = np.array(list(struct.iter_unpack(">5if", d)), dtype=np.float64).reshape(-1, 6)
    a = a[a[:, 5] > 0]                       # нулевой объём - выходные
    if not len(a):
        return None
    t0 = np.datetime64(f"{y}-{m + 1:02d}-01T00:00", "m")
    idx = ((t0 - T0) / np.timedelta64(1, "h")).astype(int) + (a[:, 0] // 3600).astype(int)
    return idx, a[:, 1], a[:, 4], a[:, 3], a[:, 2]          # записи O, C, L, H -> O, H, L, C


O = np.full((T, N), np.nan); H = O.copy(); L = O.copy(); C = O.copy()
HS = np.full((T, N), np.nan, np.float32)
months = np.arange(np.datetime64("2015-01"), np.datetime64("2026-11"))
for j, s in enumerate(SYMS):
    for mo in months:
        y, m = int(str(mo)[:4]), int(str(mo)[5:]) - 1
        b = read(s, "BID", y, m)
        if b is None:
            continue
        i, o, h, l, c = b
        ok = (i >= 0) & (i < T)
        O[i[ok], j], H[i[ok], j], L[i[ok], j], C[i[ok], j] = o[ok], h[ok], l[ok], c[ok]
        if m % 3 == 0:
            a = read(s, "ASK", y, m)
            if a is not None:
                ia, _, _, _, ca = a
                _, x, y_ = np.intersect1d(i, ia, return_indices=True)
                if len(x) > 20:
                    sp = (ca[y_] - c[x]) / c[x]
                    t_a = int(((np.datetime64(mo, "m") - T0) / np.timedelta64(1, "h")))
                    t_b = int(((np.datetime64(mo + 3, "m") - T0) / np.timedelta64(1, "h")))
                    HS[t_a:min(t_b, T), j] = np.median(sp[sp > 0]) / 2 if (sp > 0).any() else np.nan
    # смена масштаба цены в архиве (на некоторых экзотиках в 2024 - в 10 раз): приводим историю к новому
    ok = np.flatnonzero(np.isfinite(C[:, j]))
    while True:
        lr = np.log(C[ok[1:], j] / C[ok[:-1], j])
        jumps = np.flatnonzero(np.abs(np.abs(lr) - np.log(10)) < 0.1)
        if not len(jumps):
            break
        k = jumps[-1]
        f = np.exp(lr[k])
        for A in (O, H, L, C):
            A[:ok[k] + 1, j] *= f
        print("  ", s, f"масштаб x{f:.2f} до", hours[ok[k + 1]])
    n = np.isfinite(C[:, j]).sum()
    print(s, n, "баров", f"полспреда {np.nanmedian(HS[:, j])*1e4:.2f} б.п." if np.isfinite(HS[:, j]).any() else "")
# квартал без ASK - ближайшее известное значение
for j in range(N):
    v = HS[:, j]
    ok = np.flatnonzero(np.isfinite(v))
    if len(ok):
        v[:ok[0]] = v[ok[0]]
        for t in range(ok[0] + 1, T):
            if not np.isfinite(v[t]):
                v[t] = v[t - 1]

# --- ставки ---
rz = np.load("data/rates.npz")
rdays, rccy, R = rz["days"], list(rz["ccy"]), rz["R"].copy()
# ЕЦБ: до 18.09.2024 BIS даёт ставку основного рефинансирования, рынок шёл по депозитной ставке
e = rccy.index("EUR")
prev = np.datetime64("2000-01-01")
for until, spread in (("2015-12-09", 0.0025), ("2016-03-16", 0.0035), ("2019-09-18", 0.0040), ("2024-09-18", 0.0050)):
    m_ = (rdays >= prev) & (rdays < np.datetime64(until))
    R[m_, e] -= spread
    prev = np.datetime64(until)
# Банк Японии до 2016 года ставку не публиковал (таргет денежной базы), колл-рейт ~0.05%
jp = rccy.index("JPY")
R[np.isnan(R[:, jp]), jp] = 0.0005
for k, c in enumerate(rccy):
    first = np.flatnonzero(np.isfinite(R[:, k]))[0]
    R[:first, k] = R[first, k]


def rate(ccy):
    return R[:, rccy.index(ccy)]


CAR_D = np.full((len(rdays), N), np.nan)       # годовой перенос лонга по дням
kind = []
for j, s in enumerate(SYMS):
    if s in FX:
        CAR_D[:, j] = rate(s[:3]) - rate(s[3:]); kind.append("fx")
    elif s in METALS:
        CAR_D[:, j] = -rate("USD"); kind.append("metal")
    else:
        CAR_D[:, j] = DIV[s] - rate(s[-3:]); kind.append("index")
day_of_bar = hours.astype("datetime64[D]")
di = np.clip(((day_of_bar - rdays[0]).astype(int)), 0, len(rdays) - 1)   # после конца ставок - последнее значение
car_bar = CAR_D[di]                                                           # (T, N)

# бары начисления: последний бар с данными в каждый будний день UTC
CARRY = np.full((T, N), np.nan, np.float32); FI = np.zeros((T, N), np.float32)
dow = ((day_of_bar.astype(int) + 3) % 7)          # 0 = понедельник
valid = np.isfinite(O)
day_id = (day_of_bar - day_of_bar[0]).astype(int)
for j in range(N):
    idx = np.flatnonzero(valid[:, j] & (dow < 5))
    if not len(idx):
        continue
    last = idx[np.r_[day_id[idx][1:] != day_id[idx][:-1], True]]
    CARRY[last, j] = car_bar[last, j]
    FI[last, j] = np.where(dow[last] == 2, 72, 24)
FAIR = np.where(np.array([k == "fx" for k in kind])[None, :], car_bar, np.nan).astype(np.float32)

# юниверс: в прошлом месяце не меньше 200 часов с данными
month_idx = ((hours.astype("datetime64[M]") - months[0]).astype(int)).astype(np.int32)
cnt = np.zeros((len(months), N))
np.add.at(cnt, month_idx, valid)
U = np.zeros((len(months), N), bool)
U[1:] = cnt[:-1] >= 200
# принудительное закрытие: перед разрывом данных больше 150 ч и на последнем баре актива
force_close = np.zeros((T, N), bool)
for j in range(N):
    idx = np.flatnonzero(valid[:, j])
    if not len(idx):
        continue
    gap = np.diff(idx) > 150
    force_close[idx[:-1][gap], j] = True
    if idx[-1] < T - 150:
        force_close[idx[-1], j] = True
print("бары", T, "активы", N, "разрывов >150ч", int(force_close.sum()))
np.savez_compressed("data/panel_fx.npz", O=O.astype(np.float32), H=H.astype(np.float32), L=L.astype(np.float32),
                    C=C.astype(np.float32), CARRY=CARRY, FI=FI, FAIR=FAIR, HS=HS, U=U, month_idx=month_idx,
                    force_close=force_close, syms=np.array(SYMS), kind=np.array(kind), hours=hours)
