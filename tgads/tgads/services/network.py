"""Housekeeping of existing channels where an account is an admin (an old network that is brought back to life):
a survey, the description, and clearing old posts."""
from __future__ import annotations

import asyncio

from telethon import functions, types

from ..tg.pool import call, pool


def _name(ref: str) -> str:
    return ref.strip().lstrip("@")


async def survey(account_id: int, refs: list[str]) -> list[dict]:
    """What the account sees in each channel: rights, description, number of posts and the oldest ones."""
    client = await pool.get(account_id)
    out = []
    for ref in refs:
        row: dict = {"ref": ref}
        try:
            chat = await call(lambda: client.get_entity(_name(ref)))
            full = await call(lambda: client(functions.channels.GetFullChannelRequest(chat)))
            rights = chat.admin_rights
            row.update(
                title=chat.title, about=full.full_chat.about, subscribers=full.full_chat.participants_count,
                creator=bool(chat.creator), pinned=full.full_chat.pinned_msg_id,
                rights=None if rights is None else {k: bool(getattr(rights, k, False)) for k in (
                    "change_info", "post_messages", "edit_messages", "delete_messages", "invite_users")},
            )
            first = await call(lambda: client.get_messages(chat, limit=6, reverse=True))
            row["total"] = (await call(lambda: client.get_messages(chat, limit=0))).total
            row["oldest"] = [
                {"id": m.id, "date": m.date.isoformat(), "service": isinstance(m, types.MessageService),
                 "media": type(m.media).__name__ if getattr(m, "media", None) else None,
                 "text": (m.message or "")[:400] if not isinstance(m, types.MessageService) else type(m.action).__name__}
                for m in first]
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        out.append(row)
        await asyncio.sleep(1.5)
    return out


async def set_about(account_id: int, ref: str, about: str) -> str:
    client = await pool.get(account_id)
    chat = await call(lambda: client.get_input_entity(_name(ref)))
    await call(lambda: client(functions.messages.EditChatAboutRequest(peer=chat, about=about)))
    return f"{ref}: описание обновлено"


async def purge(account_id: int, ref: str, keep_id: int) -> str:
    """Delete every post of the channel except `keep_id`. Cannot be undone."""
    client = await pool.get(account_id)
    chat = await call(lambda: client.get_input_entity(_name(ref)))
    ids = [m.id async for m in client.iter_messages(chat) if m.id != keep_id and not isinstance(m, types.MessageService)]
    done = 0
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        await call(lambda: client.delete_messages(chat, part), max_sleep=120)
        done += len(part)
        await asyncio.sleep(2)
    left = (await call(lambda: client.get_messages(chat, limit=0))).total
    return f"{ref}: удалено {done}, осталось сообщений {left}"
