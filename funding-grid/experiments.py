import json, sys, time
import numpy as np
from run import run, metrics, fmt

EXPS = [
    ("base", "Как в идее: 0.5% → 5%, шаг 3%, без стопа", {}),
    ("step2", "Шаг 2%", dict(step_fix=0.02)),
    ("step5", "Шаг 5%", dict(step_fix=0.05)),
    ("stepvol", "Шаг 0.5 дневной волатильности", dict(step_fix=0.0, step_k=0.5)),
    ("long", "Только лонги", dict(side_mask=1)),
    ("short", "Только шорты", dict(side_mask=2)),
    ("flipclose", "Разворот фандинга → закрыть сразу", dict(exit_mode=0)),
    ("thr1", "Порог: фандинг по модулю > 0.01%", dict(thr=0.0001)),
    ("thr3", "Порог: фандинг по модулю > 0.03%", dict(thr=0.0003)),
    ("trail", "Сетка подтягивается за ценой", dict(trail=1)),
    ("sl2", "Стоп 2% эквити на актив", dict(sl_eq=0.02)),
    ("cap2", "Плечо ≤ 2x", dict(gross_cap=2.0)),
    ("sl2cap2", "Стоп 2% + плечо ≤ 2x", dict(sl_eq=0.02, gross_cap=2.0)),
    ("s5", "Размер /5: 0.1% → 1%", dict(lot_pct=0.001)),
    ("s10", "Размер /10: 0.05% → 0.5%", dict(lot_pct=0.0005)),
    ("fix", "Размер /5 + стоп 0.5% + плечо ≤ 1x", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0)),
    ("fixtrail", "то же, сетка подтягивается", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0, trail=1)),
    ("fixpathb", "то же, путь внутри свечи Б", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0, path_mode=1)),
    ("mn", "Нейтральный 20/20", dict(dir_mode=3, rank_n=20)),
    ("mnfix", "Нейтральный 20/20, /5 + стоп 0.5% + 1x", dict(dir_mode=3, rank_n=20, lot_pct=0.001, sl_eq=0.005, gross_cap=1.0)),
    ("pathb", "Как в идее, путь внутри свечи Б", dict(path_mode=1)),
    ("anti", "Контроль: против фандинга", dict(dir_mode=2)),
    ("allshort", "Контроль: всегда шорт", dict(dir_mode=-1)),
    ("alllong", "Контроль: всегда лонг", dict(dir_mode=1)),
    ("fixanti", "Контроль к /5 + стоп + 1x: против фандинга", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0, dir_mode=2)),
    ("fixshort", "Контроль к /5 + стоп + 1x: всегда шорт", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0, dir_mode=-1)),
    ("fixlong", "Контроль к /5 + стоп + 1x: всегда лонг", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0, dir_mode=1)),
]
STARTS = ["2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01", "2025-01-01"]

if __name__ == "__main__":
    only = sys.argv[1:] or None
    out = {}
    for key, name, kw in EXPS:
        if only and key not in only:
            continue
        for s in STARTS:
            t0 = time.time()
            r = run(start=s, **kw)
            m = metrics(r)
            st = r["st"]
            bpd = 24 * r["bph"]
            eq = r["eq"][st::bpd]
            m["curve"] = [None if not np.isfinite(x) else round(float(x), 5) for x in eq]
            m["curve_t0"] = str(r["hours"][st])
            g = r["gross"][st::bpd] / np.where(np.isfinite(r["eq"][st::bpd]), r["eq"][st::bpd], np.nan)
            m["gross_curve"] = [None if not np.isfinite(x) else round(float(x), 3) for x in g]
            ln = r["lng"][st::bpd] / r["eq"][st::bpd]; sh = r["sht"][st::bpd] / r["eq"][st::bpd]
            m["long_curve"] = [None if not np.isfinite(x) else round(float(x), 3) for x in ln]
            m["short_curve"] = [None if not np.isfinite(x) else round(float(x), 3) for x in sh]
            out[f"{key}|{s}"] = m
            print(fmt(f"{name} [{s[:4]}]", m), f"  ({time.time()-t0:.1f}s)", flush=True)
            print(f"{'':28s} worst: " + ", ".join(f"{a} {b*100:+.1f}%" for a, b in m["worst"][:6]), flush=True)
            print(f"{'':28s} counts: {m['counts']}  npos avg {m['npos_avg']:.0f} full grids avg {m['nfull_avg']:.1f} max {m['nfull_max']}", flush=True)
    tag = "_".join(only) if only else "all"
    json.dump(out, open(f"results_{tag}.json", "w"))
