from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from ...db import session
from ...models import Channel, InviteLink, Join, MyChannel, MyCreative, Post, Purchase, ViewSample
from ...services import tasks, tracking
from ..app import back, render, to_utc

router = APIRouter(prefix="/purchases")


@router.get("")
async def index(request: Request):
    async with session() as s:
        purchases = (await s.scalars(select(Purchase).order_by(Purchase.id.desc()))).all()
        rows = []
        for p in purchases:
            rows.append((p, await s.get(Channel, p.channel_id), await s.get(MyChannel, p.my_channel_id),
                         await tracking.metrics(s, p)))
        creatives = {c.id: c for c in (await s.scalars(select(MyCreative))).all()}
        channels = (await s.scalars(select(Channel).where(Channel.status.in_(("approved", "checked")))
                                    .order_by(Channel.title))).all()
        my_channels = (await s.scalars(select(MyChannel))).all()
    # summary per creative: which ad text brings subscribers cheapest
    by_creative: dict[int | None, dict] = {}
    for p, _, _, m in rows:
        if p.status == "cancelled":
            continue
        agg = by_creative.setdefault(p.creative_id, {"n": 0, "spent": 0.0, "joins": 0, "alive": 0, "views": 0})
        agg["n"] += 1
        agg["spent"] += p.price_uzs or 0
        agg["joins"] += m.joins
        agg["alive"] += m.alive
        agg["views"] += m.views or 0
    return render(request, "purchases.html", rows=rows, by_creative=by_creative, creatives=creatives,
                  channels=channels, my_channels=my_channels)


@router.post("")
async def create(channel_id: int = Form(...), my_channel_id: int = Form(...), amount: float = Form(...),
                 currency: str = Form("UZS"), fmt: str = Form("1/24"), scheduled_at: str = Form(""),
                 creative_id: int = Form(0), request_needed: str = Form(""), notes: str = Form("")):
    try:
        pid = await tracking.create_purchase(my_channel_id, channel_id, amount, currency, fmt, to_utc(scheduled_at),
                                             creative_id or None, notes, None, bool(request_needed))
    except Exception as e:
        return back("/purchases", err=f"Закуп не создан: {e}")
    return back(f"/purchases/{pid}", msg="Закуп создан")


@router.get("/{pid}")
async def detail(request: Request, pid: int):
    async with session() as s:
        p = await s.get(Purchase, pid)
        ch = await s.get(Channel, p.channel_id)
        mc = await s.get(MyChannel, p.my_channel_id)
        link = (await s.scalars(select(InviteLink).where(InviteLink.purchase_id == pid))).first()
        joins = (await s.scalars(select(Join).where(Join.invite_link_id == link.id)
                                 .order_by(Join.joined_at))).all() if link else []
        creative = await s.get(MyCreative, p.creative_id) if p.creative_id else None
        post = await s.get(Post, p.ad_post_id) if p.ad_post_id else None
        samples = (await s.execute(select(ViewSample.t_sec, ViewSample.views).where(ViewSample.post_id == post.id)
                                   .order_by(ViewSample.t_sec))).all() if post else []
        m = await tracking.metrics(s, p)
    start = post.date if post else (joins[0].joined_at if joins else None)
    join_points, n = [], 0
    for j in joins:
        n += 1
        if start:
            join_points.append({"x": round((j.joined_at - start).total_seconds() / 3600, 2), "y": n})
    view_points = [{"x": round(t / 3600, 2), "y": v} for t, v in samples]
    ad_text = (creative.text.replace("{link}", link.link) if creative and link else None)
    return render(request, "purchase.html", p=p, ch=ch, mc=mc, link=link, joins=joins, m=m, post=post,
                  ad_text=ad_text, join_points=join_points, view_points=view_points,
                  expected=ch.honest_reach or ch.avg_views)


@router.post("/{pid}/status")
async def set_status(pid: int, status: str = Form(...), notes: str = Form(None)):
    async with session() as s:
        p = await s.get(Purchase, pid)
        p.status = status
        if notes is not None:
            p.notes = notes
        await s.commit()
    return back(f"/purchases/{pid}", msg="Сохранено")


@router.post("/{pid}/sync")
async def sync(pid: int):
    async with session() as s:
        p = await s.get(Purchase, pid)
        mc_id = p.my_channel_id
        has_post = bool(p.ad_post_id)

    async def run() -> str:
        await tracking.sync_my_channel(mc_id)
        found = has_post or await tracking.find_ad_post(pid)
        return "Синхронизировано" + ("" if found else "; рекламный пост пока не найден")

    tasks.spawn(run(), f"Синхронизация закупа #{pid}")
    return back(f"/purchases/{pid}", msg="Синхронизация запущена, обновите страницу через минуту")


@router.post("/{pid}/link")
async def new_link(pid: int, request_needed: str = Form("")):
    try:
        await tracking.create_link(pid, bool(request_needed))
    except Exception as e:
        return back(f"/purchases/{pid}", err=f"Ссылка не создана: {e}")
    return back(f"/purchases/{pid}", msg="Ссылка создана")
