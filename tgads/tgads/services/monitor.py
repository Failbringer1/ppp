"""Module 3: real-time view tracking of new posts and bot-inflation verdicts.

Flow: channel.status = "monitoring" -> a monitor account joins it -> every new
post is sampled at CHECKPOINTS -> after enough posts the channel gets a verdict.
New posts are detected by NewMessage events and, as a safety net, by polling
messages.getPeerDialogs (top_message of 100 channels per request).
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, errors, events, functions, types, utils

from ..analysis.views import (
    CHECKPOINTS, Curve, PostAnalysis, StaticMetrics, Thresholds, analyze_post, build_curve,
    channel_verdict, is_night, next_checkpoint,
)
from ..db import get_setting, get_settings, session, set_setting
from ..models import Account, Channel, Post, ViewSample, utcnow
from ..tg.parse import is_content_message
from ..tg.pool import FloodWait, NoAccount, bump, call, counter, pool
from . import contacts
from .research import collect_channel, resolve_entity, upsert_post

log = logging.getLogger(__name__)

LAST_CHECKPOINT = CHECKPOINTS[-1]


class Monitor:
    def __init__(self) -> None:
        # marked peer id -> channel row id, per account
        self.watched: dict[int, dict[int, int]] = {}
        self._new_posts: asyncio.Queue[tuple[int, int]] = asyncio.Queue()

    # ----- detection -----

    def install(self, account_id: int, client: TelegramClient) -> None:
        async def on_new(event: events.NewMessage.Event) -> None:
            ch_id = self.watched.get(account_id, {}).get(event.chat_id)
            if ch_id is not None and is_content_message(event.message) and not event.message.out:
                await self._register_post(ch_id, event.message)

        client.add_event_handler(on_new, events.NewMessage(incoming=True))

    async def refresh_watch(self) -> None:
        async with session() as s:
            rows = (await s.scalars(select(Channel).where(Channel.joined.is_(True),
                                                          Channel.monitor_account_id.is_not(None),
                                                          Channel.tg_id.is_not(None)))).all()
            watched: dict[int, dict[int, int]] = {}
            for ch in rows:
                peer = utils.get_peer_id(types.PeerChannel(ch.tg_id))
                watched.setdefault(ch.monitor_account_id, {})[peer] = ch.id
        self.watched = watched

    async def _register_post(self, channel_id: int, msg) -> None:
        async with session() as s:
            ch = await s.get(Channel, channel_id)
            if ch is None:
                return
            exists = (await s.scalars(select(Post).where(Post.channel_id == ch.id, Post.msg_id == msg.id))).first()
            if exists and exists.tracked:
                return
            if msg.grouped_id:
                # albums arrive as several messages; track only the first one
                dup = (await s.scalars(select(Post).where(Post.channel_id == ch.id,
                                                          Post.grouped_id == msg.grouped_id))).first()
                if dup and dup.msg_id != msg.id:
                    return
            post = await upsert_post(s, ch, msg)
            age = (utcnow() - post.date).total_seconds()
            nxt = next_checkpoint(max(age - 10, 0))
            post.tracked = nxt is not None
            post.next_sample_at = post.date + timedelta(seconds=nxt) if nxt else None
            ch.last_msg_id = max(ch.last_msg_id or 0, msg.id)
            await s.commit()
            log.info("tracking new post %s/%s (age %.0fs)", ch.ref, msg.id, age)

    async def poll_loop(self) -> None:
        """Fallback detector: compare top_message of watched channels every N seconds."""
        while True:
            interval = 20
            try:
                async with session() as s:
                    interval = int(await get_setting(s, "poll_interval_sec") or 20)
                await self.refresh_watch()
                for acc_id, peers in self.watched.items():
                    client = pool.clients.get(acc_id)
                    if client is None:
                        continue
                    await self._poll_account(acc_id, client, peers)
            except FloodWait as e:
                interval = max(interval, min(e.seconds, 300))
            except Exception:
                log.exception("poll loop")
            await asyncio.sleep(interval)

    async def _poll_account(self, acc_id: int, client: TelegramClient, peers: dict[int, int]) -> None:
        items = list(peers.items())
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            inputs = []
            for peer, _ in chunk:
                try:
                    inputs.append(types.InputDialogPeer(await client.get_input_entity(peer)))
                except (ValueError, TypeError):
                    continue
            if not inputs:
                continue
            res = await call(lambda: client(functions.messages.GetPeerDialogsRequest(peers=inputs)))
            tops = {utils.get_peer_id(d.peer): d.top_message for d in res.dialogs}
            async with session() as s:
                last = {ch_id: (await s.get(Channel, ch_id)).last_msg_id for _, ch_id in chunk}
            updates: dict[int, int] = {}
            fresh = []
            # network first, DB writes after: never hold the SQLite write lock across requests
            for peer, ch_id in chunk:
                top = tops.get(peer)
                if not top:
                    continue
                if last[ch_id] is not None and top > last[ch_id]:
                    ids = list(range(last[ch_id] + 1, top + 1))[-20:]
                    msgs = await call(lambda: client.get_messages(peer, ids=ids))
                    fresh += [(ch_id, m) for m in msgs if m is not None and is_content_message(m)]
                if last[ch_id] is None or top > last[ch_id]:
                    updates[ch_id] = top
            for ch_id, m in fresh:
                await self._register_post(ch_id, m)
            if updates:
                async with session() as s:
                    for ch_id, top in updates.items():
                        ch = await s.get(Channel, ch_id)
                        ch.last_msg_id = max(ch.last_msg_id or 0, top)
                    await s.commit()

    # ----- sampling -----

    async def sample_loop(self) -> None:
        while True:
            try:
                await self._sample_due()
            except Exception:
                log.exception("sample loop")
            await asyncio.sleep(5)

    async def _sample_due(self) -> None:
        async with session() as s:
            due = (await s.execute(select(Post.id, Post.channel_id, Post.msg_id)
                                   .where(Post.tracked.is_(True), Post.next_sample_at <= utcnow())
                                   .limit(500))).all()
        by_channel: dict[int, list[tuple[int, int]]] = {}
        for pid, ch_id, msg_id in due:
            by_channel.setdefault(ch_id, []).append((pid, msg_id))
        for ch_id, items in by_channel.items():
            async with session() as s:
                ch = await s.get(Channel, ch_id)
            retry = None
            views = None
            try:
                views = await self._fetch_views(ch, [m for _, m in items])
            except FloodWait as e:
                retry = min(e.seconds, 120)
            except Exception as e:
                log.warning("sampling %s failed: %s", ch.ref, e)
                retry = 60
            async with session() as s:
                if views is None:
                    for pid, _ in items:
                        p = await s.get(Post, pid)
                        p.next_sample_at = utcnow() + timedelta(seconds=retry or 60)
                else:
                    await self._store_samples(s, ch_id, [pid for pid, _ in items], views)
                await s.commit()

    async def _fetch_views(self, ch: Channel, msg_ids: list[int]) -> list[tuple[int, int]]:
        client = pool.clients.get(ch.monitor_account_id) if ch.monitor_account_id else None
        if client is None:
            _, client = await pool.pick("monitor")
        entity = await resolve_entity(client, ch)
        res = await call(lambda: client(functions.messages.GetMessagesViewsRequest(
            peer=entity, id=msg_ids, increment=False)))
        return [(mv.views or 0, mv.forwards or 0) for mv in res.views]

    async def _store_samples(self, s: AsyncSession, ch_id: int, post_ids: list[int],
                             views: list[tuple[int, int]]) -> None:
        now = utcnow()
        analyze = False
        for pid, (v, f) in zip(post_ids, views):
            p = await s.get(Post, pid)
            t = int((now - p.date).total_seconds())
            s.add(ViewSample(post_id=p.id, t_sec=t, views=v, forwards=f))
            p.views, p.forwards = v, f
            nxt = next_checkpoint(t)
            if nxt is None or t >= LAST_CHECKPOINT:
                p.tracked, p.next_sample_at = False, None
            else:
                p.next_sample_at = p.date + timedelta(seconds=nxt)
            analyze = analyze or t >= 10_800  # enough data for a (preliminary) analysis
        await s.flush()
        if analyze:
            await analyze_channel(s, await s.get(Channel, ch_id))


monitor = Monitor()


# ----- analysis -----

async def load_curves(s: AsyncSession) -> tuple[Curve, Curve]:
    return (Curve.from_json(await get_setting(s, "curve_day")),
            Curve.from_json(await get_setting(s, "curve_night")))


async def _samples(s: AsyncSession, post_id: int) -> list[tuple[int, int, int]]:
    rows = (await s.execute(select(ViewSample.t_sec, ViewSample.views, ViewSample.forwards)
                            .where(ViewSample.post_id == post_id).order_by(ViewSample.t_sec))).all()
    return [tuple(r) for r in rows]


async def analyze_channel(s: AsyncSession, ch: Channel) -> None:
    settings = await get_settings(s)
    th = Thresholds.from_dict(settings.get("thresholds"))
    day, night = await load_curves(s)
    posts = (await s.scalars(select(Post).where(Post.channel_id == ch.id,
                                                Post.id.in_(select(ViewSample.post_id).distinct()))
                             .order_by(Post.date.desc()).limit(30))).all()
    analyses: list[PostAnalysis] = []
    for p in posts:
        samples = await _samples(s, p.id)
        if not samples or samples[-1][0] < 10_800:
            continue
        curve = night if is_night(p.date) else day
        a = analyze_post(samples, ch.subscribers or 0, curve, th, published_at=p.date)
        p.analysis = a.to_json()
        analyses.append(a)
    static = StaticMetrics(**ch.static) if ch.static else None
    v = channel_verdict(analyses, static, int(settings.get("min_posts_for_verdict") or 3))
    ch.verdict, ch.score, ch.reasons, ch.honest_reach = v.verdict, v.score, v.reasons, v.honest_reach
    ch.checked_at = utcnow()
    complete = sum(1 for a in analyses if a.complete)
    if ch.status == "monitoring" and complete >= int(settings.get("posts_to_track_per_channel") or 5):
        ch.status = "checked"


async def rebuild_curves() -> int:
    """Re-learn the day/night reference curves from all posts tracked to 24h."""
    async with session() as s:
        ids = (await s.scalars(select(ViewSample.post_id).where(ViewSample.t_sec >= 86_400).distinct())).all()
        day_posts, night_posts = [], []
        for pid in ids:
            p = await s.get(Post, pid)
            samples = await _samples(s, pid)
            (night_posts if is_night(p.date) else day_posts).append(samples)
        await set_setting(s, "curve_day", build_curve(day_posts).to_json())
        await set_setting(s, "curve_night", build_curve(night_posts).to_json())
        await s.commit()
        return len(ids)


# ----- joining / leaving -----

async def join_loop() -> None:
    """Join channels queued for monitoring, respecting the per-account daily limit."""
    while True:
        delay = 60
        try:
            delay = await _join_next()
        except Exception:
            log.exception("join loop")
        await asyncio.sleep(delay)


async def _join_next() -> int:
    """One step of the cycle: join the biggest waiting channel, read its description, pinned post and
    history (contacts come from them), track new posts; channels done with the check are left."""
    async with session() as s:
        settings = await get_settings(s)
        await _finish_overdue(s, settings)
        await s.commit()
        await _leave_checked(s, settings)
        await s.commit()
        # a joined channel whose description/contacts could not be read yet (flood wait, crash)
        unread = (await s.execute(select(Channel.id, Channel.monitor_account_id)
                                  .where(Channel.joined.is_(True), Channel.collected_at.is_(None))
                                  .order_by(Channel.subscribers.desc().nullslast()).limit(1))).first()
    if unread:
        await _read_channel(*unread)
    async with session() as s:
        # the biggest channels first: join slots are scarce (daily limit, 500 channels per account)
        ch = (await s.scalars(select(Channel).where(Channel.status == "monitoring", Channel.joined.is_(False))
                              .order_by(Channel.subscribers.desc().nullslast(), Channel.id).limit(1))).first()
        if ch is None:
            return 60
        limit = int(settings.get("joins_per_day") or 30)
        try:
            acc_id, client = await pool.pick("monitor", prefer=ch.monitor_account_id)
        except NoAccount:
            return 120
        acc = await s.get(Account, acc_id)
        if counter(acc, "joins") >= limit:
            return 600
        joined_now = await s.scalar(select(func.count(Channel.id)).where(Channel.joined.is_(True),
                                                                         Channel.monitor_account_id == acc_id))
        if joined_now >= int(settings.get("max_joined_per_account") or 480):
            return 600  # wait until checked channels are left
        try:
            if ch.invite_hash and not ch.username and not ch.tg_id:
                entity = await _join_by_invite(client, ch.invite_hash)
            else:
                entity = await resolve_entity(client, ch)
                await call(lambda: client(functions.channels.JoinChannelRequest(channel=entity)))
            full = await client.get_entity(entity)
            dup = await s.get(Channel, await s.scalar(select(Channel.id).where(Channel.tg_id == full.id,
                                                                                Channel.id != ch.id)) or 0)
            if dup is not None:
                # the same channel was added twice (by id and by link): keep the older row
                ch.status, ch.error = "rejected", f"Дубликат канала #{dup.id}"
                if dup.status not in ("approved",):
                    dup.status = "monitoring"
                await s.commit()
                return 5
            ch.tg_id = full.id
            await _archive_and_mute(client, entity)
            last = await client.get_messages(entity, limit=1)
            ch.last_msg_id = last[0].id if last else 0
            ch.joined, ch.joined_at, ch.monitor_account_id, ch.error = True, utcnow(), acc_id, None
            bump(acc, "joins")
            log.info("joined %s with account %s", ch.ref, acc_id)
            joined_id = ch.id
        except FloodWait as e:
            await pool.set_cooldown(acc_id, e.seconds)
            return min(e.seconds, 900)
        except Exception as e:
            ch.error = f"Не удалось вступить: {e}"[:500]
            ch.status = "error"
            joined_id = None
        await s.commit()
    await monitor.refresh_watch()
    if joined_id:
        await _read_channel(joined_id, acc_id)
    return random.randint(40, 120)


async def _read_channel(channel_id: int, account_id: int) -> None:
    """Description, pinned post and last posts of a freshly joined channel, then its admin contacts."""
    try:
        await collect_channel(channel_id, account_id=account_id)
        await contacts.extract_for_channels([channel_id])
    except (FloodWait, NoAccount):
        return  # stays unread: _join_next retries it later
    except Exception:
        log.exception("reading joined channel %s", channel_id)
    async with session() as s:
        ch = await s.get(Channel, channel_id)
        if ch.status == "error":  # a failed read must not end the view check
            ch.status = "monitoring"
        ch.collected_at = ch.collected_at or utcnow()  # tried once; do not loop on a channel that cannot be read
        await s.commit()


async def _finish_overdue(s: AsyncSession, settings: dict) -> None:
    """A channel that posts rarely must not hold a join slot for weeks: give it a verdict by what it has."""
    days = float(settings.get("max_monitor_days") or 4)
    rows = (await s.scalars(select(Channel).where(Channel.status == "monitoring", Channel.joined.is_(True),
                                                  Channel.joined_at < utcnow() - timedelta(days=days))
                            .limit(20))).all()
    for ch in rows:
        await analyze_channel(s, ch)
        ch.status = "checked"
        if ch.verdict == "unknown":
            ch.reasons = (ch.reasons or []) + [f"За {days:g} дн. мало постов для вердикта"]


async def _join_by_invite(client: TelegramClient, invite_hash: str):
    try:
        upd = await call(lambda: client(functions.messages.ImportChatInviteRequest(hash=invite_hash)))
        return upd.chats[0]
    except errors.UserAlreadyParticipantError:
        info = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
        return info.chat


async def _archive_and_mute(client: TelegramClient, entity) -> None:
    try:
        peer = await client.get_input_entity(entity)
        await client(functions.folders.EditPeerFoldersRequest(
            folder_peers=[types.InputFolderPeer(peer=peer, folder_id=1)]))
        await client(functions.account.UpdateNotifySettingsRequest(
            peer=types.InputNotifyPeer(peer=peer), settings=types.InputPeerNotifySettings(mute_until=2**31 - 1)))
    except Exception as e:
        log.debug("archive/mute failed: %s", e)


async def _leave_checked(s: AsyncSession, settings: dict) -> None:
    """Free join slots: leave checked/rejected channels that are not used for purchases."""
    if not settings.get("auto_leave_after_check"):
        return
    rows = (await s.scalars(select(Channel).where(Channel.joined.is_(True),
                                                  Channel.status.in_(("checked", "rejected"))).limit(5))).all()
    for ch in rows:
        client = pool.clients.get(ch.monitor_account_id)
        if client is None:
            continue
        try:
            entity = await resolve_entity(client, ch)
            await call(lambda: client(functions.channels.LeaveChannelRequest(channel=entity)))
            ch.joined = False
        except Exception as e:
            log.info("leave %s failed: %s", ch.ref, e)


async def start_monitoring(channel_ids: list[int]) -> int:
    async with session() as s:
        n = 0
        for ch in (await s.scalars(select(Channel).where(Channel.id.in_(channel_ids)))).all():
            if ch.status not in ("monitoring",):
                ch.status = "monitoring"
                n += 1
        await s.commit()
    return n


async def tracked_stats(s: AsyncSession, channel_id: int) -> dict:
    tracked = await s.scalar(select(func.count()).select_from(Post).where(Post.channel_id == channel_id,
                                                                          Post.tracked.is_(True)))
    analyzed = await s.scalar(select(func.count()).select_from(Post).where(Post.channel_id == channel_id,
                                                                           Post.analysis.is_not(None)))
    return {"tracking": tracked or 0, "analyzed": analyzed or 0}
