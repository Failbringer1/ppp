"""Module 2: donor posts, best-post selection, AI rewrite/generation, scheduled publishing."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, functions, types

from ..ai import prompts
from ..ai.jobs import enqueue, handler
from ..analysis.posts import ScoredPost, score_posts
from ..config import get_config
from ..db import session
from ..models import AiJob, Channel, ContentItem, Donor, MyChannel, Post, utcnow
from ..tg.pool import TASHKENT, FloodWait, call, pool
from .research import add_channel_ref, collect_channel, resolve_entity

log = logging.getLogger(__name__)


# ----- my channels -----

async def resolve_my_channel(client: TelegramClient, mc: MyChannel):
    if mc.tg_id:
        try:
            return await client.get_input_entity(types.PeerChannel(mc.tg_id))
        except (ValueError, TypeError):
            pass
    ref = mc.ref.strip()
    if ref.lstrip("-").isdigit():
        return await client.get_input_entity(int(ref))
    return await call(lambda: client.get_input_entity(ref))


async def refresh_my_channel(mc_id: int) -> str:
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
    acc_id, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
    entity = await client.get_entity(await resolve_my_channel(client, mc))
    full = await call(lambda: client(functions.channels.GetFullChannelRequest(channel=entity)))
    subs = full.full_chat.participants_count
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        mc.tg_id, mc.title, mc.subscribers = entity.id, entity.title, subs
        mc.publisher_account_id = mc.publisher_account_id or acc_id
        await s.commit()
    return f"{entity.title}: {subs} подписчиков"


def channel_info(mc: MyChannel) -> dict:
    return {"title": mc.title, "ref": mc.ref, "language": mc.language, "topic": mc.topic,
            "positioning": mc.positioning}


# ----- donors -----

async def add_donors(mc_id: int, refs: str) -> int:
    n = 0
    async with session() as s:
        for r in refs.replace(",", "\n").split():
            ch = await add_channel_ref(s, r, "donor", status="queued")
            if ch is None:
                continue
            exists = (await s.scalars(select(Donor).where(Donor.my_channel_id == mc_id,
                                                          Donor.channel_id == ch.id))).first()
            if not exists:
                s.add(Donor(my_channel_id=mc_id, channel_id=ch.id))
                n += 1
        await s.commit()
    return n


async def refresh_donors(mc_id: int, limit: int = 100) -> str:
    async with session() as s:
        ids = (await s.scalars(select(Donor.channel_id).where(Donor.my_channel_id == mc_id))).all()
    ok = 0
    for cid in ids:
        try:
            await collect_channel(cid, limit=limit)
            ok += 1
        except FloodWait as e:
            return f"Обновлено {ok}/{len(ids)}, остановлено: FloodWait {e.seconds} с"
        await asyncio.sleep(3)
    return f"Обновлено доноров: {ok}/{len(ids)}"


async def top_posts(s: AsyncSession, mc_id: int, days: int = 30, limit: int = 60) -> list[tuple[Post, float]]:
    donor_ids = (await s.scalars(select(Donor.channel_id).where(Donor.my_channel_id == mc_id))).all()
    used = set((await s.scalars(select(ContentItem.source_post_id).where(
        ContentItem.my_channel_id == mc_id, ContentItem.source_post_id.is_not(None)))).all())
    used_hashes = set((await s.scalars(select(Post.text_hash).where(Post.id.in_(used)))).all()) if used else set()
    since = utcnow() - timedelta(days=days)
    out: list[tuple[Post, float]] = []
    for cid in donor_ids:
        posts = (await s.scalars(select(Post).where(Post.channel_id == cid, Post.date >= since,
                                                    Post.is_ad.is_(False)))).all()
        now = utcnow()
        scored = score_posts([ScoredPost(p, p.views, p.forwards, p.reactions,
                                         (now - p.date).total_seconds() / 3600) for p in posts])
        out += [(sp.key, sp.score) for sp in scored
                if sp.key.id not in used and (not sp.key.text_hash or sp.key.text_hash not in used_hashes)]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:limit]


# ----- drafts -----

def strip_source(text: str, source_username: str | None) -> str:
    """Remove the donor's own mentions/links (signature) from a copied post."""
    if not text:
        return ""
    if source_username:
        text = re.sub(r"(?im)^.*(@" + re.escape(source_username) + r"|t\.me/" + re.escape(source_username) +
                      r")\b.*$", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _download_media(post: Post) -> list[str]:
    if not post.media_type or post.media_type in ("poll",):
        return []
    async with session() as s:
        ch = await s.get(Channel, post.channel_id)
    acc_id, client = await pool.pick("monitor", prefer=ch.monitor_account_id)
    entity = await resolve_entity(client, ch)
    if post.grouped_id:
        ids = list(range(post.msg_id - 9, post.msg_id + 10))
        msgs = [m for m in await call(lambda: client.get_messages(entity, ids=ids))
                if m is not None and m.grouped_id == post.grouped_id]
    else:
        msgs = [await call(lambda: client.get_messages(entity, ids=post.msg_id))]
    folder = get_config().media_dir
    paths = []
    for m in sorted((m for m in msgs if m and m.media), key=lambda m: m.id):
        p = await client.download_media(m, file=str(folder / f"p{post.id}_{m.id}"))
        if p:
            paths.append(p)
    return paths


async def make_draft(mc_id: int, post_id: int, mode: str) -> int:
    """mode: copy (as is, minus donor signature) | rewrite (AI)."""
    async with session() as s:
        post = await s.get(Post, post_id)
        ch = await s.get(Channel, post.channel_id)
        mc = await s.get(MyChannel, mc_id)
        uname = ch.username
    try:
        paths, err = await _download_media(post), None
    except Exception as e:
        paths, err = [], f"Медиа не скачалось: {e}"[:500]
    async with session() as s:
        item = ContentItem(my_channel_id=mc_id, source=mode, source_post_id=post_id, media_paths=paths,
                           text=strip_source(post.text, uname), error=err,
                           status="generating" if mode == "rewrite" else "draft")
        s.add(item)
        await s.flush()
        if mode == "rewrite":
            await enqueue(s, "rewrite_post", f"Рерайт поста {ch.ref}/{post.msg_id}",
                          prompts.rewrite_post(post.text, channel_info(mc)), ref_id=item.id)
        await s.commit()
        return item.id


async def generate(mc_id: int, hint: str) -> int:
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        examples = [p.text for p, _ in (await top_posts(s, mc_id, limit=5)) if p.text]
        item = ContentItem(my_channel_id=mc_id, source="ai", status="generating", text="")
        s.add(item)
        await s.flush()
        await enqueue(s, "generate_post", f"Пост для {mc.title or mc.ref}: {hint[:60]}",
                      prompts.generate_post(channel_info(mc), hint, examples), ref_id=item.id)
        await s.commit()
        return item.id


@handler("rewrite_post")
@handler("generate_post")
async def _apply_text(s: AsyncSession, job: AiJob, text: str) -> None:
    item = await s.get(ContentItem, job.ref_id)
    if item:
        item.text = text.strip()
        if item.status == "generating":
            item.status = "draft"


# ----- schedule & publish -----

def _slots(mc: MyChannel) -> list[tuple[int, int]]:
    out = []
    for t in (mc.post_times or "").split(","):
        t = t.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", t):
            h, m = t.split(":")
            out.append((int(h), int(m)))
    return sorted(out) or [(9, 0), (13, 0), (19, 0)]


async def next_free_slot(s: AsyncSession, mc: MyChannel) -> datetime:
    taken = set((await s.scalars(select(ContentItem.scheduled_at).where(
        ContentItem.my_channel_id == mc.id, ContentItem.status == "queued"))).all())
    now_local = datetime.now(TASHKENT)
    for day in range(0, 60):
        d = (now_local + timedelta(days=day)).date()
        for h, m in _slots(mc):
            local = datetime(d.year, d.month, d.day, h, m, tzinfo=TASHKENT)
            if local <= now_local + timedelta(minutes=2):
                continue
            naive = local.astimezone(timezone.utc).replace(tzinfo=None)
            if naive not in taken:
                return naive
    return utcnow() + timedelta(hours=1)


async def schedule(item_id: int, when_utc: datetime | None = None) -> datetime:
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc = await s.get(MyChannel, item.my_channel_id)
        item.scheduled_at = when_utc or await next_free_slot(s, mc)
        item.status = "queued"
        item.error = None
        await s.commit()
        return item.scheduled_at


async def publish(item_id: int) -> None:
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc = await s.get(MyChannel, item.my_channel_id)
        text, paths = item.text, list(item.media_paths or [])
    try:
        acc_id, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
        entity = await resolve_my_channel(client, mc)
        if paths:
            caption = text if len(text) <= 1024 else ""
            sent = await client.send_file(entity, paths if len(paths) > 1 else paths[0], caption=caption)
            if not caption and text:
                sent = await client.send_message(entity, text)
        else:
            sent = await client.send_message(entity, text, link_preview=True)
        msg_id = sent[0].id if isinstance(sent, list) else sent.id
        status, err = "published", None
    except Exception as e:
        status, err, msg_id = "failed", str(e)[:500], None
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        item.status, item.error, item.tg_msg_id = status, err, msg_id
        if status == "published":
            item.published_at = utcnow()
        await s.commit()


async def publisher_loop() -> None:
    while True:
        try:
            async with session() as s:
                due = (await s.scalars(select(ContentItem.id).where(ContentItem.status == "queued",
                                                                    ContentItem.scheduled_at <= utcnow()))).all()
            for item_id in due:
                await publish(item_id)
        except Exception:
            log.exception("publisher loop")
        await asyncio.sleep(20)

