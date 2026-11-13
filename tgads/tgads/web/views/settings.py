from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

from ...analysis.views import Thresholds
from ...db import get_settings, session, set_setting
from ..app import back, render

router = APIRouter(prefix="/settings")

NUMBERS = [
    ("usd_uzs", "Курс USD → сум", float),
    ("rub_uzs", "Курс RUB → сум", float),
    ("target_cpm_uzs", "Целевой CPM, сум за 1000 просмотров", float),
    ("min_posts_for_verdict", "Постов для вердикта по каналу", int),
    ("posts_to_track_per_channel", "Постов отследить до статуса «проверен»", int),
    ("joins_per_day", "Вступлений в каналы в день на аккаунт", int),
    ("dms_per_day", "Новых диалогов в день на аккаунт", int),
    ("dm_delay_min", "Пауза между сообщениями, от (с)", int),
    ("dm_delay_max", "Пауза между сообщениями, до (с)", int),
    ("collect_delay_sec", "Пауза между сборами каналов (с)", int),
    ("poll_interval_sec", "Опрос новых постов, каждые (с)", int),
    ("tgstat_pages_per_day", "Страниц TGStat в день", int),
]
THRESHOLD_LABELS = {
    "late_boost_ratio": "Докрутка: факт / прогноз по первым минутам ≥",
    "dead_start_early_share": "Мёртвый старт: за 5 мин меньше доли подписчиков",
    "dead_start_final_share": "Мёртвый старт: за сутки больше доли подписчиков",
    "instant_dump_share": "Вброс: за 2 мин больше доли суточных просмотров",
    "spike_factor": "Скачок: рост скорости просмотров в N раз",
    "spike_min_views": "Скачок: минимум просмотров в интервале",
    "min_early_views": "Минимум просмотров в раннем замере для прогноза",
}


@router.get("")
async def index(request: Request):
    async with session() as s:
        st = await get_settings(s)
    th = asdict(Thresholds.from_dict(st.get("thresholds")))
    return render(request, "settings.html", st=st, numbers=NUMBERS, th=th, th_labels=THRESHOLD_LABELS)


@router.post("")
async def save(request: Request):
    form = await request.form()
    async with session() as s:
        for key, _, typ in NUMBERS:
            if form.get(key) not in (None, ""):
                await set_setting(s, key, typ(form[key]))
        await set_setting(s, "auto_leave_after_check", bool(form.get("auto_leave_after_check")))
        await set_setting(s, "dm_hours", [int(form.get("dm_from") or 9), int(form.get("dm_to") or 21)])
        await set_setting(s, "outreach_templates", {"uz": form.get("tpl_uz", ""), "ru": form.get("tpl_ru", "")})
        await set_setting(s, "outreach_language", form.get("outreach_language") or "uz")
        await set_setting(s, "search_keywords", form.get("search_keywords", ""))
        token = (form.get("claude_oauth_token") or "").strip()
        if token and not token.startswith("•"):
            await set_setting(s, "claude_oauth_token", token)
        elif form.get("claude_token_clear"):
            await set_setting(s, "claude_oauth_token", "")
        if form.get("tgstat_base"):
            await set_setting(s, "tgstat_base", form["tgstat_base"].strip().rstrip("/"))
        defaults = asdict(Thresholds())
        th = {}
        for k, v in defaults.items():
            raw = form.get(f"th_{k}")
            if raw not in (None, ""):
                th[k] = type(v)(float(raw)) if isinstance(v, int) else float(raw)
        await set_setting(s, "thresholds", th)
        await s.commit()
    return back("/settings", msg="Настройки сохранены")
