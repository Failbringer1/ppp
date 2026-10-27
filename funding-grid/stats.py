"""Статистика фандинга по юниверсу и «доходность платной стороны» без сетки.

Для каждого расчёта фандинга по активу из топ-100 берём сторону, которой платят
(шорт при r>0, лонг при r<0), и смотрим, что дальше даёт цена и сколько набегает фандинга.
"""
import numpy as np
from run import panel

P = panel()
O, C, FR, FI, U, mi = P["O"], P["C"], P["FR"], P["FI"], P["U"], P["month_idx"]
hours = P["hours"]
T, N = C.shape
st = int(np.searchsorted(hours, np.datetime64("2021-01-01")))

tt, jj = np.nonzero(np.isfinite(FR))
m = (tt >= st) & U[mi[tt], jj] & np.isfinite(O[tt, jj])
tt, jj = tt[m], jj[m]
r = FR[tt, jj].astype(np.float64)
ih = np.where(FI[tt, jj] > 0, FI[tt, jj], 8.0)
r8 = r * 8 / ih
yr = hours[tt].astype("datetime64[Y]").astype(int) + 1970

print("=== Доля времени по знаку фандинга (8ч-эквивалент, вес = длина интервала), топ-100 ===")
base = np.abs(r8 - 0.0001) < 2e-7
for y in [None] + sorted(set(yr)):
    s = slice(None) if y is None else (yr == y)
    rr, bb, w = r8[s], base[s], ih[s]
    wm = lambda x: np.sum(w * x) / np.sum(w)
    print(f"{'всего' if y is None else y}: r<0: {wm(rr < 0)*100:5.1f}%  "
          f"ровно +0.01%: {wm(bb)*100:5.1f}%  >0.01%: {wm((rr > 0.0001) & ~bb)*100:5.1f}%  "
          f"0..0.01%: {wm((rr > 0) & (rr < 0.0001) & ~bb)*100:5.1f}%  "
          f"медиана r: {np.median(rr)*100:+.4f}%  ср.r: {wm(rr)*100:+.4f}%")

# Доля времени, когда платят шортам, в среднем по активам
print()

# Форвардная доходность платной стороны
cum = np.nancumsum(np.nan_to_num(FR.astype(np.float64)), axis=0)   # кумулятивный фандинг
Cf = C.astype(np.float64)
# равновзвешенный индекс юниверса (для бета-нейтрального сравнения): средняя часовая доходность
ret1 = np.full((T, N), np.nan)
ret1[1:] = Cf[1:] / Cf[:-1] - 1
inu = U[mi]  # [T, N]
ew = np.nanmean(np.where(inu, ret1, np.nan), axis=1)
ew = np.nan_to_num(ew)
ew_idx = np.cumprod(1 + ew)

buckets = [(-1, -0.001), (-0.001, -0.0003), (-0.0003, -0.0001), (-0.0001, -1e-12),
           (1e-12, 0.0001 - 2e-7), (0.0001 - 2e-7, 0.0001 + 2e-7), (0.0001 + 2e-7, 0.0003),
           (0.0003, 0.001), (0.001, 1)]
names = ["< -0.1%", "-0.1..-0.03%", "-0.03..-0.01%", "-0.01..0%", "0..0.01%", "=0.01% (база)",
         "0.01..0.03%", "0.03..0.1%", "> 0.1%"]
for hz in [24, 24 * 7, 24 * 30]:
    t2 = tt + hz
    ok = t2 < T
    a, b, rr = tt[ok], jj[ok], r8[ok]
    p0 = O[a, b].astype(np.float64); p1 = Cf[t2[ok] - 1, b]
    good = np.isfinite(p1) & (p0 > 0)
    a, b, rr, p0, p1 = a[good], b[good], rr[good], p0[good], p1[good]
    d = np.where(rr > 0, -1.0, 1.0)
    pr = d * (p1 / p0 - 1)                                  # цена для платной стороны
    fu = -d * (cum[a + hz - 1, b] - cum[a, b])              # фандинг после входа (расчёт в момент входа не наш)
    mk = ew_idx[a + hz - 1] / ew_idx[a - 1] - 1
    ex = d * ((p1 / p0 - 1) - mk)                           # цена сверх рынка
    print(f"=== Горизонт {hz//24} дн: платная сторона (шорт при r>0, лонг при r<0), без сетки ===")
    print(f"{'фандинг 8ч':>15s} {'n':>8s} {'цена ср':>9s} {'цена мед':>9s} {'фандинг':>8s} {'итого':>8s} "
          f"{'сверх рынка':>11s} {'доля +':>7s}")
    for (lo, hi), nm in zip(buckets, names):
        s = (rr > lo) & (rr <= hi)
        if s.sum() < 50:
            continue
        tot = pr[s] + fu[s]
        print(f"{nm:>15s} {s.sum():8d} {pr[s].mean()*100:+8.2f}% {np.median(pr[s])*100:+8.2f}% "
              f"{fu[s].mean()*100:+7.2f}% {tot.mean()*100:+7.2f}% {ex[s].mean()*100:+10.2f}% "
              f"{np.mean(tot > 0)*100:6.1f}%")
