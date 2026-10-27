"""Панель на 15-минутных барах: юниверс и фандинг берём из часовой панели."""
import os
import numpy as np

d = np.load("data/panel.npz")
syms, months, U, mi1 = d["syms"], d["months"], d["U"], d["month_idx"]
T1, N = d["O"].shape
B = 4
T = T1 * B
t0_ms = int(np.datetime64("2020-12-01T00:00", "ms").astype(np.int64))
STEP = 15 * 60 * 1000
O = np.full((T, N), np.nan, np.float32); H = np.full((T, N), np.nan, np.float32)
L = np.full((T, N), np.nan, np.float32); C = np.full((T, N), np.nan, np.float32)
for j, s in enumerate(syms):
    for m in months:
        f = f"data/raw15/{s}-{str(m)[:7]}.npy"
        if not os.path.exists(f):
            continue
        k = np.load(f)
        if len(k) == 0:
            continue
        idx = np.rint((k[:, 0] - t0_ms) / STEP).astype(np.int64)
        ok = (idx >= 0) & (idx < T)
        idx, k = idx[ok], k[ok]
        O[idx, j], H[idx, j], L[idx, j], C[idx, j] = k[:, 1], k[:, 2], k[:, 3], k[:, 4]
FR = np.full((T, N), np.nan, np.float32); FI = np.zeros((T, N), np.float32)
FR[::B] = d["FR"]; FI[::B] = d["FI"]
month_idx = np.repeat(mi1, B)
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
np.savez("data/panel15m.npz", O=O, H=H, L=L, C=C, FR=FR, FI=FI, U=U, month_idx=month_idx,
         force_close=force_close, months=months, syms=syms)
# сверка с часовыми данными: закрытие 15m-бара в конце часа = закрытие часа
c1 = d["C"]
c15 = C[B - 1::B]
both = np.isfinite(c1) & np.isfinite(c15)
rel = np.abs(c15[both] / c1[both] - 1)
print("bars", T, "symbols", N, "close mismatch >0.1%:", (rel > 1e-3).mean())
