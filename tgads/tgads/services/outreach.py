"""Module 5: semi-automatic price requests to ad managers and reply parsing."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, errors, events

from ..ai import prompts
from ..ai.jobs import enqueue, handler
from ..ai.provider import parse_json
from ..analysis.pricing import cpm, parse_prices, recommend, to_uzs
from ..db import get_settings, session
from ..models import Account, AiJob, Channel, Contact, Conversation, Message, MyChannel, Offer, utcnow
from ..tg.pool import TASHKENT, FloodWait, bump, counter, pool
from .contacts import best_contact

log = logging.getLogger(__name__)

_SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def spin(template: str, **values: str) -> str:
    """Fill {placeholders} and pick a random option in {a|b|c} groups."""
    text = template
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return _SPIN.sub(lambda m: random.choice(m.group(1).split("|")).strip(), text)


async def queue_outreach(channel_ids: list[int], my_channel_id: int | None, language: str) -> dict:
    """One conversation per manager: channels that share a manager go into one message."""
    stats = {"queued": 0, "no_contact": 0, "bot": 0, "already": 0}
    async with session() as s:
        settings = await get_settings(s)
        template = settings["outreach_templates"].get(language) or settings["outreach_templates"]["uz"]
        groups: dict[int, list[Channel]] = {}
        for ch in (await s.scalars(select(Channel).where(Channel.id.in_(channel_ids)))).all():
            c = await best_contact(s, ch.id)
            if c is None:
                stats["no_contact"] += 1
            elif c.entity_type == "bot":
                stats["bot"] += 1
            else:
                groups.setdefault(c.id, []).append(ch)
        for contact_id, chans in groups.items():
            existing = (await s.scalars(select(Conversation).where(Conversation.contact_id == contact_id,
                                                                   Conversation.status != "failed"))).first()
            if existing:
                existing.channel_ids = sorted(set(existing.channel_ids or []) | {c.id for c in chans})
                stats["already"] += 1
                continue
            text = spin(template, channel=", ".join(c.ref for c in chans))
            s.add(Conversation(contact_id=contact_id, my_channel_id=my_channel_id, channel_ids=[c.id for c in chans],
                               first_text=text, status="queued"))
            stats["queued"] += 1
        await s.commit()
    return stats


def _in_hours(hours: list[int]) -> bool:
    h = datetime.now(TASHKENT).hour
    return hours[0] <= h < hours[1]


async def sender_loop() -> None:
    while True:
        delay = 30
        try:
            delay = await _send_next()
        except Exception:
            log.exception("sender loop")
        await asyncio.sleep(delay)


async def _send_next() -> int:
    async with session() as s:
        settings = await get_settings(s)
        if not _in_hours(settings.get("dm_hours") or [9, 21]):
            return 300
        conv = (await s.scalars(select(Conversation).where(Conversation.status == "queued")
                                .order_by(Conversation.id).limit(1))).first()
        if conv is None:
            return 30
        contact = await s.get(Contact, conv.contact_id)
        conv_id, target, text = conv.id, contact.value, conv.first_text
        limit = int(settings.get("dms_per_day") or 15)
        accs = (await s.scalars(select(Account).where(Account.enabled.is_(True), Account.status == "active"))).all()
        free = [a.id for a in accs if a.has_role("outreach") and counter(a, "dms") < limit
                and (not a.cooldown_until or a.cooldown_until < utcnow())]
        dmin, dmax = int(settings.get("dm_delay_min") or 60), int(settings.get("dm_delay_max") or 240)
    if not free:
        return 600
    acc_id = free[0]
    try:
        client = await pool.get(acc_id)
        msg = await client.send_message(target, text)
    except (errors.PeerFloodError, FloodWait) as e:
        # Telegram thinks we spam: rest this account for a day
        await pool.set_cooldown(acc_id, getattr(e, "seconds", 0) or 86_400)
        return 60
    except Exception as e:
        async with session() as s:
            conv = await s.get(Conversation, conv_id)
            conv.status, conv.error = "failed", str(e)[:500]
            await s.commit()
        return 10
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        conv.status, conv.account_id, conv.last_message_at, conv.error = "sent", acc_id, utcnow(), None
        s.add(Message(conversation_id=conv_id, direction="out", text=text, tg_msg_id=msg.id))
        bump(await s.get(Account, acc_id), "dms")
        await s.commit()
    return random.randint(dmin, dmax)


# ----- incoming replies -----

def install(account_id: int, client: TelegramClient) -> None:
    async def on_private(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.message.out:
            return
        sender = await event.get_sender()
        uname = (getattr(sender, "username", None) or "").lower()
        await store_incoming(account_id, sender.id, uname, event.message.message or "", event.message.id)

    client.add_event_handler(on_private, events.NewMessage(incoming=True))


async def store_incoming(account_id: int, user_id: int, username: str, text: str, msg_id: int) -> None:
    async with session() as s:
        q = select(Contact).where((Contact.tg_user_id == user_id) |
                                  ((Contact.kind == "username") & (Contact.value == username)))
        contacts = (await s.scalars(q)).all()
        if not contacts:
            return
        for c in contacts:
            c.tg_user_id = c.tg_user_id or user_id
        contact_ids = [c.id for c in contacts]
        conv = (await s.scalars(select(Conversation).where(Conversation.contact_id.in_(contact_ids),
                                                           Conversation.account_id == account_id)
                                .order_by(Conversation.id.desc()))).first()
        if conv is None:
            return
        s.add(Message(conversation_id=conv.id, direction="in", text=text, tg_msg_id=msg_id))
        conv.unread += 1
        conv.last_message_at = utcnow()
        if conv.status in ("sent", "no_reply"):
            conv.status = "replied"
        await s.flush()
        await regex_offers(s, conv)
        await enqueue_parse(s, conv)
        await s.commit()


# ----- offers -----

async def make_offer(s: AsyncSession, conv: Conversation, amount: float | None, currency: str | None,
                     fmt: str | None, notes: str, source: str, channel_id: int | None) -> Offer:
    settings = await get_settings(s)
    ch = await s.get(Channel, channel_id) if channel_id else None
    price = to_uzs(amount, currency, settings["usd_uzs"], settings["rub_uzs"])
    views = (ch.honest_reach or ch.avg_views) if ch else None
    c = cpm(price, views)
    offer = Offer(conversation_id=conv.id, channel_id=channel_id, amount=amount, currency=currency,
                  price_uzs=price, fmt=fmt, notes=notes, expected_views=views, cpm_uzs=c,
                  recommendation=recommend(c, float(settings["target_cpm_uzs"])), source=source)
    s.add(offer)
    return offer


async def regex_offers(s: AsyncSession, conv: Conversation) -> None:
    await s.execute(delete(Offer).where(Offer.conversation_id == conv.id, Offer.source == "regex"))
    msgs = (await s.scalars(select(Message).where(Message.conversation_id == conv.id,
                                                  Message.direction == "in"))).all()
    default_ch = conv.channel_ids[0] if conv.channel_ids else None
    for m in msgs:
        for opt in parse_prices(m.text):
            await make_offer(s, conv, opt.amount, opt.currency, opt.fmt, opt.line, "regex", default_ch)


async def _messages_json(s: AsyncSession, conv: Conversation) -> list[dict]:
    msgs = (await s.scalars(select(Message).where(Message.conversation_id == conv.id)
                            .order_by(Message.at))).all()
    return [{"from": "я" if m.direction == "out" else "админ", "text": m.text} for m in msgs[-20:]]


async def _channel_refs(s: AsyncSession, conv: Conversation) -> list[Channel]:
    if not conv.channel_ids:
        return []
    return list((await s.scalars(select(Channel).where(Channel.id.in_(conv.channel_ids)))).all())


async def enqueue_parse(s: AsyncSession, conv: Conversation) -> AiJob:
    chans = await _channel_refs(s, conv)
    return await enqueue(s, "parse_offer", f"Разбор ответа: диалог #{conv.id}",
                         prompts.parse_offer(await _messages_json(s, conv), [c.ref for c in chans]), ref_id=conv.id)


@handler("parse_offer")
async def _apply_offer(s: AsyncSession, job: AiJob, text: str) -> None:
    conv = await s.get(Conversation, job.ref_id)
    if conv is None:
        return
    data = parse_json(text)
    chans = await _channel_refs(s, conv)
    by_name = {c.username: c.id for c in chans if c.username}
    await s.execute(delete(Offer).where(Offer.conversation_id == conv.id, Offer.source == "ai"))
    for o in data.get("offers") or []:
        name = (o.get("channel") or "").lstrip("@").lower()
        ch_id = by_name.get(name) or (chans[0].id if chans else None)
        amount = o.get("amount")
        await make_offer(s, conv, float(amount) if amount is not None else None, o.get("currency"),
                         o.get("format"), o.get("notes") or "", "ai", ch_id)
    if data.get("status") == "declined":
        conv.status = "declined"


async def enqueue_draft(conv_id: int) -> int:
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        settings = await get_settings(s)
        chans = await _channel_refs(s, conv)
        mc = await s.get(MyChannel, conv.my_channel_id) if conv.my_channel_id else None
        offers = (await s.scalars(select(Offer).where(Offer.conversation_id == conv.id))).all()
        ctx = {
            "target_cpm_uzs": settings["target_cpm_uzs"],
            "channels": [{"ref": c.ref, "subscribers": c.subscribers, "honest_reach": c.honest_reach,
                          "verdict": c.verdict} for c in chans],
            "offers": [{"price_uzs": o.price_uzs, "format": o.fmt, "cpm_uzs": o.cpm_uzs} for o in offers],
            "my_channel": mc.title if mc else None,
        }
        lang = settings.get("outreach_language") or "uz"
        job = await enqueue(s, "draft_reply", f"Черновик ответа: диалог #{conv.id}",
                            prompts.draft_reply(await _messages_json(s, conv), lang, ctx), ref_id=conv.id)
        await s.commit()
        return job.id


@handler("draft_reply")
async def _apply_draft(s: AsyncSession, job: AiJob, text: str) -> None:
    conv = await s.get(Conversation, job.ref_id)
    if conv:
        conv.draft_reply = text.strip()


async def send_reply(conv_id: int, text: str) -> None:
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        contact = await s.get(Contact, conv.contact_id)
        acc_id, target = conv.account_id, contact.value
    if acc_id is None:
        acc_id, client = await pool.pick("outreach")
    else:
        client = await pool.get(acc_id)
    msg = await client.send_message(target, text)
    async with session() as s:
        conv = await s.get(Conversation, conv_id)
        conv.account_id = acc_id
        if conv.status == "queued":
            conv.status = "sent"
        conv.draft_reply = ""
        conv.last_message_at = utcnow()
        s.add(Message(conversation_id=conv_id, direction="out", text=text, tg_msg_id=msg.id))
        await s.commit()

