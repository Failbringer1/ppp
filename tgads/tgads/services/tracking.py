"""Module 6: one invite link per purchase, joins / leaves, ad post views, CPS."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import functions, types

from ..analysis.views import next_checkpoint
from ..db import get_settings, session
from ..models import Channel, Conversation, InviteLink, Join, MyChannel, Offer, Post, Purchase, utcnow
from ..tg.parse import is_content_message, message_urls, naive_utc
from ..tg.pool import FloodWait, call, pool
from .content import resolve_my_channel
from .research import resolve_entity, upsert_post

log = logging.getLogger(__name__)


async def create_purchase(my_channel_id: int, channel_id: int, amount: float | None, currency: str, fmt: str,
                          scheduled_at: datetime | None, creative_id: int | None, notes: str,
                          offer_id: int | None = None, request_needed: bool = False) -> int:
    async with session() as s:
        settings = await get_settings(s)
        rate = {"USD": settings["usd_uzs"], "RUB": settings["rub_uzs"]}.get(currency, 1.0)
        p = Purchase(my_channel_id=my_channel_id, channel_id=channel_id, offer_id=offer_id, amount=amount,
                     currency=currency, price_uzs=(amount or 0) * rate if amount else None, fmt=fmt,
                     scheduled_at=scheduled_at, creative_id=creative_id, notes=notes)
        s.add(p)
        if offer_id:
            offer = await s.get(Offer, offer_id)
            if offer:
                conv = await s.get(Conversation, offer.conversation_id)
                conv.status = "agreed"
        ch = await s.get(Channel, channel_id)
        if ch.status not in ("approved",):
            ch.status = "approved"
        await s.commit()
        pid = p.id
    await create_link(pid, request_needed)
    return pid


async def create_link(purchase_id: int, request_needed: bool = False) -> str:
    async with session() as s:
        p = await s.get(Purchase, purchase_id)
        mc = await s.get(MyChannel, p.my_channel_id)
        ch = await s.get(Channel, p.channel_id)
        title = f"ad {ch.username or ch.id} {p.id}"[:32]
    _, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
    peer = await resolve_my_channel(client, mc)
    inv = await call(lambda: client(functions.messages.ExportChatInviteRequest(
        peer=peer, title=title, request_needed=request_needed or None)))
    async with session() as s:
        s.add(InviteLink(my_channel_id=mc.id, purchase_id=purchase_id, link=inv.link, title=title,
                         request_needed=request_needed))
        await s.commit()
    return inv.link


async def sync_my_channel(mc_id: int) -> None:
    """Pull importers of every tracked link and who left the channel (admin log keeps 48h)."""
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        links = (await s.scalars(select(InviteLink).where(InviteLink.my_channel_id == mc_id))).all()
        link_rows = [(x.id, x.link, x.request_needed) for x in links]
    if not link_rows:
        return
    _, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
    peer = await resolve_my_channel(client, mc)
    for link_id, link, request_needed in link_rows:
        info = await call(lambda: client(functions.messages.GetExportedChatInviteRequest(peer=peer, link=link)))
        importers = await _importers(client, peer, link, requested=False)
        if request_needed:
            # approve pending join requests from our ad links so nobody is lost
            pending = await _importers(client, peer, link, requested=True)
            for uid, _ in pending:
                try:
                    user = await client.get_input_entity(uid)
                    await call(lambda: client(functions.messages.HideChatJoinRequestRequest(
                        peer=peer, user_id=user, approved=True)))
                    importers.append((uid, utcnow()))
                except Exception as e:
                    log.info("approve %s failed: %s", uid, e)
        async with session() as s:
            row = await s.get(InviteLink, link_id)
            inv = getattr(info, "invite", info)
            row.usage = getattr(inv, "usage", None) or row.usage
            row.requested = getattr(inv, "requested", None) or 0
            row.synced_at = utcnow()
            known = set((await s.scalars(select(Join.user_id).where(Join.invite_link_id == link_id))).all())
            for uid, date in importers:
                if uid not in known:
                    s.add(Join(invite_link_id=link_id, user_id=uid, joined_at=date))
                    known.add(uid)
            await s.commit()
    await _sync_leaves(client, peer, mc_id)


async def _importers(client, peer, link: str, requested: bool) -> list[tuple[int, datetime]]:
    out: list[tuple[int, datetime]] = []
    offset_date, offset_user = None, types.InputUserEmpty()
    for _ in range(50):
        res = await call(lambda: client(functions.messages.GetChatInviteImportersRequest(
            peer=peer, link=link, offset_date=offset_date, offset_user=offset_user, limit=100,
            requested=requested or None)))
        if not res.importers:
            break
        for imp in res.importers:
            out.append((imp.user_id, naive_utc(imp.date)))
        last = res.importers[-1]
        users = {u.id: u for u in res.users}
        if len(res.importers) < 100 or last.user_id not in users:
            break
        offset_date = last.date
        offset_user = types.InputUser(last.user_id, users[last.user_id].access_hash or 0)
    return out


async def _sync_leaves(client, peer, mc_id: int) -> None:
    try:
        res = await call(lambda: client(functions.channels.GetAdminLogRequest(
            channel=peer, q="", max_id=0, min_id=0, limit=100,
            events_filter=types.ChannelAdminLogEventsFilter(leave=True))))
    except Exception as e:
        log.info("admin log unavailable: %s", e)
        return
    async with session() as s:
        link_ids = (await s.scalars(select(InviteLink.id).where(InviteLink.my_channel_id == mc_id))).all()
        for ev in res.events:
            if not isinstance(ev.action, types.ChannelAdminLogEventActionParticipantLeave):
                continue
            left = naive_utc(ev.date)
            joins = (await s.scalars(select(Join).where(Join.invite_link_id.in_(link_ids),
                                                        Join.user_id == ev.user_id, Join.left_at.is_(None)))).all()
            for j in joins:
                if j.joined_at <= left:
                    j.left_at = left
        await s.commit()


async def find_ad_post(purchase_id: int) -> bool:
    """Find our ad in the host channel (a recent post containing our link) and track its views."""
    async with session() as s:
        p = await s.get(Purchase, purchase_id)
        link = await s.scalar(select(InviteLink.link).where(InviteLink.purchase_id == purchase_id))
        ch = await s.get(Channel, p.channel_id)
    if not link:
        return False
    key = link.rsplit("/", 1)[-1].lstrip("+")
    _, client = await pool.pick("monitor", prefer=ch.monitor_account_id)
    entity = await resolve_entity(client, ch)
    msgs = await call(lambda: client.get_messages(entity, limit=40))
    found = None
    for m in msgs:
        if is_content_message(m) and (key in (m.message or "") or any(key in u for u in message_urls(m))):
            found = m
            break
    if found is None:
        return False
    async with session() as s:
        ch = await s.get(Channel, ch.id)
        post = await upsert_post(s, ch, found)
        await s.flush()
        age = (utcnow() - post.date).total_seconds()
        nxt = next_checkpoint(age)
        if nxt and not post.tracked:
            post.tracked = True
            post.next_sample_at = post.date + timedelta(seconds=nxt)
        p = await s.get(Purchase, purchase_id)
        p.ad_post_id = post.id
        if p.status in ("planned", "paid"):
            p.status = "published"
        await s.commit()
    return True


async def tracking_loop() -> None:
    while True:
        try:
            async with session() as s:
                mc_ids = (await s.scalars(select(InviteLink.my_channel_id).distinct())).all()
                waiting = (await s.scalars(select(Purchase.id).where(
                    Purchase.ad_post_id.is_(None), Purchase.status.in_(("planned", "paid")),
                    (Purchase.scheduled_at.is_(None)) | (Purchase.scheduled_at <= utcnow())))).all()
            for mc_id in mc_ids:
                try:
                    await sync_my_channel(mc_id)
                except FloodWait as e:
                    await asyncio.sleep(min(e.seconds, 300))
                except Exception as e:
                    log.warning("sync channel %s: %s", mc_id, e)
            for pid in waiting:
                try:
                    await find_ad_post(pid)
                except Exception as e:
                    log.info("find ad post %s: %s", pid, e)
        except Exception:
            log.exception("tracking loop")
        await asyncio.sleep(600)


@dataclass
class PurchaseMetrics:
    joins: int
    left_1d: int
    left_3d: int
    left_7d: int
    alive: int
    views: int | None
    cpm: float | None
    ctr: float | None
    cps: float | None
    cps_alive: float | None

    @property
    def retention_7d(self) -> float | None:
        return (1 - self.left_7d / self.joins) if self.joins else None


def _left_within(joins: list[Join], days: int) -> int:
    return sum(1 for j in joins if j.left_at and (j.left_at - j.joined_at) <= timedelta(days=days))


async def metrics(s: AsyncSession, p: Purchase) -> PurchaseMetrics:
    link = (await s.scalars(select(InviteLink).where(InviteLink.purchase_id == p.id))).first()
    joins = list((await s.scalars(select(Join).where(Join.invite_link_id == link.id))).all()) if link else []
    n = max(len(joins), link.usage if link else 0)
    alive = n - sum(1 for j in joins if j.left_at)
    views = None
    if p.ad_post_id:
        post = await s.get(Post, p.ad_post_id)
        views = post.views if post else None
    price = p.price_uzs
    return PurchaseMetrics(
        joins=n, left_1d=_left_within(joins, 1), left_3d=_left_within(joins, 3), left_7d=_left_within(joins, 7),
        alive=alive, views=views,
        cpm=(price / views * 1000) if price and views else None,
        ctr=(n / views) if views else None,
        cps=(price / n) if price and n else None,
        cps_alive=(price / alive) if price and alive else None,
    )
