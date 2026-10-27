"""Панель на 5-минутных барах за 2024-12..2026-09 (только символы, бывшие в юниверсе в этот период)."""
import os
import numpy as np

d = np.load("data/panel.npz")
syms, months, U, mi1 = d["syms"], d["months"], d["U"], d["month_idx"]
hours1 = np.datetime64("2020-12-01T00:00") + np.arange(d["O"].shape[0]) * np.timedelta64(1, "h")
h0 = int(np.searchsorted(hours1, np.datetime64("2024-12-01T00:00")))
m0 = int(mi1[h0])
keep = (U[m0:].any(0)) | (U[m0 - 1])
cols = np.flatnonzero(keep)
B = 12
T1 = d["O"].shape[0] - h0
T = T1 * B
N = len(cols)
t0_ms = int(np.datetime64("2024-12-01T00:00", "ms").astype(np.int64))
STEP = 5 * 60 * 1000
O = np.full((T, N), np.nan, np.float32); H = np.full((T, N), np.nan, np.float32)
L = np.full((T, N), np.nan, np.float32); C = np.full((T, N), np.nan, np.float32)
for jj, j in enumerate(cols):
    s = syms[j]
    for m in months[m0:]:
        f = f"data/raw5/{s}-{str(m)[:7]}.npy"
        if not os.path.exists(f):
            continue
        k = np.load(f)
        if len(k) == 0:
            continue
        idx = np.rint((k[:, 0] - t0_ms) / STEP).astype(np.int64)
        ok = (idx >= 0) & (idx < T)
        idx, k = idx[ok], k[ok]
        O[idx, jj], H[idx, jj], L[idx, jj], C[idx, jj] = k[:, 1], k[:, 2], k[:, 3], k[:, 4]
FR = np.full((T, N), np.nan, np.float32); FI = np.zeros((T, N), np.float32)
FR[::B] = d["FR"][h0:, cols]; FI[::B] = d["FI"][h0:, cols]
month_idx = np.repeat(mi1[h0:] - m0, B).astype(np.int32)
valid = np.isfinite(C)
force_close = np.zeros((T, N), bool)
for j in range(N):
    v = np.flatnonzero(valid[:, j])
    if len(v) == 0:
        continue
    gaps = np.diff(v)
    force_close[v[:-1][gaps > 24 * B], j] = True
    if v[-1] < T - 1:
        force_close[v[-1], j] = True
hours = np.datetime64("2024-12-01T00:00") + np.arange(T) * np.timedelta64(5, "m")
np.savez("data/panel5m.npz", O=O, H=H, L=L, C=C, FR=FR, FI=FI, U=U[m0:, cols], month_idx=month_idx,
         force_close=force_close, months=months[m0:], syms=syms[cols], hours=hours.astype("datetime64[m]").astype(np.int64))
c1 = d["C"][h0:, cols]; c5 = C[B - 1::B]
both = np.isfinite(c1) & np.isfinite(c5)
print("bars", T, "symbols", N, "close mismatch >0.1%:", (np.abs(c5[both] / c1[both] - 1) > 1e-3).mean())
