from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import func, or_, select

from ...analysis.views import CHECKPOINTS, Curve, is_night
from ...db import get_settings, session
from ...models import Channel, ChannelContact, Contact, Conversation, MyChannel, Post, ViewSample
from ...services import contacts as contacts_svc
from ...services import content, monitor, outreach, research, tasks
from ..app import back, render

router = APIRouter()
PAGE = 100
SORTS = {
    "subs": Channel.subscribers.desc().nullslast(),
    "views": Channel.avg_views.desc().nullslast(),
    "err": Channel.err.desc().nullslast(),
    "score": Channel.score.desc().nullslast(),
    "reach": Channel.honest_reach.desc().nullslast(),
    "new": Channel.id.desc(),
}
STATUSES = ["new", "queued", "collected", "monitoring", "checked", "approved", "rejected", "error"]


@router.get("/channels")
async def channels(request: Request, status: str = "", topic: str = "", verdict: str = "", source: str = "",
                   q: str = "", min_subs: int = 0, sort: str = "subs", page: int = 1):
    async with session() as s:
        stmt = select(Channel)
        if status:
            stmt = stmt.where(Channel.status == status)
        if topic:
            stmt = stmt.where(Channel.topic == topic)
        if verdict:
            stmt = stmt.where(Channel.verdict == verdict)
        if source:
            stmt = stmt.where(Channel.source == source)
        if q:
            like = f"%{q.lower().lstrip('@')}%"
            stmt = stmt.where(or_(func.lower(Channel.title).like(like), Channel.username.like(like)))
        if min_subs:
            stmt = stmt.where(Channel.subscribers >= min_subs)
        total = await s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (await s.scalars(stmt.order_by(SORTS.get(sort, SORTS["subs"])).offset((page - 1) * PAGE)
                                .limit(PAGE))).all()
        topics = [t for t in (await s.scalars(select(Channel.topic).distinct())).all() if t]
        my_channels = (await s.scalars(select(MyChannel))).all()
        settings = await get_settings(s)
    params = dict(status=status, topic=topic, verdict=verdict, source=source, q=q, min_subs=min_subs, sort=sort)
    return render(request, "channels.html", rows=rows, total=total, page=page, pages=(total + PAGE - 1) // PAGE,
                  params=params, topics=sorted(topics), statuses=STATUSES, my_channels=my_channels,
                  target_cpm=settings["target_cpm_uzs"])


@router.post("/channels/bulk")
async def bulk(request: Request, action: str = Form(...), ids: list[int] = Form([]), my_channel_id: int = Form(0),
               language: str = Form("uz"), back_url: str = Form("/channels")):
    if not ids:
        return back(back_url, err="Ничего не выбрано")
    if action == "collect":
        late = []
        async with session() as s:
            for ch in (await s.scalars(select(Channel).where(Channel.id.in_(ids)))).all():
                if ch.status in ("new", "collected", "error", "queued"):
                    ch.status = "queued"
                else:
                    late.append(ch.id)  # monitoring/approved/...: keep the status, refresh right away
            await s.commit()
        if late:
            tasks.spawn(_recollect(late), f"Обновление {len(late)} каналов")
        return back(back_url, msg="Каналы поставлены в очередь сбора")
    if action == "classify":
        jobs = await research.classify_topics(ids)
        return back(back_url, msg=f"Создано ИИ-задач: {len(jobs)}")
    if action == "monitor":
        n = await monitor.start_monitoring(ids)
        return back(back_url, msg=f"На проверку отправлено: {n}. Монитор вступит в каналы с паузами.")
    if action == "contacts":
        tasks.spawn(contacts_svc.find_contacts(ids), f"Поиск контактов: {len(ids)} каналов")
        return back(back_url, msg="Поиск контактов запущен")
    if action == "outreach":
        st = await outreach.queue_outreach(ids, my_channel_id or None, language)
        return back("/outreach", msg=f"В очереди: {st['queued']}, без контакта: {st['no_contact']}, "
                                     f"только бот: {st['bot']}, уже в переписке: {st['already']}")
    if action == "donor":
        if not my_channel_id:
            return back(back_url, err="Выберите свой канал")
        async with session() as s:
            refs = [c.ref for c in (await s.scalars(select(Channel).where(Channel.id.in_(ids)))).all()]
        n = await content.add_donors(my_channel_id, "\n".join(refs))
        return back(back_url, msg=f"Добавлено доноров: {n}")
    if action in ("approve", "reject", "unmonitor"):
        new = {"approve": "approved", "reject": "rejected", "unmonitor": "collected"}[action]
        async with session() as s:
            for ch in (await s.scalars(select(Channel).where(Channel.id.in_(ids)))).all():
                ch.status = new
            await s.commit()
        return back(back_url, msg="Статус обновлён")
    return back(back_url, err=f"Неизвестное действие {action}")


async def _recollect(ids: list[int]) -> str:
    for cid in ids:
        await research.collect_channel(cid)
    return f"Обновлено: {len(ids)}"


@router.get("/channels/{ch_id}")
async def channel(request: Request, ch_id: int):
    async with session() as s:
        ch = await s.get(Channel, ch_id)
        settings = await get_settings(s)
        posts = (await s.scalars(select(Post).where(Post.channel_id == ch_id).order_by(Post.date.desc())
                                 .limit(60))).all()
        links = (await s.execute(select(ChannelContact, Contact).join(Contact, ChannelContact.contact_id == Contact.id)
                                 .where(ChannelContact.channel_id == ch_id)
                                 .order_by(ChannelContact.priority.desc()))).all()
        convs = [c for c in (await s.scalars(select(Conversation))).all() if ch_id in (c.channel_ids or [])]
        day = Curve.from_json(settings.get("curve_day"))
        night = Curve.from_json(settings.get("curve_night"))
        series = []
        for p in [p for p in posts if p.analysis or p.tracked][:15]:
            samples = (await s.execute(select(ViewSample.t_sec, ViewSample.views).where(ViewSample.post_id == p.id)
                                       .order_by(ViewSample.t_sec))).all()
            est = ((p.analysis or {}).get("forecast") or {}).get("est")
            norm = bool(est)
            series.append({
                "label": f"{(p.date.strftime('%d.%m %H:%M'))} #{p.msg_id}",
                "flags": (p.analysis or {}).get("flags", []),
                "night": is_night(p.date),
                "points": [{"x": round(t / 60, 2), "y": round(v / est, 3) if norm else v} for t, v in samples if t > 0],
                "normalized": norm,
            })
        my_channels = (await s.scalars(select(MyChannel))).all()
    ref = {"p25": [], "p50": [], "p75": []}
    for t in CHECKPOINTS:
        q = day.quantiles(t)
        for k, v in zip(("p25", "p50", "p75"), q):
            ref[k].append({"x": round(t / 60, 2), "y": round(v, 3)})
    fair_price = (ch.honest_reach or ch.avg_views or 0) * settings["target_cpm_uzs"] / 1000
    return render(request, "channel.html", ch=ch, posts=posts, links=links, convs=convs, series=series, ref=ref,
                  curve_posts=(day.n_posts, night.n_posts), fair_price=fair_price, my_channels=my_channels)


@router.get("/vetting")
async def vetting(request: Request, show: str = "active"):
    statuses = ("monitoring", "checked", "approved") if show == "active" else ("monitoring", "checked", "approved",
                                                                               "rejected")
    async with session() as s:
        rows = (await s.scalars(select(Channel).where(Channel.status.in_(statuses))
                                .order_by(Channel.verdict, Channel.honest_reach.desc().nullslast()))).all()
        stats = {ch.id: await monitor.tracked_stats(s, ch.id) for ch in rows}
        settings = await get_settings(s)
        my_channels = (await s.scalars(select(MyChannel))).all()
        day = Curve.from_json(settings.get("curve_day"))
    return render(request, "vetting.html", rows=rows, stats=stats, target_cpm=settings["target_cpm_uzs"],
                  my_channels=my_channels, curve_posts=day.n_posts, show=show)


@router.post("/vetting/curves")
async def curves():
    n = await monitor.rebuild_curves()
    return back("/vetting", msg=f"Эталонные кривые пересчитаны по {n} постам")


@router.post("/vetting/reanalyze")
async def reanalyze():
    async with session() as s:
        rows = (await s.scalars(select(Channel).where(Channel.status.in_(("monitoring", "checked", "approved"))))).all()
        for ch in rows:
            await monitor.analyze_channel(s, ch)
        await s.commit()
    return back("/vetting", msg=f"Пересчитано каналов: {len(rows)}")
