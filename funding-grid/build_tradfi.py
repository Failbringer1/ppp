"""Панель по TradFi-перпетуалам Binance (металлы, сырьё, индексы и акции США), 1h, с декабря 2025.

Все инструменты торгуются всегда, когда есть данные (юниверс не отбирается по обороту).
Токенизированное золото (PAXG, XAUT), плечевые ETF (TQQQ, SQQQ) и акции криптокомпаний
(COIN, HOOD, MSTR, CRCL) не берём.
"""
import numpy as np

SYMS = ["XAUUSDT", "XAGUSDT", "XPTUSDT", "XPDUSDT", "COPPERUSDT", "NATGASUSDT",
        "SPYUSDT", "QQQUSDT", "AAPLUSDT", "AMZNUSDT", "GOOGLUSDT", "METAUSDT", "MSFTUSDT", "NVDAUSDT", "TSLAUSDT"]
T0 = np.datetime64("2025-12-01T00:00")
T1 = np.datetime64("2026-10-01T00:00")
T = int((T1 - T0) / np.timedelta64(1, "h"))
N = len(SYMS)
t0_ms = int(T0.astype("datetime64[ms]").astype(np.int64))
O = np.full((T, N), np.nan, np.float32); H = np.full((T, N), np.nan, np.float32)
L = np.full((T, N), np.nan, np.float32); C = np.full((T, N), np.nan, np.float32)
FR = np.full((T, N), np.nan, np.float32); FI = np.zeros((T, N), np.float32)
for j, s in enumerate(SYMS):
    d = np.load(f"data/raw/{s}.npz")
    k, f = d["k"], d["f"]
    idx = np.rint((k[:, 0] - t0_ms) / 3_600_000).astype(np.int64)
    m = (idx >= 0) & (idx < T)
    O[idx[m], j], H[idx[m], j], L[idx[m], j], C[idx[m], j] = k[m, 1], k[m, 2], k[m, 3], k[m, 4]
    fi = np.rint((f[:, 0] - t0_ms) / 3_600_000).astype(np.int64)
    m = (fi >= 0) & (fi < T)
    FR[fi[m], j] = f[m, 2]; FI[fi[m], j] = f[m, 1]
hours = T0 + np.arange(T) * np.timedelta64(1, "h")
month = hours.astype("datetime64[M]")
months = np.unique(month)
month_idx = np.searchsorted(months, month).astype(np.int32)
U = np.ones((len(months), N), bool)
valid = np.isfinite(C)
force_close = np.zeros((T, N), bool)
for j in range(N):
    v = np.flatnonzero(valid[:, j])
    gaps = np.diff(v)
    force_close[v[:-1][gaps > 24], j] = True
    if v[-1] < T - 1:
        force_close[v[-1], j] = True
np.savez("data/panel_tradfi.npz", O=O, H=H, L=L, C=C, FR=FR, FI=FI, U=U, month_idx=month_idx,
         force_close=force_close, months=months.astype(str), syms=np.array(SYMS),
         hours=hours.astype("datetime64[m]").astype(np.int64))
print("bars", T, "symbols", N, "gaps>24h:", int(force_close.sum()))
