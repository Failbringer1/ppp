"""Бэктест: сетка по 100 активам, направление = сторона, которой платят фандинг.

Модель (бары 1h/15m/5m, путь внутри бара: бычий O->L->H->C, медвежий O->H->L->C):
  * Вход: базовый лот lot_pct от эквити по рынку, направление по знаку сглаженного фандинга
    (фандинг > thr -> шорт, < -thr -> лонг).
  * Доборы: лимитки через каждые step (геометрически) против позиции, пока лотов < max_lots.
  * Частичный выход на отскоке: каждый добор k закрывается лимиткой на уровне k-1.
    Базовый лот держим ради фандинга.
  * Разворот фандинга / выпадение из топ-100:
      exit_mode=0 - закрыть всё по рынку;
      exit_mode=1 - не доливать, базовый лот закрыть в безубыток (уровень входа),
                    по таймауту unwind_h - по рынку.
  * Опционально: стоп по убытку на актив (доля эквити), лимит общей экспозиции.
  * trail=1 - якорь сетки подтягивается за ценой, пока открыт только базовый лот.
  * Фандинг начисляется на позицию в момент расчёта по цене открытия часа.
  * Комиссии: maker на лимитки, taker+проскальзывание на рыночные.
  * Делистинг / разрыв данных >24ч: принудительное закрытие по последней цене.
  * Ликвидация портфеля: эквити (в худшей точке бара) < mmr * gross.
  * path_mode=1 - альтернативный путь внутри бара: для лонга всегда O->H->L->C, для шорта O->L->H->C
    (проверка чувствительности к допущению о пути цены внутри бара).
  * fund_tp=1 - фандинг, полученный лотом, засчитывается в его прибыль: тейк на откате (и выход базы
    в безубыток) сдвигается на накопленный фандинг.
  * drift_k > 0 - фильтр одностороннего движения: если за drift_win баров цена ушла против стороны,
    которой платят, вход и доборы разрешены, только если годовой фандинг >= drift_k * годовой темп
    этого движения; иначе позиция переходит в режим выхода, как при развороте фандинга.
  * FS - ставка, которую платит шорт в момент расчёта (у перпов = -FR, у брокера со своей наценкой
    платить могут обе стороны). FAIR - справедливый годовой перенос для лонга (разница ставок, только
    для валют; NaN - правило не применяется). fair_k > 0 - валютное правило: входим, только если
    получаемый своп >= fair_k * разница ставок.
  * SPR (T, N) - половина спреда по активу и бару, добавляется к комиссии каждой сделки (форма (1, 1) - нет).
  * bph - баров в часе (1 для 1h, 4 для 15m, 12 для 5m); интервалы в параметрах - в барах.
  * dir_mode: 0 - по фандингу, 1/-1 - всегда лонг/шорт, 2 - против фандинга (контроль),
              3 - рыночно-нейтральный кросс-секционный вариант: лонг rank_n монет с самым
              отрицательным фандингом (< 0), шорт rank_n с самым высоким (> thr); выпала из
              списка - выход как при развороте.
"""
import numpy as np
from numba import njit

MAXL = 64


@njit(cache=True)
def _lvl(anchor, step, k, d):
    # цена уровня k: для лонга ниже якоря, для шорта выше
    if d > 0:
        return anchor * (1.0 - step) ** k
    return anchor * (1.0 + step) ** k


@njit(cache=True)
def simulate(O, H, L, C, FR, FS, FI, FAIR, SPR, U, month_idx, force_close, start_t,
             lot_pct, max_lots, step_fix, step_k, step_min, step_max,
             thr, alpha, exit_mode, unwind_h, sl_eq, cooldown_h, gross_cap,
             trail, side_mask, maker, taker, slip, mmr, vol_win, dir_mode, rank_n, path_mode,
             fund_tp, drift_k, drift_win, fair_k, bph=1, dbg_j=-1):
    T, N = O.shape
    E0 = 1.0
    cash = E0
    d = np.zeros(N, np.int64)            # направление позиции
    nl = np.zeros(N, np.int64)           # число открытых лотов (0 - нет позиции)
    lp = np.zeros((N, MAXL))             # цены лотов
    lq = np.zeros((N, MAXL))             # количества лотов
    lf = np.zeros((N, MAXL))             # фандинг, полученный каждым лотом (минус - заплачен)
    Q = np.zeros(N)                      # суммарное кол-во
    QP = np.zeros(N)                     # sum q*p
    anchor = np.zeros(N)
    stp = np.zeros(N)
    lotn = np.zeros(N)                   # номинал лота в USDT (фиксируется на входе)
    sig = np.zeros(N)
    sigS = np.zeros(N)                   # сглаженная ставка, которую платит шорт (для перпов = -sig)
    sig_ok = np.zeros(N, np.bool_)
    unw = np.zeros(N, np.bool_)
    unw_t = np.zeros(N, np.int64)
    cool = np.zeros(N, np.int64)
    lastp = np.full(N, np.nan)
    rfav = np.zeros(N, np.int64)
    tmp = np.zeros(N)
    # скользящая волатильность часовых лог-доходностей (для шага от волатильности)
    s1 = np.zeros(N); s2 = np.zeros(N); sn = np.zeros(N)
    ring = np.zeros((vol_win, N)); ring_ok = np.zeros((vol_win, N), np.bool_)

    dbg = np.zeros((T, 6))
    eq = np.full(T, np.nan); eq_worst = np.full(T, np.nan)
    gross_c = np.zeros(T); long_c = np.zeros(T); short_c = np.zeros(T)
    n_pos_c = np.zeros(T); n_full_c = np.zeros(T)
    # бухгалтерия: [лонг, шорт]
    fund = np.zeros(2); real = np.zeros(2); fee = np.zeros(2)
    cnt = np.zeros(8)  # входы, доборы, тейки, выходы-разворот, стопы, делистинг, таймаут, бу-выход базы
    pnl_asset = np.zeros(N)
    blown_t = -1

    for t in range(start_t, T):
        mi = month_idx[t]
        # --- волатильность ---
        r = t % vol_win
        for j in range(N):
            if ring_ok[r, j]:
                x = ring[r, j]; s1[j] -= x; s2[j] -= x * x; sn[j] -= 1
                ring_ok[r, j] = False
            if t > 0 and np.isfinite(C[t - 1, j]) and np.isfinite(O[t, j]) and np.isfinite(C[t, j]):
                x = np.log(C[t, j] / C[t - 1, j])
                ring[r, j] = x; ring_ok[r, j] = True
                s1[j] += x; s2[j] += x * x; sn[j] += 1
        # --- фандинг ---
        for j in range(N):
            f = FR[t, j]
            if np.isfinite(f):
                fs = FS[t, j]
                if nl[j] > 0 and np.isfinite(O[t, j]):
                    rate = f if d[j] > 0 else fs
                    pay = Q[j] * O[t, j] * rate
                    cash -= pay
                    for kk in range(nl[j]):
                        lf[j, kk] -= lq[j, kk] * O[t, j] * rate
                    k = 0 if d[j] > 0 else 1
                    fund[k] -= pay
                    pnl_asset[j] -= pay
                ih = FI[t, j] if FI[t, j] > 0 else 8.0
                f8 = f * 8.0 / ih
                fs8 = fs * 8.0 / ih
                if sig_ok[j]:
                    sig[j] = alpha * f8 + (1 - alpha) * sig[j]
                    sigS[j] = alpha * fs8 + (1 - alpha) * sigS[j]
                else:
                    sig[j] = f8; sigS[j] = fs8; sig_ok[j] = True
        # --- эквити и gross на открытии ---
        E = cash
        gross = 0.0
        for j in range(N):
            if nl[j] > 0:
                p = O[t, j] if np.isfinite(O[t, j]) else lastp[j]
                E += d[j] * (Q[j] * p - QP[j])
                gross += Q[j] * p
        if E <= 0:
            blown_t = t
            break
        if dir_mode == 3:
            for j in range(N):
                rfav[j] = 0
                tmp[j] = sig[j] if (sig_ok[j] and U[mi, j] and np.isfinite(O[t, j])) else np.nan
            order = np.argsort(tmp)          # NaN уходят в конец
            k = 0
            for ii in range(N):
                j = order[ii]
                if not np.isfinite(tmp[j]) or tmp[j] >= 0 or k >= rank_n:
                    break
                rfav[j] = 1; k += 1
            k = 0
            for ii in range(N - 1, -1, -1):
                j = order[ii]
                if not np.isfinite(tmp[j]):
                    continue
                if tmp[j] <= thr or k >= rank_n:
                    break
                rfav[j] = -1; k += 1

        for j in range(N):
            o = O[t, j]
            if not np.isfinite(o):
                continue
            h = H[t, j]; l = L[t, j]; c = C[t, j]
            lastp[j] = c
            # издержка на сделку сверх комиссии (половина спреда), доля номинала; SPR формы (1, 1) - нет
            cs = SPR[t, j] if SPR.shape[0] > 1 else 0.0
            fav = 0
            if sig_ok[j]:
                # сторона, которой платят: что получает лонг (-sig) и что получает шорт (-sigS)
                rl = -sig[j]; rs = -sigS[j]
                if rs > thr and rs >= rl:
                    fav = -1
                elif rl > thr and rl > rs:
                    fav = 1
            if dir_mode == 1 or dir_mode == -1:
                fav = dir_mode if sig_ok[j] else 0
            elif dir_mode == 2:
                fav = -fav
            elif dir_mode == 3:
                fav = rfav[j]
            if fav != 0 and (side_mask & (1 if fav > 0 else 2)) == 0:
                fav = 0
            inu = U[mi, j]
            # фильтр одностороннего движения: если цена за drift_win ушла против стороны, которой платят,
            # фандинг (в годовых) должен покрывать хотя бы drift_k этого движения (в годовых)
            blocked = False
            if drift_k > 0 and fav != 0:
                tb = t - 1 - drift_win
                if tb < 0 or not (np.isfinite(C[t - 1, j]) and np.isfinite(C[tb, j])):
                    blocked = True
                else:
                    adv = -fav * np.log(C[t - 1, j] / C[tb, j]) * (8760.0 * bph / drift_win)
                    recv = ((-sig[j]) if fav > 0 else (-sigS[j])) * 1095.0 if sig_ok[j] else 0.0
                    if adv > 0 and recv < drift_k * adv:
                        blocked = True
            # валюты: своп, который мы получаем, должен быть не меньше fair_k от разницы ставок (FAIR - годовой
            # справедливый перенос для лонга: ставка базовой валюты минус ставка котируемой)
            if fair_k > 0 and fav != 0 and FAIR.shape[0] > 1 and np.isfinite(FAIR[t, j]):
                fair_recv = fav * FAIR[t, j]
                if fair_recv > 0:
                    recv = ((-sig[j]) if fav > 0 else (-sigS[j])) * 1095.0 if sig_ok[j] else 0.0
                    if recv < fair_k * fair_recv:
                        blocked = True
            side = 0
            # ---------- управление открытой позицией на открытии бара ----------
            if nl[j] > 0:
                side = 0 if d[j] > 0 else 1
                against = (fav == -d[j]) or (not inu) or (dir_mode == 3 and fav != d[j]) \
                    or (blocked and fav == d[j])
                if against:
                    if exit_mode == 0:
                        # закрыть всё по рынку
                        px = o * (1 - d[j] * slip)
                        pr = d[j] * (Q[j] * px - QP[j]); fe = Q[j] * px * (taker + cs)
                        cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                        gross -= Q[j] * o
                        nl[j] = 0; Q[j] = 0; QP[j] = 0; unw[j] = False
                        cnt[3] += 1
                    elif not unw[j]:
                        unw[j] = True; unw_t[j] = t
                elif unw[j] and fav == d[j]:
                    unw[j] = False
                if nl[j] > 0 and unw[j] and t - unw_t[j] >= unwind_h:
                    px = o * (1 - d[j] * slip)
                    pr = d[j] * (Q[j] * px - QP[j]); fe = Q[j] * px * (taker + cs)
                    cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                    gross -= Q[j] * o
                    nl[j] = 0; Q[j] = 0; QP[j] = 0; unw[j] = False
                    cnt[6] += 1
            # ---------- новый вход ----------
            if nl[j] == 0 and inu and fav != 0 and not blocked and t >= cool[j] and not force_close[t, j]:
                ln = lot_pct * E
                if gross + ln <= gross_cap * E:
                    if step_fix > 0:
                        st = step_fix
                    else:
                        if sn[j] < vol_win // 2:
                            continue
                        m1 = s1[j] / sn[j]
                        sd = np.sqrt(max(s2[j] / sn[j] - m1 * m1, 0.0)) * np.sqrt(24.0 * bph)
                        st = min(max(step_k * sd, step_min), step_max)
                    d[j] = fav
                    px = o * (1 + fav * slip)
                    q = ln / px
                    fe = ln * (taker + cs)
                    side = 0 if fav > 0 else 1
                    cash -= fe; fee[side] += fe; pnl_asset[j] -= fe
                    lp[j, 0] = px; lq[j, 0] = q; lf[j, 0] = 0.0; Q[j] = q; QP[j] = q * px
                    nl[j] = 1; anchor[j] = o; stp[j] = st; lotn[j] = ln; unw[j] = False
                    gross += ln
                    cnt[0] += 1
            if nl[j] == 0:
                continue
            side = 0 if d[j] > 0 else 1
            dj = d[j]
            # ---------- путь внутри бара ----------
            if path_mode == 1:
                # альтернативный путь: сначала в сторону позиции, потом против
                if dj > 0:
                    p1 = h; p2 = l
                else:
                    p1 = l; p2 = h
            elif c >= o:
                p1 = l; p2 = h
            else:
                p1 = h; p2 = l
            path = (o, p1, p2, c)
            cur = o
            closed = False
            for sgi in range(1, 4):
                b = path[sgi]
                if dj * (b - cur) < 0:
                    # движение против позиции: доборы и стоп
                    while True:
                        can_add = (not unw[j]) and nl[j] < max_lots and nl[j] < MAXL \
                            and gross + lotn[j] <= gross_cap * E
                        nxt = _lvl(anchor[j], stp[j], nl[j], dj) if can_add else np.nan
                        sp = np.nan
                        if sl_eq > 0:
                            sp = (QP[j] - dj * sl_eq * E) / Q[j]
                        # что достигнуто первым (для лонга - более высокая цена)
                        hit_add = can_add and dj * (b - nxt) <= 0
                        hit_sp = sl_eq > 0 and sp > 0 and dj * (b - sp) <= 0
                        if hit_sp and (not hit_add or dj * (sp - nxt) >= 0):
                            px = sp if dj * (cur - sp) > 0 else cur
                            px = px * (1 - dj * slip)
                            pr = dj * (Q[j] * px - QP[j]); fe = Q[j] * px * (taker + cs)
                            cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                            gross -= Q[j] * px
                            nl[j] = 0; Q[j] = 0; QP[j] = 0; unw[j] = False
                            cool[j] = t + cooldown_h
                            cnt[4] += 1
                            closed = True
                            break
                        if hit_add:
                            late = dj * (cur - nxt) < 0
                            px = cur if late else nxt
                            q = lotn[j] / px
                            fe = lotn[j] * ((taker if late else maker) + cs)
                            cash -= fe; fee[side] += fe; pnl_asset[j] -= fe
                            k = nl[j]
                            lp[j, k] = px; lq[j, k] = q; lf[j, k] = 0.0
                            Q[j] += q; QP[j] += q * px
                            nl[j] = k + 1
                            gross += lotn[j]
                            cur = px
                            cnt[1] += 1
                            continue
                        break
                    if closed:
                        break
                else:
                    # движение в сторону позиции: тейки доборов, якорь, бу-выход базы
                    while nl[j] >= 2:
                        k = nl[j] - 1
                        tp = _lvl(anchor[j], stp[j], k - 1, dj)
                        if fund_tp:
                            # полученный лотом фандинг засчитывается в прибыль: тейк ближе
                            tp -= dj * lf[j, k] / lq[j, k]
                        if dj * (b - tp) >= 0:
                            px = tp if dj * (tp - cur) >= 0 else cur
                            pr = dj * lq[j, k] * (px - lp[j, k]); fe = lq[j, k] * px * (maker + cs)
                            cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                            gross -= lq[j, k] * px
                            Q[j] -= lq[j, k]; QP[j] -= lq[j, k] * lp[j, k]
                            nl[j] = k
                            cur = px
                            cnt[2] += 1
                        else:
                            break
                    if nl[j] == 1:
                        if unw[j]:
                            tp = lp[j, 0]
                            if fund_tp:
                                tp -= dj * lf[j, 0] / lq[j, 0]
                            if dj * (b - tp) >= 0:
                                px = tp if dj * (tp - cur) >= 0 else cur
                                pr = dj * Q[j] * (px - QP[j] / Q[j]); fe = Q[j] * px * (maker + cs)
                                cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                                gross -= Q[j] * px
                                nl[j] = 0; Q[j] = 0; QP[j] = 0; unw[j] = False
                                cnt[7] += 1
                                closed = True
                                break
                        elif trail:
                            if dj * (b - anchor[j]) > 0:
                                anchor[j] = b
                cur = b
            # ---------- делистинг / разрыв данных ----------
            if nl[j] > 0 and force_close[t, j]:
                px = c * (1 - dj * slip)
                pr = dj * (Q[j] * px - QP[j]); fe = Q[j] * px * (taker + cs)
                cash += pr - fe; real[side] += pr; fee[side] += fe; pnl_asset[j] += pr - fe
                nl[j] = 0; Q[j] = 0; QP[j] = 0; unw[j] = False
                cnt[5] += 1

        # --- закрытие бара: эквити, худшая точка, ликвидация ---
        Ec = cash; Ew = cash; g = 0.0; lg = 0.0; sh = 0.0; npos = 0; nfull = 0
        for j in range(N):
            if nl[j] > 0:
                p = C[t, j] if np.isfinite(C[t, j]) else lastp[j]
                if np.isfinite(C[t, j]):
                    pw = L[t, j] if d[j] > 0 else H[t, j]
                else:
                    pw = p
                Ec += d[j] * (Q[j] * p - QP[j])
                Ew += d[j] * (Q[j] * pw - QP[j])
                g += Q[j] * p
                if d[j] > 0:
                    lg += Q[j] * p
                else:
                    sh += Q[j] * p
                npos += 1
                if nl[j] >= max_lots:
                    nfull += 1
        if dbg_j >= 0:
            dbg[t, 0] = d[dbg_j]; dbg[t, 1] = nl[dbg_j]; dbg[t, 2] = Q[dbg_j]; dbg[t, 3] = QP[dbg_j]
            dbg[t, 4] = pnl_asset[dbg_j]; dbg[t, 5] = anchor[dbg_j]
        eq[t] = Ec; eq_worst[t] = Ew; gross_c[t] = g; long_c[t] = lg; short_c[t] = sh
        n_pos_c[t] = npos; n_full_c[t] = nfull
        if Ew < mmr * g:
            blown_t = t
            eq[t] = max(Ew, 0.0)
            break

    # нереализованное на конец
    unreal = np.zeros(2)
    for j in range(N):
        if nl[j] > 0:
            p = lastp[j]
            if not np.isfinite(p):
                p = QP[j] / Q[j]
            u = d[j] * (Q[j] * p - QP[j])
            unreal[0 if d[j] > 0 else 1] += u
            pnl_asset[j] += u
    return eq, eq_worst, gross_c, long_c, short_c, n_pos_c, n_full_c, fund, real, fee, unreal, cnt, pnl_asset, blown_t, dbg
