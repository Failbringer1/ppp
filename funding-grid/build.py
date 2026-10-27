"""Собирает сырые месячные файлы Binance в плотные матрицы [час, символ]."""
import glob, os
import numpy as np

T0 = np.datetime64("2020-12-01T00:00")
T1 = np.datetime64("2026-10-01T00:00")
H_MS = 3_600_000
t0_ms = int(T0.astype("datetime64[ms]").astype(np.int64))
T = int((T1 - T0) / np.timedelta64(1, "h"))

# Индексы, стейблкоины и прочее, что не является нормальным активом для сетки.
EXCLUDE = {
    "BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT", "USDCUSDT", "BUSDUSDT",
    "TUSDUSDT", "USDPUSDT", "FDUSDUSDT", "USDEUSDT", "EURUSDT", "GBPUSDT", "AEURUSDT",
    "USD1USDT", "RLUSDUSDT", "XUSDUSDT", "BFUSDUSDT", "USDTUSDT",
}

files = sorted(glob.glob("data/raw/*.npz"))
syms = [os.path.basename(f)[:-4] for f in files]
syms = [s for s in syms if s not in EXCLUDE]
N = len(syms)
print("T", T, "N", N)

O = np.full((T, N), np.nan, np.float32)
H = np.full((T, N), np.nan, np.float32)
L = np.full((T, N), np.nan, np.float32)
C = np.full((T, N), np.nan, np.float32)
QV = np.zeros((T, N), np.float32)
FR = np.full((T, N), np.nan, np.float32)   # ставка за интервал, проставлена в час расчёта
FI = np.zeros((T, N), np.float32)          # длина интервала фандинга, ч

for j, s in enumerate(syms):
    d = np.load(f"data/raw/{s}.npz")
    k, f = d["k"], d["f"]
    idx = np.rint((k[:, 0] - t0_ms) / H_MS).astype(np.int64)
    m = (idx >= 0) & (idx < T)
    idx, kk = idx[m], k[m]
    O[idx, j], H[idx, j], L[idx, j], C[idx, j], QV[idx, j] = kk[:, 1], kk[:, 2], kk[:, 3], kk[:, 4], kk[:, 5]
    if len(f):
        fi = np.rint((f[:, 0] - t0_ms) / H_MS).astype(np.int64)
        m = (fi >= 0) & (fi < T)
        FR[fi[m], j] = f[m, 2]
        FI[fi[m], j] = f[m, 1]

# Помесячная разметка и юниверс: топ-100 по обороту прошлого месяца,
# символ должен торговаться >= 600 часов в прошлом месяце (отсекаем свежие листинги).
hours = T0 + np.arange(T) * np.timedelta64(1, "h")
month = hours.astype("datetime64[M]")
months = np.unique(month)
month_idx = np.searchsorted(months, month).astype(np.int32)
M = len(months)
vol_m = np.zeros((M, N)); cnt_m = np.zeros((M, N))
for mi in range(M):
    sl = month_idx == mi
    vol_m[mi] = QV[sl].sum(0)
    cnt_m[mi] = np.isfinite(C[sl]).sum(0)
U = np.zeros((M, N), bool)
for mi in range(1, M):
    ok = cnt_m[mi - 1] >= 600
    v = np.where(ok, vol_m[mi - 1], -1)
    top = np.argsort(-v)[:100]
    top = top[v[top] > 0]
    U[mi, top] = True

# Делистинг/долгий разрыв: если следующий бар отсутствует >= 24 ч подряд (или данных больше нет),
# на этом баре позицию нужно принудительно закрыть.
valid = np.isfinite(C)
force_close = np.zeros((T, N), bool)
for j in range(N):
    v = np.flatnonzero(valid[:, j])
    if len(v) == 0:
        continue
    gaps = np.diff(v)
    ends = v[:-1][gaps > 24]
    force_close[ends, j] = True
    if v[-1] < T - 1:
        force_close[v[-1], j] = True

keep = U.any(0)
syms = np.array(syms)[keep]
np.savez("data/panel.npz", O=O[:, keep], H=H[:, keep], L=L[:, keep], C=C[:, keep],
         FR=FR[:, keep], FI=FI[:, keep], U=U[:, keep], month_idx=month_idx,
         force_close=force_close[:, keep], months=months.astype(str), syms=syms,
         vol_m=vol_m[:, keep])
print("universe sizes", U.sum(1)[:5], U.sum(1)[-5:])
print("ever in universe", U.any(0).sum())
