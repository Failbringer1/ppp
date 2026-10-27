"""Сколько времени каждая валютная пара проходит правило 75% у брокера с наценкой m (panel_fx)."""
import numpy as np

p = np.load("data/panel_fx.npz")
syms, kind, CARRY = p["syms"], p["kind"], p["CARRY"]
fx = np.flatnonzero(kind == "fx")
print("Доля дней 2015-2026, когда пару можно держать: получаемый своп (|разница| - m) >= 0.75 * |разница|")
print(f"{'пара':8s} {'|разница| ср.':>14s} {'m=0 (фьючерсы)':>15s} {'m=1%':>7s} {'m=2%':>7s}  сторона")
rows = []
for j in fx:
    c = CARRY[:, j]
    c = c[np.isfinite(c)].astype(float)
    a = np.abs(c)
    sh = [np.mean((a > 0) & (a - m >= 0.75 * a)) for m in (0.0, 0.01, 0.02)]
    side = "лонг" if np.mean(c > 0) > 0.5 else "шорт"
    rows.append((sh[1], syms[j], a.mean(), sh, side))
for _, s, am, sh, side in sorted(rows, key=lambda r: -r[0]):
    print(f"{s:8s} {am*100:13.1f}% {sh[0]*100:14.0f}% {sh[1]*100:6.0f}% {sh[2]*100:6.0f}%  {side}")
