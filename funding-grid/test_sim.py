import numpy as np
from sim import simulate
from run import BASE, ORDER

def mk(prices, fr_val, n=1):
    T = len(prices)
    p = np.asarray(prices, np.float32)
    O = np.empty((T, n), np.float32); C = np.empty((T, n), np.float32)
    O[:, 0] = np.r_[p[0], p[:-1]]; C[:, 0] = p
    H = np.maximum(O, C); L = np.minimum(O, C)
    FR = np.full((T, n), np.nan, np.float32); FI = np.zeros((T, n), np.float32)
    FR[::8, 0] = fr_val; FI[::8, 0] = 8
    U = np.ones((1, n), bool); mi = np.zeros(T, np.int32); FC = np.zeros((T, n), bool)
    return O, H, L, C, FR, FI, U, mi, FC

def go(prices, fr, **kw):
    cfg = dict(BASE); cfg.update(kw); cfg["vol_win"] = 24
    O, H, L, C, FR, FI, U, mi, FC = mk(prices, fr)
    r = simulate(O, H, L, C, FR, FI, U, mi, FC, 0, *[cfg[k] for k in ORDER])
    eq, eqw, gross, lng, sht, npos, nfull, fund, real, fee, unreal, cnt, pa, blown, dbg = r
    ident = 1 + fund.sum() + real.sum() - fee.sum() + unreal.sum()
    last = eq[np.isfinite(eq)][-1]
    print(f"final {last:.6f} ident {ident:.6f} fund {fund} real {real} fee {fee} unreal {unreal} cnt {cnt} blown {blown}")
    assert abs(last - ident) < 1e-9, "accounting identity broken"
    return r

t = np.arange(24 * 60)
print("1) синус, фандинг -0.01% (платят лонгам)")
go(100 + 10 * np.sin(t / 24), -0.0001)
print("2) падение 100 -> 50, лонг, без стопа")
r = go(np.linspace(100, 50, len(t)), -0.0001)
print("   ожидаемый убыток ~ сумма лотов: ", sum(0.005 * (50 / (100 * 0.97 ** k) - 1) for k in range(10)))
print("3) рост 100 -> 150, фандинг +0.01% (платят шортам), без стопа")
go(np.linspace(100, 150, len(t)), 0.0001)
print("4) то же со стопом 1% эквити")
go(np.linspace(100, 150, len(t)), 0.0001, sl_eq=0.01)
print("5) разворот фандинга: exit_mode 0")
p = 100 + 10 * np.sin(t / 24)
O, H, L, C, FR, FI, U, mi, FC = mk(p, -0.0001)
FR[24*30::8, 0] = 0.0001
cfg = dict(BASE); cfg["vol_win"] = 24; cfg["exit_mode"] = 0
r = simulate(O, H, L, C, FR, FI, U, mi, FC, 0, *[cfg[k] for k in ORDER])
print("   cnt", r[11], "fund", r[7], "eq", r[0][-1])
