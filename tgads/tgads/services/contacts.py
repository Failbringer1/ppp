"""Module 4: find ad managers' contacts in channel descriptions and pinned posts."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import functions, types

from ..analysis.contacts import extract_contacts
from ..db import session
from ..models import Channel, ChannelContact, Contact
from ..tg.pool import FloodWait, call, pool

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
        about = None
        try:
            ent = await call(lambda: client.get_entity(value), max_sleep=60)
            if isinstance(ent, types.User):
                etype = "bot" if ent.bot else "user"
                name = " ".join(x for x in (ent.first_name, ent.last_name) if x)
                uid = ent.id
            elif isinstance(ent, types.Channel):
                etype = "channel" if ent.broadcast else "group"
                name, uid = ent.title, None
                if ent.broadcast:
                    full = await call(lambda: client(functions.channels.GetFullChannelRequest(channel=ent)))
                    about = full.full_chat.about or ""
            else:
                etype, name, uid = "unknown", "", None
        except FloodWait as e:
            await pool.set_cooldown(acc_id, e.seconds)
            log.warning("resolve stopped: flood wait %s", e.seconds)
            break
        except Exception as e:
            etype, name, uid = "unknown", f"не найден: {e}"[:200], None
        async with session() as s:
            c = await s.get(Contact, cid)
            c.entity_type, c.display_name, c.tg_user_id = etype, name or "", uid
            if about:
                # price-list channel: its contacts belong to every channel that pointed to it
                links = (await s.scalars(select(ChannelContact).where(ChannelContact.contact_id == c.id))).all()
                for link in links:
                    ch = await s.get(Channel, link.channel_id)
                    for fc in extract_contacts(about, own_username=value):
                        if fc.kind == "username":
                            await _link(s, ch, fc.kind, fc.value, True, "price_channel", fc.priority + 1)
            await s.commit()
        done += 1
        await asyncio.sleep(4)
    return done


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
