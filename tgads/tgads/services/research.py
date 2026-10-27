"""Module 1: collecting channels, their posts and stats; niches; ad creatives."""
from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, functions, types

from ..ai import prompts
from ..ai.jobs import enqueue, handler
from ..ai.provider import parse_json
from ..analysis.niches import TOPICS, AdPost, ChannelRow, cluster_creatives, niche_table
from ..analysis.posts import detect_ad
from ..analysis.text import parse_channel_ref, text_hash
from ..analysis.views import PostStat, static_metrics
from ..db import get_setting, session
from ..models import AiJob, Channel, ChannelSnapshot, Creative, MyChannel, MyCreative, Post, utcnow
from ..tg.parse import (
    is_content_message, media_type, message_urls, naive_utc, reactions_count, replies_count,
)
from ..tg.pool import FloodWait, NoAccount, call, pool

log = logging.getLogger(__name__)


# ---------- channel rows ----------

async def find_channel(s: AsyncSession, tg_id: int | None = None, username: str | None = None,
                       invite_hash: str | None = None) -> Channel | None:
    if tg_id:
        ch = (await s.scalars(select(Channel).where(Channel.tg_id == tg_id))).first()
        if ch:
            return ch
    if username:
        ch = (await s.scalars(select(Channel).where(Channel.username == username.lower()))).first()
        if ch:
            return ch
    if invite_hash:
        return (await s.scalars(select(Channel).where(Channel.invite_hash == invite_hash))).first()
    return None


async def add_channel_ref(s: AsyncSession, ref: str, source: str, detail: str = "",
                          status: str = "new") -> Channel | None:
    parsed = parse_channel_ref(ref)
    if not parsed:
        return None
    if parsed.startswith("+"):
        ch = await find_channel(s, invite_hash=parsed[1:])
        if ch is None:
            ch = Channel(invite_hash=parsed[1:], source=source, source_detail=detail, status=status)
            s.add(ch)
    else:
        ch = await find_channel(s, username=parsed)
        if ch is None:
            ch = Channel(username=parsed, source=source, source_detail=detail, status=status)
            s.add(ch)
    if status == "queued" and ch.status in ("new", "error"):
        ch.status = "queued"
    await s.flush()
    return ch


async def import_refs(text: str, source: str = "import") -> int:
    refs = [r for r in text.replace(",", "\n").split() if r.strip()]
    n = 0
    async with session() as s:
        for r in refs:
            if await add_channel_ref(s, r, source, status="queued"):
                n += 1
        await s.commit()
    return n


def _upsert_from_entity(ch: Channel, ent: types.Channel) -> None:
    ch.tg_id = ent.id
    if ent.username:
        ch.username = ent.username.lower()
    elif getattr(ent, "usernames", None):
        active = [u.username for u in ent.usernames if u.active]
        if active:
            ch.username = active[0].lower()
    ch.title = ent.title or ch.title
    if getattr(ent, "participants_count", None):
        ch.subscribers = ent.participants_count


async def search_channels(query: str) -> int:
    """Global Telegram search by keyword; adds broadcast channels as candidates."""
    acc_id, client = await pool.pick("monitor")
    res = await call(lambda: client(functions.contacts.SearchRequest(q=query, limit=100)))
    n = 0
    async with session() as s:
        for chat in res.chats:
            if not isinstance(chat, types.Channel) or not chat.broadcast:
                continue
            ch = await find_channel(s, tg_id=chat.id, username=(chat.username or "").lower() or None)
            if ch is None:
                ch = Channel(source="search", source_detail=query, status="queued")
                s.add(ch)
                n += 1
            _upsert_from_entity(ch, chat)
        await s.commit()
    return n


# ---------- collection ----------

async def resolve_entity(client: TelegramClient, ch: Channel):
    """Input entity for a channel; may raise if it is private and we are not a member."""
    if ch.tg_id:
        try:
            return await client.get_input_entity(types.PeerChannel(ch.tg_id))
        except (ValueError, TypeError):
            pass
    if ch.username:
        return await call(lambda: client.get_input_entity(ch.username))
    if ch.invite_hash:
        info = await call(lambda: client(functions.messages.CheckChatInviteRequest(hash=ch.invite_hash)))
        chat = getattr(info, "chat", None)
        if chat is not None:
            return chat
        ch.title = getattr(info, "title", ch.title)
        ch.subscribers = getattr(info, "participants_count", ch.subscribers)
        raise ValueError("Закрытый канал: данные доступны только после вступления")
    raise ValueError("Нет username / id")


async def upsert_post(s: AsyncSession, ch: Channel, msg) -> Post:
    post = (await s.scalars(select(Post).where(Post.channel_id == ch.id, Post.msg_id == msg.id))).first()
    if post is None:
        post = Post(channel_id=ch.id, msg_id=msg.id)
        s.add(post)
    text = msg.message or ""
    urls = message_urls(msg)
    ad = detect_ad(text, urls, own_username=ch.username)
    post.date = naive_utc(msg.date)
    post.text = text
    post.media_type = media_type(msg)
    post.grouped_id = msg.grouped_id
    post.views = msg.views or 0
    post.forwards = msg.forwards or 0
    post.reactions = reactions_count(msg)
    post.replies = replies_count(msg)
    post.urls = urls
    post.is_ad = ad.is_ad
    post.promotes_channel = ad.promotes_channel
    post.ad_targets = sorted(ad.targets)
    post.text_hash = text_hash(text) if text else ""
    return post


def _merge_albums(messages: list) -> list:
    """One post per album: the part with text (or the first part)."""
    out, seen = [], {}
    for m in messages:
        if not m.grouped_id:
            out.append(m)
            continue
        if m.grouped_id in seen:
            if not seen[m.grouped_id].message and m.message:
                out[out.index(seen[m.grouped_id])] = m
                seen[m.grouped_id] = m
            continue
        seen[m.grouped_id] = m
        out.append(m)
    return out


def compute_stats(ch: Channel, posts: list[Post]) -> None:
    now = utcnow()
    ages = [(p, (now - p.date).total_seconds() / 3600) for p in posts]
    window = [p.views for p, a in ages if 24 <= a <= 72 and p.views]
    if len(window) < 3:
        window = [p.views for p, a in ages if a >= 24 and p.views]
    ch.avg_views = statistics.median(window) if window else None
    ch.err = (ch.avg_views / ch.subscribers) if ch.avg_views and ch.subscribers else None
    if posts:
        dates = sorted(p.date for p in posts)
        ch.last_post_at = dates[-1]
        week = [d for d in dates if d >= now - timedelta(days=7)]
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1 / 24)
        ch.posts_per_day = len(week) / 7 if len(week) < len(dates) else len(dates) / span_days
        ch.ad_share = sum(p.is_ad for p in posts) / len(posts)
    m = static_metrics([PostStat(p.views, p.forwards, p.reactions, a, p.is_ad) for p, a in ages],
                       ch.subscribers or 0)
    ch.fwd_per_1k = m.fwd_per_1k
    ch.react_per_1k = m.react_per_1k
    ch.static = asdict(m)


async def _dedupe(s: AsyncSession, ch: Channel, chat: types.Channel) -> Channel:
    """The same channel may have been added by id (search) and by @username (import)."""
    uname = (chat.username or "").lower() or None
    for dup in (await find_channel(s, tg_id=chat.id), await find_channel(s, username=uname)):
        if dup is not None and dup.id != ch.id:
            has_posts = (await s.scalar(select(func.count()).select_from(Post).where(Post.channel_id == ch.id)))
            if not has_posts:
                await s.delete(ch)
                await s.flush()
                return dup
    return ch


async def collect_channel(channel_id: int, limit: int = 50, account_id: int | None = None) -> None:
    acc_id, client = await pool.pick("monitor", prefer=account_id)
    async with session() as s:
        ch = await s.get(Channel, channel_id)
    # 1. network (no DB transaction open)
    try:
        entity = await resolve_entity(client, ch)
        full = await call(lambda: client(functions.channels.GetFullChannelRequest(channel=entity)))
        chat = next((c for c in full.chats if isinstance(c, types.Channel)), None)
        msgs, pinned = [], None
        if chat is not None and chat.broadcast:
            msgs = [m for m in await call(lambda: client.get_messages(entity, limit=limit)) if is_content_message(m)]
            pinned_id = getattr(full.full_chat, "pinned_msg_id", None)
            if pinned_id:
                pinned = await call(lambda: client.get_messages(entity, ids=pinned_id))
    except FloodWait as e:
        await pool.set_cooldown(acc_id, e.seconds)
        await _set_error(channel_id, f"FloodWait {e.seconds} с — повторю позже", keep_status=True)
        raise
    except Exception as e:
        log.info("collect %s failed: %s", ch.ref, e)
        await _set_error(channel_id, str(e), title=ch.title, subscribers=ch.subscribers)
        return
    # 2. DB
    async with session() as s:
        ch = await s.get(Channel, channel_id)
        if chat is None or not chat.broadcast:
            ch.status, ch.error = "rejected", "Это группа, а не канал"
            await s.commit()
            return
        ch = await _dedupe(s, ch, chat)
        _upsert_from_entity(ch, chat)
        ch.about = full.full_chat.about or ""
        ch.subscribers = full.full_chat.participants_count or ch.subscribers
        ch.pinned_text = (pinned.message or "") if pinned else ""
        posts = [await upsert_post(s, ch, m) for m in _merge_albums(msgs)]
        if msgs:
            ch.last_msg_id = max(ch.last_msg_id or 0, max(m.id for m in msgs))
        compute_stats(ch, posts)
        # snowball: every channel promoted in ads becomes a new candidate
        for p in posts:
            if p.promotes_channel:
                for t in p.ad_targets:
                    await add_channel_ref(s, t if t.startswith("+") else "@" + t, "snowball", ch.ref)
        s.add(ChannelSnapshot(channel_id=ch.id, subscribers=ch.subscribers, avg_views=ch.avg_views))
        ch.collected_at = utcnow()
        ch.error = None
        if ch.status in ("new", "queued", "error"):
            ch.status = "collected"
        await s.commit()


async def _set_error(channel_id: int, err: str, keep_status: bool = False, title: str | None = None,
                     subscribers: int | None = None) -> None:
    async with session() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None:
            return
        ch.error = err[:500]
        if not keep_status:
            ch.status = "error"
        if title:
            ch.title = title
        if subscribers:
            ch.subscribers = subscribers
        await s.commit()


async def collector_loop() -> None:
    """Collect queued channels one by one with a pause (flood safety)."""
    while True:
        delay = 10
        try:
            async with session() as s:
                ch = (await s.scalars(select(Channel).where(Channel.status == "queued")
                                      .order_by(Channel.id).limit(1))).first()
                delay = int(await get_setting(s, "collect_delay_sec") or 8)
            if ch:
                await collect_channel(ch.id)
            else:
                delay = 15
        except NoAccount:
            delay = 60
        except FloodWait as e:
            delay = min(e.seconds, 600)
        except Exception:
            log.exception("collector loop")
        await asyncio.sleep(delay)


# ---------- topics (AI) ----------

async def classify_topics(channel_ids: list[int]) -> list[int]:
    job_ids = []
    async with session() as s:
        chans = (await s.scalars(select(Channel).where(Channel.id.in_(channel_ids)))).all()
        for i in range(0, len(chans), 25):
            batch = []
            for ch in chans[i:i + 25]:
                posts = (await s.scalars(select(Post.text).where(Post.channel_id == ch.id, Post.text != "")
                                         .order_by(Post.date.desc()).limit(3))).all()
                batch.append({"id": ch.id, "title": ch.title, "about": ch.about[:300],
                              "posts": [p[:200] for p in posts]})
            job = await enqueue(s, "classify_topics", f"Тематики для {len(batch)} каналов",
                                prompts.classify_topics(batch))
            job_ids.append(job.id)
        await s.commit()
    return job_ids


@handler("classify_topics")
async def _apply_topics(s: AsyncSession, job: AiJob, text: str) -> None:
    data = parse_json(text)
    for cid, topic in data.items():
        ch = await s.get(Channel, int(cid))
        if ch and isinstance(topic, str):
            ch.topic = topic if topic in TOPICS else "прочее"


# ---------- creatives & niches ----------

async def rebuild_creatives() -> int:
    async with session() as s:
        rows = (await s.execute(
            select(Post, Channel.username, Channel.tg_id).join(Channel, Post.channel_id == Channel.id)
            .where(Post.promotes_channel.is_(True)))).all()
        ads = [AdPost(key=p.id, host=u or str(t), text=p.text, targets=set(p.ad_targets), date=p.date,
                      views=p.views) for p, u, t in rows if p.text]
        clusters = cluster_creatives(ads)
        await s.execute(update(Post).values(creative_id=None))
        await s.execute(delete(Creative))
        topics = {c.username: c.topic for c in (await s.scalars(select(Channel).where(Channel.topic != ""))).all()
                  if c.username}
        for c in clusters:
            tgt = sorted(c.targets)
            topic = next((topics[t] for t in tgt if t in topics), "")
            cr = Creative(text=c.representative.text, targets=tgt, placements=len(c.posts), hosts=len(c.hosts),
                          days_span=round(c.days_span, 1), total_views=c.total_views,
                          first_seen=min(p.date for p in c.posts), last_seen=max(p.date for p in c.posts),
                          score=c.score, topic=topic)
            s.add(cr)
            await s.flush()
            await s.execute(update(Post).where(Post.id.in_([p.key for p in c.posts])).values(creative_id=cr.id))
        await s.commit()
        return len(clusters)


async def niches() -> list:
    async with session() as s:
        chans = (await s.scalars(select(Channel).where(Channel.topic != "", Channel.avg_views.is_not(None)))).all()
        rows = [ChannelRow(c.topic, c.subscribers or 0, c.avg_views or 0, c.err, c.ad_share) for c in chans]
        incoming = dict((await s.execute(select(Creative.topic, func.sum(Creative.placements))
                                         .where(Creative.topic != "").group_by(Creative.topic))).all())
    return niche_table(rows, {k: int(v or 0) for k, v in incoming.items()})


async def generate_ideas(wishes: str = "") -> int:
    table = await niches()
    async with session() as s:
        crs = (await s.scalars(select(Creative).order_by(Creative.score.desc()).limit(15))).all()
        creatives = [{"text": c.text[:600], "targets": c.targets, "placements": c.placements, "hosts": c.hosts,
                      "days": c.days_span, "topic": c.topic} for c in crs]
        niche_rows = [{"topic": r.topic, "score": r.score, "channels": r.channels, "total_reach": int(r.total_reach),
                       "median_err": round(r.median_err, 3) if r.median_err else None,
                       "incoming_ads": r.incoming_ads, "ad_share": round(r.ad_share, 2) if r.ad_share else None}
                      for r in table[:15]]
        job = await enqueue(s, "channel_ideas", "Идеи каналов", prompts.channel_ideas(niche_rows, creatives, wishes))
        await s.commit()
        return job.id


# ---------- our ad posts (AI variants of a proven creative) ----------

async def make_ad_variants(my_channel_id: int, source_text: str) -> int:
    from .content import channel_info

    async with session() as s:
        mc = await s.get(MyChannel, my_channel_id)
        job = await enqueue(s, "ad_variants", f"Рекламные посты для {mc.title or mc.ref}",
                            prompts.ad_variants(source_text, channel_info(mc)), ref_id=my_channel_id)
        await s.commit()
        return job.id


@handler("ad_variants")
async def _apply_variants(s: AsyncSession, job: AiJob, text: str) -> None:
    data = parse_json(text)
    if isinstance(data, dict):
        data = data.get("posts") or data.get("variants") or list(data.values())
    for i, t in enumerate(data or [], 1):
        if isinstance(t, str) and t.strip():
            s.add(MyCreative(my_channel_id=job.ref_id, title=f"ИИ-вариант #{job.id}.{i}", text=t.strip(),
                             source="ai"))
