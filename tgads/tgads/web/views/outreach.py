from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import Integer, func, select

from ...analysis.pricing import REC_TEXT
from ...db import get_settings, session
from ...models import (
    Channel, ChannelContact, Contact, Conversation, Message, MyChannel, MyCreative, Offer,
)
from ...services import outreach, tasks, tracking
from ...services.contacts import resolve_usernames
from ..app import back, render, to_utc

router = APIRouter()


@router.get("/contacts")
async def contacts(request: Request, only_ads: int = 0):
    async with session() as s:
        rows = (await s.execute(
            select(Contact, func.count(ChannelContact.id), func.max(ChannelContact.ad_context.cast(Integer)))
            .join(ChannelContact, ChannelContact.contact_id == Contact.id)
            .group_by(Contact.id).order_by(func.count(ChannelContact.id).desc()))).all()
        chans: dict[int, list[Channel]] = {}
        for link, ch in (await s.execute(select(ChannelContact, Channel)
                                         .join(Channel, ChannelContact.channel_id == Channel.id))).all():
            chans.setdefault(link.contact_id, []).append(ch)
        convs = {c.contact_id: c for c in (await s.scalars(select(Conversation))).all()}
    if only_ads:
        rows = [r for r in rows if r[2]]
    return render(request, "contacts.html", rows=rows, chans=chans, convs=convs, only_ads=only_ads)


@router.post("/contacts/resolve")
async def resolve():
    tasks.spawn(resolve_usernames(limit=60), "Проверка username контактов")
    return back("/contacts", msg="Проверка запущена")


@router.get("/outreach")
async def index(request: Request, status: str = ""):
    async with session() as s:
        stmt = select(Conversation, Contact).join(Contact, Conversation.contact_id == Contact.id)
        if status:
            stmt = stmt.where(Conversation.status == status)
        rows = (await s.execute(stmt.order_by(Conversation.unread.desc(),
                                              Conversation.last_message_at.desc().nullslast()))).all()
        offers: dict[int, Offer] = {}
        for o in (await s.scalars(select(Offer).where(Offer.cpm_uzs.is_not(None)))).all():
            best = offers.get(o.conversation_id)
            if best is None or o.cpm_uzs < best.cpm_uzs:
                offers[o.conversation_id] = o
        names = {c.id: c.ref for c in (await s.scalars(select(Channel).where(Channel.id.in_(
            {i for conv, _ in rows for i in (conv.channel_ids or [])})))).all()}
    return render(request, "outreach.html", rows=rows, offers=offers, names=names, status=status, REC_TEXT=REC_TEXT)


@router.get("/outreach/{conv_id}")
async def conversation(request: Request, conv_id: int):
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        contact = await s.get(Contact, conv.contact_id)
        msgs = (await s.scalars(select(Message).where(Message.conversation_id == conv_id)
                                .order_by(Message.at))).all()
        offers = (await s.scalars(select(Offer).where(Offer.conversation_id == conv_id)
                                  .order_by(Offer.cpm_uzs.nullslast()))).all()
        chans = (await s.scalars(select(Channel).where(Channel.id.in_(conv.channel_ids or [])))).all()
        my_channels = (await s.scalars(select(MyChannel))).all()
        creatives = (await s.scalars(select(MyCreative))).all()
        settings = await get_settings(s)
        if conv.unread:
            conv.unread = 0
            await s.commit()
    return render(request, "conversation.html", conv=conv, contact=contact, msgs=msgs, offers=offers, chans=chans,
                  my_channels=my_channels, creatives=creatives, target_cpm=settings["target_cpm_uzs"],
                  REC_TEXT=REC_TEXT)


@router.post("/outreach/{conv_id}/send")
async def send(conv_id: int, text: str = Form(...)):
    try:
        await outreach.send_reply(conv_id, text)
    except Exception as e:
        return back(f"/outreach/{conv_id}", err=f"Не отправлено: {e}")
    return back(f"/outreach/{conv_id}", msg="Отправлено")


@router.post("/outreach/{conv_id}/draft")
async def draft(conv_id: int):
    job_id = await outreach.enqueue_draft(conv_id)
    return back(f"/outreach/{conv_id}", msg=f"Черновик готовится (ИИ-задача #{job_id}), обновите страницу")


@router.post("/outreach/{conv_id}/parse")
async def parse(conv_id: int):
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        await outreach.regex_offers(s, conv)
        job = await outreach.enqueue_parse(s, conv)
        await s.commit()
    return back(f"/outreach/{conv_id}", msg=f"Разбор запущен (ИИ-задача #{job.id})")


@router.post("/outreach/{conv_id}/status")
async def set_status(conv_id: int, status: str = Form(...)):
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        conv.status = status
        await s.commit()
    return back(f"/outreach/{conv_id}", msg="Статус обновлён")


@router.post("/outreach/{conv_id}/offer")
async def add_offer(conv_id: int, channel_id: int = Form(0), amount: float = Form(...), currency: str = Form("UZS"),
                    fmt: str = Form("1/24"), notes: str = Form("")):
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        await outreach.make_offer(s, conv, amount, currency, fmt, notes, "manual", channel_id or None)
        await s.commit()
    return back(f"/outreach/{conv_id}", msg="Предложение добавлено")


@router.post("/outreach/{conv_id}/buy")
async def buy(conv_id: int, offer_id: int = Form(0), channel_id: int = Form(...), my_channel_id: int = Form(...),
              amount: float = Form(...), currency: str = Form("UZS"), fmt: str = Form("1/24"),
              scheduled_at: str = Form(""), creative_id: int = Form(0), request_needed: str = Form(""),
              notes: str = Form("")):
    try:
        pid = await tracking.create_purchase(my_channel_id, channel_id, amount, currency, fmt, to_utc(scheduled_at),
                                             creative_id or None, notes, offer_id or None, bool(request_needed))
    except Exception as e:
        return back(f"/outreach/{conv_id}", err=f"Закуп не создан: {e}")
    return back(f"/purchases/{pid}", msg="Закуп создан, трекинг-ссылка готова")
