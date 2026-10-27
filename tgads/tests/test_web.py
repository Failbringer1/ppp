"""Smoke test: seed the DB with fake data and render every page (no Telegram needed)."""
import json
from datetime import timedelta

import pytest

import httpx

from tgads.analysis.views import CHECKPOINTS, Curve
from tgads.db import init_db, session
from tgads.models import (
    Account, AiJob, Channel, ChannelContact, Contact, ContentItem, Conversation, Donor, InviteLink, Join, Message,
    MyChannel, MyCreative, Post, Purchase, ViewSample, utcnow,
)
from tgads.services import monitor, research
from tgads.web.app import create_app


async def seed() -> dict:
    await init_db()
    now = utcnow()
    prior = Curve.prior()
    async with session() as s:
        acc = Account(phone="+998900000000", title="Test", status="active")
        s.add(acc)
        clean = Channel(username="clean_uz", title="Чистый канал", subscribers=40_000, topic="кино/сериалы",
                        status="monitoring", joined=True, about="Kino kanali\nReklama: @kino_admin", source="search")
        boosted = Channel(username="boost_uz", title="Накрученный", subscribers=50_000, topic="кино/сериалы",
                          status="monitoring", joined=True, source="snowball")
        news = Channel(username="news_uz", title="Новости", subscribers=200_000, topic="новости", status="collected",
                       avg_views=30_000, err=0.15, ad_share=0.1)
        s.add_all([clean, boosted, news])
        await s.flush()
        for ch, extra in ((clean, 0), (boosted, 9_000)):
            for i in range(4):
                p = Post(channel_id=ch.id, msg_id=100 + i, date=now - timedelta(days=3, hours=i), text=f"post {i}",
                         views=8_000 + extra, forwards=50)
                s.add(p)
                await s.flush()
                for t in CHECKPOINTS[:-1]:
                    v = int(3_000 * prior.share(t)) + (extra if t >= 10_800 else 0)
                    s.add(ViewSample(post_id=p.id, t_sec=t, views=v, forwards=10))
        text = "Eng zo'r kinolar faqat bizda! Har kuni premyera. Obuna bo'ling 👉 t.me/+Kino123abcd #reklama"
        for i, host in enumerate((clean, boosted, news)):
            s.add(Post(channel_id=host.id, msg_id=500 + i, date=now - timedelta(days=10 - i * 3), text=text,
                       views=5_000, is_ad=True, promotes_channel=True, ad_targets=["+Kino123abcd"]))
        mc = MyChannel(ref="@my_kino", title="Мой кино-канал", language="uz", topic="кино/сериалы")
        s.add(mc)
        await s.flush()
        s.add(Donor(my_channel_id=mc.id, channel_id=clean.id))
        cr = MyCreative(my_channel_id=mc.id, title="A", text="Kino! {link}")
        s.add(cr)
        contact = Contact(kind="username", value="kino_admin", entity_type="user")
        s.add(contact)
        await s.flush()
        s.add(ChannelContact(channel_id=clean.id, contact_id=contact.id, ad_context=True, priority=8))
        conv = Conversation(contact_id=contact.id, account_id=acc.id, my_channel_id=mc.id, channel_ids=[clean.id],
                            status="replied", unread=1, first_text="Salom")
        s.add(conv)
        await s.flush()
        s.add_all([Message(conversation_id=conv.id, direction="out", text="Salom, reklama narxi?"),
                   Message(conversation_id=conv.id, direction="in", text="1/24 — 250 ming so'm, 1/48 — 350 ming")])
        pur = Purchase(my_channel_id=mc.id, channel_id=clean.id, amount=250_000, price_uzs=250_000, creative_id=cr.id,
                       scheduled_at=now - timedelta(days=2), status="published")
        s.add(pur)
        await s.flush()
        link = InviteLink(my_channel_id=mc.id, purchase_id=pur.id, link="https://t.me/+TrackLink1", usage=3)
        s.add(link)
        await s.flush()
        s.add_all([Join(invite_link_id=link.id, user_id=i, joined_at=now - timedelta(days=2),
                        left_at=(now - timedelta(days=1)) if i == 1 else None) for i in range(1, 4)])
        s.add(ContentItem(my_channel_id=mc.id, text="draft text", source="manual"))
        s.add(AiJob(kind="channel_ideas", title="Идеи", prompt="p", status="done", result="# Идея 1"))
        await s.commit()
        ids = dict(clean=clean.id, boosted=boosted.id, news=news.id, mc=mc.id, conv=conv.id, pur=pur.id, acc=acc.id)
    async with session() as s:
        for cid in (ids["clean"], ids["boosted"]):
            await monitor.analyze_channel(s, await s.get(Channel, cid))
        await s.commit()
    await research.rebuild_creatives()
    return ids


@pytest.fixture
async def client_ids():
    ids = await seed()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, ids


async def test_pages_and_forms(client_ids):
    c, ids = client_ids
    pages = ["/", "/accounts", "/research", "/channels", "/channels?status=monitoring&sort=reach",
             f"/channels/{ids['clean']}", f"/channels/{ids['boosted']}", "/vetting", "/vetting?show=all",
             "/content", f"/content/{ids['mc']}", "/contacts", "/contacts?only_ads=1", "/outreach",
             f"/outreach/{ids['conv']}", "/purchases", f"/purchases/{ids['pur']}", "/ai", "/settings"]
    for url in pages:
        r = await c.get(url)
        assert r.status_code == 200, (url, r.text[:2000])

    # verdicts computed from the seeded view curves
    async with session() as s:
        clean = await s.get(Channel, ids["clean"])
        boosted = await s.get(Channel, ids["boosted"])
        assert clean.verdict == "clean", clean.reasons
        assert boosted.verdict == "boosted", boosted.reasons
        assert boosted.honest_reach < 5_000

    r = await c.get("/research")
    assert "Kino123abcd" in r.text  # reused creative is listed

    # settings round trip
    r = await c.post("/settings", data={"usd_uzs": "12800", "target_cpm_uzs": "25000", "tpl_uz": "Salom {channel}",
                                        "tpl_ru": "Привет {channel}", "dm_from": "10", "dm_to": "20",
                                        "th_late_boost_ratio": "2.5"})
    assert r.status_code == 303
    r = await c.get("/settings")
    assert "12800" in r.text and "2.5" in r.text

    # parse prices from the admin reply (regex + manual AI answer)
    r = await c.post(f"/outreach/{ids['conv']}/parse")
    assert r.status_code == 303
    async with session() as s:
        from sqlalchemy import select
        job = (await s.scalars(select(AiJob).where(AiJob.kind == "parse_offer"))).first()
    answer = json.dumps({"offers": [{"channel": "@clean_uz", "amount": 250000, "currency": "UZS", "format": "1/24"}],
                         "status": "price_given", "summary": "ok"})
    r = await c.post(f"/ai/{job.id}/answer", data={"text": answer})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    r = await c.get(f"/outreach/{ids['conv']}")
    assert "250 000" in r.text

    # topics via manual AI answer
    r = await c.post("/channels/bulk", data={"action": "classify", "ids": [str(ids["news"])]})
    assert r.status_code == 303
    async with session() as s:
        job = (await s.scalars(select(AiJob).where(AiJob.kind == "classify_topics"))).first()
    r = await c.post(f"/ai/{job.id}/answer", data={"text": json.dumps({str(ids["news"]): "футбол/спорт"})})
    async with session() as s:
        assert (await s.get(Channel, ids["news"])).topic == "футбол/спорт"

    # import + manual content
    r = await c.post("/research/import", data={"refs": "@new_one https://t.me/another_one"})
    assert r.status_code == 303
    r = await c.post(f"/content/{ids['mc']}/manual", data={"text": "hello"})
    assert r.status_code == 303
    r = await c.post("/research/ideas", data={"wishes": "кино"})
    assert r.status_code == 303
    r = await c.get(r.headers["location"])
    assert r.status_code == 200
