"""Module 4: find ad managers' contacts in channel descriptions and pinned posts."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import functions, types

from ..analysis.contacts import extract_contacts
from ..analysis.pricing import AD_TEXT_RE, ad_prices
from ..db import session
from ..models import Channel, ChannelContact, Contact, utcnow
from ..tg.pool import FloodWait, NoAccount, call, pool

log = logging.getLogger(__name__)


async def _link(s: AsyncSession, ch: Channel, kind: str, value: str, ad_context: bool, source: str,
                priority: int) -> Contact:
    c = (await s.scalars(select(Contact).where(Contact.kind == kind, Contact.value == value))).first()
    if c is None:
        c = Contact(kind=kind, value=value)
        if kind == "username" and value.endswith("bot"):
            c.entity_type = "bot"
        s.add(c)
        await s.flush()
    link = (await s.scalars(select(ChannelContact).where(ChannelContact.channel_id == ch.id,
                                                         ChannelContact.contact_id == c.id))).first()
    if link is None:
        s.add(ChannelContact(channel_id=ch.id, contact_id=c.id, ad_context=ad_context, source=source,
                             priority=priority))
    else:
        link.ad_context = link.ad_context or ad_context
        link.priority = max(link.priority, priority)
    return c


async def extract_for_channels(channel_ids: list[int]) -> int:
    """Parse descriptions + pinned posts (no network). Returns number of contacts linked."""
    n = 0
    async with session() as s:
        chans = (await s.scalars(select(Channel).where(Channel.id.in_(channel_ids)))).all()
        for ch in chans:
            for source, text in (("about", ch.about), ("pinned", ch.pinned_text)):
                for fc in extract_contacts(text, own_username=ch.username):
                    await _link(s, ch, fc.kind, fc.value, fc.ad_context, source, fc.priority)
                    n += 1
        await s.commit()
    return n


async def _resolve_one(client, value: str) -> tuple[str, str, int | None, object | None]:
    """(entity_type, display name, user id, entity) of an @username; FloodWait is raised to the caller."""
    try:
        ent = await call(lambda: client.get_entity(value), max_sleep=60)
    except FloodWait:
        raise
    except Exception as e:
        return "unknown", f"не найден: {e}"[:200], None, None
    if isinstance(ent, types.User):
        name = " ".join(x for x in (ent.first_name, ent.last_name) if x)
        return ("bot" if ent.bot else "user"), name, ent.id, ent
    if isinstance(ent, types.Channel):
        return ("channel" if ent.broadcast else "group"), ent.title, None, ent
    return "unknown", "", None, ent


async def _read_price_channel(client, ent) -> list[str]:
    """Description, pinned post and recent posts about ads of a public price-list channel (no join needed)."""
    full = await call(lambda: client(functions.channels.GetFullChannelRequest(channel=ent)))
    texts = [full.full_chat.about or ""]
    pinned_id = getattr(full.full_chat, "pinned_msg_id", None)
    if pinned_id:
        pinned = await call(lambda: client.get_messages(ent, ids=pinned_id))
        texts.append((pinned.message or "") if pinned else "")
    for m in await call(lambda: client.get_messages(ent, limit=30)):
        if m.message and AD_TEXT_RE.search(m.message):
            texts.append(m.message)
    return [t for t in texts if t]


async def _apply_price_channel(s: AsyncSession, ch: Channel, value: str, texts: list[str]) -> None:
    """Contacts and prices of a price-list channel belong to the channel that pointed to it."""
    for text in texts:
        for fc in extract_contacts(text, own_username=value):
            if fc.kind == "username" and fc.value.lower() != (ch.username or "").lower():
                await _link(s, ch, fc.kind, fc.value, True, "price_channel", fc.priority + 1)
    found = [p for t in texts for p in ad_prices(t, "@" + value)]
    if found:
        ch.prices = [p for p in ch.prices or [] if p["source"] != "@" + value] + found
        ch.price_text = ch.price_text or next(t for t in texts if ad_prices(t, ""))[:2000]


async def resolve_usernames(limit: int = 40) -> int:
    """Find out whether @contacts are people, bots or (price-list) channels.

    Price-list channels are opened one level deep and their contacts are linked
    to the original channel. ResolveUsername is heavily rate-limited, so this
    runs slowly and stops on a long flood wait.
    """
    async with session() as s:
        todo = (await s.scalars(select(Contact).where(Contact.kind == "username", Contact.entity_type.is_(None))
                                .limit(limit))).all()
        todo_ids = [c.id for c in todo]
    acc_id, client = await pool.pick("outreach")
    done = 0
    for cid in todo_ids:
        async with session() as s:
            c = await s.get(Contact, cid)
            value = c.value
        texts = []
        try:
            etype, name, uid, ent = await _resolve_one(client, value)
            if etype == "channel":
                texts = await _read_price_channel(client, ent)
        except FloodWait as e:
            await pool.set_cooldown(acc_id, e.seconds)
            log.warning("resolve stopped: flood wait %s", e.seconds)
            break
        except Exception as e:
            log.info("price channel @%s: %s", value, e)
        async with session() as s:
            c = await s.get(Contact, cid)
            c.entity_type, c.display_name, c.tg_user_id = etype, name or "", uid
            if texts:
                links = (await s.scalars(select(ChannelContact).where(ChannelContact.contact_id == c.id))).all()
                for link in links:
                    await _apply_price_channel(s, await s.get(Channel, link.channel_id), value, texts)
            await s.commit()
        done += 1
        await asyncio.sleep(4)
    return done


def own_prices(ch: Channel) -> None:
    """Prices written right in the channel's description or pinned post."""
    keep = [p for p in ch.prices or [] if p["source"] not in ("about", "pinned")]
    found = ad_prices(ch.about, "about") + ad_prices(ch.pinned_text, "pinned")
    ch.prices = keep + found
    if found and not ch.price_text:
        ch.price_text = (ch.pinned_text if ad_prices(ch.pinned_text, "") else ch.about)[:2000]


PRICE_CHANNELS_PER_CHANNEL = 4


async def check_prices(channel_id: int) -> None:
    """Prices of one channel: its own description and pinned post, then the @links of its ad contacts.
    A link that turns out to be a channel is usually a price list with the real manager's contact."""
    async with session() as s:
        ch = await s.get(Channel, channel_id)
        own_prices(ch)
        rows = (await s.execute(select(Contact.id, Contact.value)
                                .join(ChannelContact, ChannelContact.contact_id == Contact.id)
                                .where(ChannelContact.channel_id == channel_id, Contact.kind == "username",
                                       Contact.entity_type.is_(None) | (Contact.entity_type == "channel"))
                                .order_by(ChannelContact.ad_context.desc(), ChannelContact.priority.desc())
                                .limit(PRICE_CHANNELS_PER_CHANNEL))).all()
        await s.commit()
    acc_id = client = None
    for cid, value in rows:
        if client is None:
            acc_id, client = await pool.pick("outreach")
        texts = []
        try:
            etype, name, uid, ent = await _resolve_one(client, value)
            if etype == "channel":
                texts = await _read_price_channel(client, ent)
        except FloodWait as e:
            await pool.set_cooldown(acc_id, e.seconds)
            raise
        except Exception as e:
            log.info("price channel @%s: %s", value, e)
            etype, name, uid = "channel", "", None
        async with session() as s:
            c = await s.get(Contact, cid)
            c.entity_type, c.display_name, c.tg_user_id = etype, name or c.display_name, uid
            if texts:
                await _apply_price_channel(s, await s.get(Channel, channel_id), value, texts)
            await s.commit()
        await asyncio.sleep(5)
    async with session() as s:
        ch = await s.get(Channel, channel_id)
        ch.prices_checked_at = utcnow()
        await s.commit()


async def price_loop() -> None:
    """Slowly go through read channels (biggest first): contacts behind price-list links and ad prices."""
    while True:
        delay = 90
        try:
            async with session() as s:
                ch_id = await s.scalar(select(Channel.id).where(Channel.collected_at.is_not(None),
                                                                Channel.prices_checked_at.is_(None))
                                       .order_by(Channel.subscribers.desc().nullslast()).limit(1))
            if ch_id is None:
                delay = 600
            else:
                await check_prices(ch_id)
        except FloodWait as e:
            delay = min(max(e.seconds, 60), 3600)
        except NoAccount:
            delay = 300
        except Exception:
            log.exception("price loop")
            delay = 300
        await asyncio.sleep(delay)


async def find_contacts(channel_ids: list[int]) -> str:
    n = await extract_for_channels(channel_ids)
    resolved = 0
    while True:
        r = await resolve_usernames(limit=40)
        resolved += r
        if r < 40:
            break
    return f"Найдено связей канал-контакт: {n}, проверено username: {resolved}"


async def best_contact(s: AsyncSession, channel_id: int) -> Contact | None:
    """The contact to write to: a person (or bot) near ad keywords first."""
    links = (await s.scalars(select(ChannelContact).where(ChannelContact.channel_id == channel_id)
                             .order_by(ChannelContact.priority.desc()))).all()
    for link in links:
        c = await s.get(Contact, link.contact_id)
        if c.kind == "username" and c.entity_type in ("user", None):
            return c
    for link in links:
        c = await s.get(Contact, link.contact_id)
        if c.kind == "username" and c.entity_type == "bot":
            return c
    return None
