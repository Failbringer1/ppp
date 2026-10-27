"""Turn Telethon objects into plain values."""
from __future__ import annotations

from datetime import datetime, timezone

from telethon import types


def naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def message_urls(msg: types.Message) -> list[str]:
    urls: list[str] = []
    try:
        for ent, txt in msg.get_entities_text():
            if isinstance(ent, types.MessageEntityTextUrl):
                urls.append(ent.url)
            elif isinstance(ent, types.MessageEntityUrl):
                urls.append(txt)
    except Exception:
        pass
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for b in row.buttons:
            # Telethon 1.45 (newer layers) merged KeyboardButtonUrl into KeyboardButton: read the field
            url = getattr(b, "url", None)
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def reactions_count(msg: types.Message) -> int:
    r = getattr(msg, "reactions", None)
    if not r or not getattr(r, "results", None):
        return 0
    return sum(x.count for x in r.results)


def replies_count(msg: types.Message) -> int:
    r = getattr(msg, "replies", None)
    return int(r.replies) if r else 0


def media_type(msg: types.Message) -> str | None:
    m = msg.media
    if m is None or isinstance(m, types.MessageMediaWebPage):
        return None
    if isinstance(m, types.MessageMediaPhoto):
        return "photo"
    if isinstance(m, types.MessageMediaDocument):
        if msg.video or msg.gif:
            return "video"
        if msg.voice or msg.audio:
            return "audio"
        return "document"
    if isinstance(m, types.MessageMediaPoll):
        return "poll"
    return type(m).__name__.replace("MessageMedia", "").lower()


def is_content_message(msg) -> bool:
    return isinstance(msg, types.Message) and not getattr(msg, "action", None)
