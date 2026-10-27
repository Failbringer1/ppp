"""Ориентир: сбор фандинга без направленного риска (cash & carry: шорт перпа + лонг спота).

Грубая оценка: раз в неделю берём K монет из топ-100 с максимальным фандингом за прошлые 7 дней
(только положительный), держим неделю, получаем их фандинг за следующие 7 дней.
Смена состава стоит 0.3% номинала (спот 0.1% x2 + перп 0.05% x2); держим монету,
пока она в топ-2K и фандинг > 0. Базис и наличие спота
не моделируются. Доходность указана на номинал одной ноги; на капитал (спот + маржа перпа
при 3x) она примерно в 1.33 раза ниже.
"""
import numpy as np
from run import panel

P = panel()
FR, U, mi, hours = P["FR"], P["U"], P["month_idx"], P["hours"]
T, N = FR.shape
f = np.nan_to_num(FR.astype(np.float64))
cs = np.vstack([np.zeros((1, N)), np.cumsum(f, axis=0)])   # cs[t] = сумма фандинга по часам < t
st = int(np.searchsorted(hours, np.datetime64("2021-01-01")))
for K in [10, 20, 50]:
    held = np.zeros(N, bool)
    daily = []; days = []; gross_l = []; cost_l = []
    W = 24 * 7
    for t in range(st, T - W, W):
        past = cs[t] - cs[t - W]
        inu = U[mi[t]]
        cand = np.where(inu & (past > 0), past, -np.inf)
        order = np.lexsort((~held, -cand))   # при равных ставках приоритет у уже купленных
        rank = np.empty(N, np.int64); rank[order] = np.arange(N)
        # гистерезис: держим, пока монета в топ-2K и фандинг > 0, освободившиеся места добираем лучшими
        keep = held & (rank < 2 * K) & np.isfinite(cand)
        new = keep.copy()
        for j in order:
            if new.sum() >= K or not np.isfinite(cand[j]):
                break
            new[j] = True
        top = np.flatnonzero(new)
        if len(top) == 0:
            daily.append(0.0); gross_l.append(0.0); cost_l.append(0.0); days.append(hours[t]); held = new; continue
        nxt = cs[t + W] - cs[t]
        changed = (new & ~held).sum()
        cost = changed / len(top) * 0.003
        daily.append(nxt[top].mean() - cost); gross_l.append(nxt[top].mean()); cost_l.append(cost)
        days.append(hours[t]); held = new
    daily = np.array(daily); yrs = np.array(days).astype("datetime64[Y]")
    s = " ".join(f"{str(y)}: {daily[yrs == y].sum()*100:5.1f}%" for y in np.unique(yrs))
    print(f"K={K:3d}: средн. {daily.mean()*52*100:5.1f}% годовых на номинал "
          f"(фандинг {np.mean(gross_l)*52*100:.1f}%, ротация -{np.mean(cost_l)*52*100:.1f}%) | {s}")
