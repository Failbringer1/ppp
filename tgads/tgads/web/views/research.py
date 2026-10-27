from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from ...db import get_setting, session
from ...models import AiJob, Channel, Creative, MyChannel
from ...analysis.tgstat import CATEGORIES
from ...services import research, tasks, tgstat
from ..app import back, render

router = APIRouter(prefix="/research")


@router.get("")
async def index(request: Request):
    table = await research.niches()
    async with session() as s:
        creatives = (await s.scalars(select(Creative).order_by(Creative.score.desc()).limit(40))).all()
        ideas = (await s.scalars(select(AiJob).where(AiJob.kind == "channel_ideas")
                                 .order_by(AiJob.id.desc()).limit(3))).all()
        keywords = await get_setting(s, "search_keywords")
        my_channels = (await s.scalars(select(MyChannel))).all()
        untyped = (await s.scalars(select(Channel.id).where(Channel.status.in_(("collected", "checked", "approved")),
                                                            Channel.topic == ""))).all()
    return render(request, "research.html", niches=table, creatives=creatives, ideas=ideas, keywords=keywords,
                  my_channels=my_channels, untyped=len(untyped), categories=CATEGORIES,
                  tgstat=await tgstat.settings_snapshot())


async def _search_many(queries: list[str]) -> str:
    total = 0
    for q in queries:
        total += await research.search_channels(q)
        await asyncio.sleep(5)
    return f"Новых каналов: {total}"


@router.post("/search")
async def search(keywords: str = Form(...)):
    queries = [q.strip() for q in keywords.replace("\n", ",").split(",") if q.strip()]
    tasks.spawn(_search_many(queries), f"Поиск каналов: {len(queries)} запросов")
    return back("/research", msg="Поиск запущен — новые каналы встанут в очередь на сбор")


@router.post("/import")
async def import_(refs: str = Form(...)):
    n = await research.import_refs(refs)
    return back("/channels?status=queued", msg=f"Добавлено в очередь сбора: {n}")


@router.post("/classify")
async def classify():
    async with session() as s:
        ids = (await s.scalars(select(Channel.id).where(Channel.status.in_(("collected", "checked", "approved")),
                                                        Channel.topic == ""))).all()
    jobs = await research.classify_topics(list(ids))
    return back("/ai", msg=f"Создано ИИ-задач: {len(jobs)}")


@router.post("/creatives")
async def creatives():
    n = await research.rebuild_creatives()
    return back("/research", msg=f"Креативов (кластеров): {n}")


@router.post("/ideas")
async def ideas(wishes: str = Form("")):
    job_id = await research.generate_ideas(wishes)
    return back(f"/ai/{job_id}", msg="Задача создана")


@router.post("/variants")
async def variants(my_channel_id: int = Form(...), creative_id: int = Form(...)):
    async with session() as s:
        cr = await s.get(Creative, creative_id)
        text = cr.text
    job_id = await research.make_ad_variants(my_channel_id, text)
    return back(f"/ai/{job_id}", msg="Варианты появятся в «Контент → рекламные посты»")


@router.post("/tgstat")
async def tgstat_rating(category: str = Form(""), sort: str = Form("members"), url: str = Form(""),
                        pages: int = Form(3)):
    target = url.strip() or await tgstat.rating_url(category, sort)
    tasks.spawn(tgstat.import_list(target, max_pages=max(1, min(pages, 20))), f"TGStat: {target}")
    return back("/research", msg="Сбор из TGStat запущен — результат на дашборде, каналы встанут в очередь сбора")


@router.post("/tgstat/html")
async def tgstat_html(html: str = Form(...)):
    try:
        res = await tgstat.import_html(html)
    except tgstat.TgstatError as e:
        return back("/research", err=str(e))
    return back("/research", msg=res)


@router.get("/tgstat/debug")
async def tgstat_debug(request: Request):
    return render(request, "tgstat_debug.html", pages=tgstat.debug_pages(), tgstat=await tgstat.settings_snapshot())
