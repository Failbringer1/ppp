"""End-to-end flow of modules 1-6 against a fake Telethon client (no network)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from sqlalchemy import select
from telethon import functions, types, utils

from tgads.db import init_db, session, set_setting
from tgads.models import (
    Account, Channel, Contact, ContentItem, Conversation, InviteLink, Join, MyChannel, Offer, Post, Purchase,
    ViewSample, utcnow,
)
from tgads.services import content, contacts, monitor, outreach, research, tracking
from tgads.tg.pool import pool


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


class FakeClient:
    """Implements the subset of TelegramClient used by the services."""

    def __init__(self):
        self.channels: dict[str, types.Channel] = {}
        self.msgs: dict[int, list] = {}
        self.views: dict[tuple[int, int], int] = {}
        self.users: dict[str, types.User] = {}
        self.sent: list[tuple[object, str]] = []
        self.joined: set[int] = set()
        self.about: dict[int, str] = {}

    def is_connected(self):
        return True

    def add_event_handler(self, *a, **k):
        pass

    def add_channel(self, cid: int, username: str, title: str, subs: int, about: str = ""):
        ch = types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=aware(utcnow()),
                           broadcast=True, username=username, access_hash=cid * 7, participants_count=subs)
        self.channels[username] = ch
        self.msgs[cid] = []
        self.about[cid] = about
        return ch

    def add_msg(self, cid: int, mid: int, text: str, age: timedelta, views: int, fwd: int = 0):
        m = types.Message(id=mid, peer_id=types.PeerChannel(cid), date=aware(utcnow() - age), message=text,
                          views=views, forwards=fwd, post=True)
        self.msgs[cid].append(m)
        self.views[(cid, mid)] = views
        return m

    def _cid(self, x) -> int:
        if isinstance(x, (types.InputPeerChannel, types.PeerChannel, types.Channel)):
            return getattr(x, "channel_id", None) or x.id
        if isinstance(x, int):
            return utils.resolve_id(x)[0]
        if isinstance(x, str):
            return self.channels[x.lstrip("@").lower()].id
        raise ValueError(f"cannot resolve {x!r}")

    def _channel(self, cid: int) -> types.Channel:
        return next(c for c in self.channels.values() if c.id == cid)

    async def get_input_entity(self, x):
        if isinstance(x, str) and x.lstrip("@").lower() in self.users:
            u = self.users[x.lstrip("@").lower()]
            return types.InputPeerUser(u.id, u.access_hash)
        cid = self._cid(x)
        return types.InputPeerChannel(cid, cid * 7)

    async def get_entity(self, x):
        if isinstance(x, str) and x.lstrip("@").lower() in self.users:
            return self.users[x.lstrip("@").lower()]
        return self._channel(self._cid(x))

    async def get_messages(self, entity, limit=None, ids=None):
        cid = self._cid(entity)
        msgs = sorted(self.msgs[cid], key=lambda m: m.id, reverse=True)
        if ids is not None:
            if isinstance(ids, int):
                return next((m for m in msgs if m.id == ids), None)
            return [next((m for m in msgs if m.id == i), None) for i in ids]
        return msgs[:limit]

    async def send_message(self, target, text, **kw):
        self.sent.append((target, text))
        return NS(id=len(self.sent))

    async def send_file(self, target, files, caption=""):
        self.sent.append((target, caption))
        return NS(id=len(self.sent))

    async def download_media(self, m, file=None):
        return None

    async def __call__(self, req):
        if isinstance(req, functions.contacts.SearchRequest):
            return NS(chats=list(self.channels.values()))
        if isinstance(req, functions.channels.GetFullChannelRequest):
            cid = self._cid(req.channel)
            ch = self._channel(cid)
            return NS(full_chat=NS(about=self.about[cid], participants_count=ch.participants_count,
                                   pinned_msg_id=None), chats=[ch])
        if isinstance(req, functions.channels.JoinChannelRequest):
            self.joined.add(self._cid(req.channel))
            return NS()
        if isinstance(req, (functions.folders.EditPeerFoldersRequest, functions.account.UpdateNotifySettingsRequest,
                            functions.channels.LeaveChannelRequest)):
            return True
        if isinstance(req, functions.messages.GetPeerDialogsRequest):
            dialogs = []
            for p in req.peers:
                cid = p.peer.channel_id
                top = max((m.id for m in self.msgs[cid]), default=0)
                dialogs.append(NS(peer=types.PeerChannel(cid), top_message=top))
            return NS(dialogs=dialogs)
        if isinstance(req, functions.messages.GetMessagesViewsRequest):
            cid = self._cid(req.peer)
            return NS(views=[NS(views=self.views[(cid, i)], forwards=0) for i in req.id])
        if isinstance(req, functions.messages.ExportChatInviteRequest):
            return NS(link="https://t.me/+TrackHash123")
        if isinstance(req, functions.messages.GetExportedChatInviteRequest):
            return NS(invite=NS(usage=2, requested=0))
        if isinstance(req, functions.messages.GetChatInviteImportersRequest):
            return NS(importers=[NS(user_id=501, date=aware(utcnow() - timedelta(hours=5))),
                                 NS(user_id=502, date=aware(utcnow() - timedelta(hours=4)))], users=[])
        if isinstance(req, functions.channels.GetAdminLogRequest):
            return NS(events=[NS(action=types.ChannelAdminLogEventActionParticipantLeave(), user_id=502,
                                 date=aware(utcnow() - timedelta(hours=1)))])
        raise NotImplementedError(type(req).__name__)


async def test_full_flow():
    await init_db()
    fake = FakeClient()
    fake.add_channel(1001, "kino_uz", "Kino UZ", 40_000, about="Eng zo'r kinolar\nReklama bo'yicha: @kino_admin")
    fake.add_channel(1002, "my_channel", "Мой канал", 500)
    for i in range(12):
        fake.add_msg(1001, 10 + i, f"Yangi film #{i}", timedelta(hours=30 + i * 6), 8000 + i * 300, fwd=20)
    fake.add_msg(1001, 30, "Obuna bo'ling 👉 @other_kino — eng yaxshi seriallar #reklama", timedelta(hours=50), 7000)
    fake.users["kino_admin"] = types.User(id=777, access_hash=1, first_name="Admin", username="kino_admin")

    async with session() as s:
        acc = Account(phone="+998000", status="active", roles="monitor,outreach,publisher")
        s.add(acc)
        await set_setting(s, "dm_hours", [0, 24])
        await s.commit()
        acc_id = acc.id
    pool.clients[acc_id] = fake

    # --- module 1: search + collect + snowball
    assert await research.search_channels("kino") == 2
    async with session() as s:
        ch = (await s.scalars(select(Channel).where(Channel.username == "kino_uz"))).one()
        ch_id = ch.id
    await research.collect_channel(ch_id)
    async with session() as s:
        ch = await s.get(Channel, ch_id)
        assert ch.status == "collected" and ch.subscribers == 40_000
        assert ch.avg_views and 0.15 < ch.err < 0.3
        assert ch.static["n_posts"] >= 5
        ad = (await s.scalars(select(Post).where(Post.channel_id == ch_id, Post.msg_id == 30))).one()
        assert ad.promotes_channel and ad.ad_targets == ["other_kino"]
        snow = (await s.scalars(select(Channel).where(Channel.username == "other_kino"))).one()
        assert snow.source == "snowball"
    assert await research.rebuild_creatives() >= 1

    # --- module 3: join, detect a new post, sample views, analyze
    await monitor.start_monitoring([ch_id])
    await monitor._join_next()
    async with session() as s:
        ch = await s.get(Channel, ch_id)
        assert ch.joined and ch.tg_id == 1001 and ch.last_msg_id == 30
    assert 1001 in fake.joined

    await monitor.monitor.refresh_watch()
    peers = monitor.monitor.watched[acc_id]
    assert list(peers.values()) == [ch_id]
    fake.add_msg(1001, 31, "Yangi premyera!", timedelta(seconds=20), 120)
    await monitor.monitor._poll_account(acc_id, fake, peers)
    async with session() as s:
        post = (await s.scalars(select(Post).where(Post.channel_id == ch_id, Post.msg_id == 31))).one()
        assert post.tracked and post.next_sample_at is not None
        post_id = post.id
        post.next_sample_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    await monitor.monitor._sample_due()
    # jump to 25h later: views exploded -> late boost
    fake.views[(1001, 31)] = 25_000
    async with session() as s:
        post = await s.get(Post, post_id)
        post.date = utcnow() - timedelta(hours=25)
        # rewrite the first sample as a 30-minute one so the early forecast exists
        smp = (await s.scalars(select(ViewSample).where(ViewSample.post_id == post_id))).one()
        smp.t_sec, smp.views = 1800, 600
        post.next_sample_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    await monitor.monitor._sample_due()
    async with session() as s:
        post = await s.get(Post, post_id)
        assert post.analysis and "late_boost" in post.analysis["flags"]
        ch = await s.get(Channel, ch_id)
        assert ch.verdict in ("unknown", "suspicious")  # 1 post < min_posts_for_verdict
        assert ch.honest_reach and ch.honest_reach < 5000

    # --- module 4: contacts
    await contacts.find_contacts([ch_id])
    async with session() as s:
        c = (await s.scalars(select(Contact).where(Contact.value == "kino_admin"))).one()
        assert c.entity_type == "user" and c.tg_user_id == 777
        assert (await contacts.best_contact(s, ch_id)).id == c.id

    # --- module 5: outreach + reply parsing
    st = await outreach.queue_outreach([ch_id], None, "uz")
    assert st["queued"] == 1
    await outreach._send_next()
    assert fake.sent[-1][0] == "kino_admin" and "@kino_uz" in fake.sent[-1][1]
    await outreach.store_incoming(acc_id, 777, "kino_admin", "1/24 — 300 ming so'm, 1/48 — 450 ming", 5)
    async with session() as s:
        conv = (await s.scalars(select(Conversation).where(Conversation.contact_id == c.id))).one()
        assert conv.status == "replied" and conv.unread == 1
        offers = (await s.scalars(select(Offer).where(Offer.conversation_id == conv.id))).all()
        assert sorted((o.price_uzs, o.fmt) for o in offers) == [(300_000, "1/24"), (450_000, "1/48")]
        assert all(o.cpm_uzs and o.recommendation != "unknown" for o in offers)
        offer_id = min(offers, key=lambda o: o.price_uzs).id
        mc = MyChannel(ref="@my_channel", language="uz")
        s.add(mc)
        await s.commit()
        mc_id = mc.id

    # --- module 6: purchase, link, joins/leaves, ad post
    pid = await tracking.create_purchase(mc_id, ch_id, 300_000, "UZS", "1/24", None, None, "", offer_id)
    async with session() as s:
        link = (await s.scalars(select(InviteLink).where(InviteLink.purchase_id == pid))).one()
        assert link.link == "https://t.me/+TrackHash123"
        assert (await s.get(Channel, ch_id)).status == "approved"
    fake.add_msg(1001, 40, "Zo'r kanal! Obuna: https://t.me/+TrackHash123", timedelta(hours=6), 9000)
    await tracking.sync_my_channel(mc_id)
    assert await tracking.find_ad_post(pid)
    async with session() as s:
        p = await s.get(Purchase, pid)
        assert p.ad_post_id and p.status == "published"
        joins = (await s.scalars(select(Join).where(Join.invite_link_id == link.id))).all()
        assert {j.user_id for j in joins} == {501, 502}
        assert sum(1 for j in joins if j.left_at) == 1
        m = await tracking.metrics(s, p)
        assert m.joins == 2 and m.alive == 1 and m.cps_alive == 300_000 and m.views == 9000

    # --- module 2: donors, best posts, draft, schedule, publish
    await content.add_donors(mc_id, "@kino_uz")
    async with session() as s:
        top = await content.top_posts(s, mc_id)
        assert top and all(not p.is_ad for p, _ in top)
        best_id = top[0][0].id
    item_id = await content.make_draft(mc_id, best_id, "copy")
    when = await content.schedule(item_id)
    assert when > utcnow()
    await content.publish(item_id)
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        assert item.status == "published", item.error
    pool.clients.pop(acc_id, None)


async def test_join_read_contacts_leave_cycle():
    """Catalog channel -> join (biggest first) -> description/contacts right away -> overdue check -> leave."""
    await init_db()
    fake = FakeClient()
    fake.add_channel(2001, "small_uz", "Small", 5_000)
    fake.add_channel(2002, "big_uz", "Big", 900_000, about="Yangiliklar\nReklama: @big_reklama")
    for i in range(6):
        fake.add_msg(2002, 10 + i, f"Xabar {i}", timedelta(hours=30 + i), 90_000)
    async with session() as s:
        acc = Account(phone="+998001", status="active", roles="monitor")
        s.add(acc)
        s.add_all([Channel(username="small_uz", subscribers=5_000, status="monitoring"),
                   Channel(username="big_uz", subscribers=900_000, status="monitoring")])
        await s.commit()
        acc_id = acc.id
    pool.clients[acc_id] = fake
    try:
        await monitor._join_next()
        assert fake.joined == {2002}                       # the biggest one first
        async with session() as s:
            big = (await s.scalars(select(Channel).where(Channel.username == "big_uz"))).one()
            assert big.joined and big.joined_at and big.status == "monitoring"
            assert big.about.startswith("Yangiliklar") and big.avg_views   # read right after the join
            linked = (await s.scalars(select(Contact.value))).all()
            assert "big_reklama" in linked
            big.joined_at = utcnow() - timedelta(days=5)    # a rare poster: the check must not hold the slot
            await s.commit()
        await monitor._join_next()                          # finishes the overdue check, leaves, joins the next
        async with session() as s:
            big = (await s.scalars(select(Channel).where(Channel.username == "big_uz"))).one()
            assert big.status == "checked" and not big.joined
        assert 2001 in fake.joined
    finally:
        pool.clients.pop(acc_id, None)
