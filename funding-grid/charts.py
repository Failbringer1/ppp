"""Графики для отчёта (PNG)."""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from run import run, panel

SURF, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
OUT = "report"
os.makedirs(OUT, exist_ok=True)
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.facecolor": SURF, "figure.facecolor": SURF,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "axes.spines.top": False, "axes.spines.right": False, "axes.titlecolor": INK,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
})


def curve(r):
    st = r["st"]; eq = r["eq"][st:].copy(); h = r["hours"][st:]
    end = r["blown"] - st if r["blown"] >= 0 else len(eq) - 1
    hh, ee = h[:end + 1:24], eq[:end + 1:24]
    if r["blown"] >= 0:
        # дорисовываем обвал до точки ликвидации
        hh = np.r_[hh, h[end - 48:end + 1]]; ee = np.r_[ee, np.maximum(np.nan_to_num(eq[end - 48:end + 1], nan=0.012), 0.012)]
        o = np.argsort(hh, kind="stable"); hh, ee = hh[o], ee[o]
    return hh, ee, (h[end], 0.012) if r["blown"] >= 0 else None


# 1. Эквити с января 2022: база, размер /5, размер /5 + стоп + лимит плеча
runs = [
    ("Как в идее: 0.5% → 5%, без стопа", dict(), S1),
    ("Размер /5: 0.1% → 1%", dict(lot_pct=0.001), S2),
    ("Размер /5 + стоп 0.5% на актив + плечо ≤ 1x", dict(lot_pct=0.001, sl_eq=0.005, gross_cap=1.0), S3),
]
fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
for name, kw, col in runs:
    r = run(start="2022-01-01", **kw)
    h, e, blown = curve(r)
    ax.plot(h.astype("datetime64[ms]").astype(object), np.maximum(e, 1e-3), color=col, lw=1.5,
            solid_capstyle="round", label=name)
    if blown:
        bx = blown[0].astype("datetime64[ms]").astype(object)
        ax.plot([bx], [0.012], marker="X", ms=8, color=col, mec=SURF, mew=1.5, zorder=5)
        ax.annotate(f"ликвидация {str(blown[0])[:10]}", (bx, 0.012), xytext=(8, 0), textcoords="offset points",
                    fontsize=8, color=INK2, va="center")
    else:
        ax.annotate(f"{e[-1]:.2f}", (h[-1].astype("datetime64[ms]").astype(object), e[-1]), xytext=(4, 0),
                    textcoords="offset points", fontsize=8, color=INK2, va="center")
ax.set_yscale("log")
ax.set_ylim(0.01, 8)
ax.set_yticks([0.01, 0.03, 0.1, 0.3, 1, 3])
ax.get_yaxis().set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
ax.axhline(1, color=AXIS, lw=0.8)
ax.xaxis.set_major_locator(mdates.YearLocator()); ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax.set_title("Эквити с 1 января 2022, капитал = 1 (лог. шкала)")
ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=INK2)
fig.tight_layout()
fig.savefig(f"{OUT}/equity_2022.png")
plt.close(fig)

# 2. Экспозиция базовой версии в 2022: лонг/шорт в долях эквити
r = run(start="2022-01-01")
st = r["st"]; end = r["blown"]
h = r["hours"][st:end:6].astype("datetime64[ms]").astype(object)
eq = r["eq"][st:end:6]
fig, ax = plt.subplots(figsize=(8, 3.2), dpi=150)
ax.plot(h, r["sht"][st:end:6] / eq, color=S2, lw=1.2, label="шорт, × эквити")
ax.plot(h, r["lng"][st:end:6] / eq, color=S1, lw=1.2, label="лонг, × эквити")
ax.set_ylim(0, 10)
RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
ax.xaxis.set_major_locator(mdates.MonthLocator())
ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: RU[mdates.num2date(v).month - 1]))
ax.set_title("Экспозиция версии «как в идее», янв–май 2022 (шкала обрезана на 10x)")
ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=INK2)
fig.tight_layout()
fig.savefig(f"{OUT}/exposure_2022.png")
plt.close(fig)

# 3. MYX: шорт «по фандингу» во время сквиза
P = panel()
j = list(P["syms"]).index("MYXUSDT")
hours = P["hours"]
t0 = np.searchsorted(hours, np.datetime64("2025-07-25")); t1 = np.searchsorted(hours, np.datetime64("2025-09-20"))
fig, ax = plt.subplots(figsize=(8, 3.2), dpi=150)
hh = hours[t0:t1].astype("datetime64[ms]").astype(object)
ax.plot(hh, P["C"][t0:t1, j], color=S1, lw=1.2)
ax.set_yscale("log")
ax.get_yaxis().set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
ax.axhline(0.1166, color=AXIS, lw=0.8)
ax.annotate("шорт-сетка заполнена, средняя цена 0.117", (hh[30], 0.1166), xytext=(0, 6), textcoords="offset points",
            fontsize=8, color=INK2)
ax.annotate("максимум 17.8 (×150)", (hh[-270], 17.8), xytext=(-8, 0), textcoords="offset points",
            fontsize=8, color=INK2, va="center", ha="right")
ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0, interval=1))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
ax.set_title("MYXUSDT, авг–сен 2025: шорту «платили» ~0.001%/ч, цена выросла в 150 раз")
fig.tight_layout()
fig.savefig(f"{OUT}/myx_2025.png")
plt.close(fig)
print("ok")
