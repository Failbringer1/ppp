import json

import httpx
from sqlalchemy import select

from tgads.analysis.tgstat import catalog_rows
from tgads.db import init_db, session
from tgads.models import CatalogChannel, Channel
from tgads.services import catalog
from tgads.web.app import create_app

ITEMS = [
    ["@Kun_UZ", "Kun.uz | Расмий канал", "1 130 559", "270.5k", "4 316", 2, 13, "//static3.tgstat.ru/channels/_100/a.jpg"],
    ["@daryo", "Daryo", "900 000", "120k", "1 900", 2, 3, ""],
    ["AAAAAFkLq7c8VmYzOT1RMg", "Закрытый канал", "15 000", "2.1k", "0", 11, 13, "https://evil.example/x.jpg"],
    ["@bad name", "сломанный", "1", "1", "1", 2, 13, ""],
    ["@kino_uz", "Kino", 50000, 6000.0, None, 999, None, ""],
]


def test_catalog_rows():
    rows = {r.ref: r for r in catalog_rows(ITEMS)}
    assert set(rows) == {"@kun_uz", "@daryo", "AAAAAFkLq7c8VmYzOT1RMg", "@kino_uz"}
    kun = rows["@kun_uz"]
    assert kun.subscribers == 1_130_559 and kun.avg_reach == 270_500 and kun.ci == 4_316
    assert kun.category_id == 2 and kun.language_id == 13 and kun.avatar.startswith("//static3.tgstat.ru/")
    assert rows["AAAAAFkLq7c8VmYzOT1RMg"].avatar == ""          # only TGStat images
    assert rows["@kino_uz"].category_id is None and rows["@kino_uz"].language_id is None


def test_parse_payload_text_plain():
    body = ("d=" + json.dumps({"items": ITEMS[:1]}) + "\r\n").encode()
    assert catalog.parse_payload(body)["items"][0][0] == "@Kun_UZ"


async def test_ingest_page_and_to_work():
    await init_db()
    key = await catalog.ingest_key()
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        body = "d=" + json.dumps({"country": "uz", "items": ITEMS}, ensure_ascii=False) + "\r\n"
        r = await c.post("/catalog/ingest/wrong", content=body.encode())
        assert r.status_code == 403
        r = await c.post(f"/catalog/ingest/{key}", content=body.encode(), headers={"content-type": "text/plain"})
        assert r.status_code == 200 and "новых 4" in r.text, r.text
        # the same rows again only update
        r = await c.post(f"/catalog/ingest/{key}", content=body.encode())
        assert "новых 0, обновлено 4" in r.text

        for url in ("/catalog", "/catalog?category=2", "/catalog?language=13&sort=err", "/catalog?language=0",
                    "/catalog?kind=private", "/catalog?q=kun&min_subs=1000&max_subs=2000000", "/catalog?page=3"):
            r = await c.get(url)
            assert r.status_code == 200, (url, r.text[:2000])
        r = await c.get("/catalog?category=2")
        assert "Kun.uz" in r.text and "Daryo" in r.text and "Kino" not in r.text
        assert "Новости и СМИ" in r.text and "Узбекский" in r.text

        async with session() as s:
            ids = (await s.scalars(select(CatalogChannel.id))).all()
        r = await c.post("/catalog/to-work", data={"ids": [str(i) for i in ids]})
        assert r.status_code == 303
    async with session() as s:
        queued = {ch.username: ch for ch in (await s.scalars(select(Channel))).all()}
    assert set(queued) == {"kun_uz", "daryo", "kino_uz"}       # the private one is skipped
    assert queued["kun_uz"].status == "queued" and queued["kun_uz"].tgstat["category"] == "Новости и СМИ"


async def test_ingest_needs_no_panel_login(monkeypatch):
    monkeypatch.setenv("TGADS_PANEL_PASSWORD", "secret")
    from tgads import config
    config.get_config.cache_clear()
    await init_db()
    key = await catalog.ingest_key()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        assert (await c.get("/catalog")).status_code == 401
        r = await c.post(f"/catalog/ingest/{key}", content=b'd={"items": []}')
        assert r.status_code == 200


async def test_top_goes_to_monitoring_biggest_first():
    await init_db()
    await catalog.ingest({"items": ITEMS})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.post("/catalog/top", data={"n": "2", "category": "2", "language": "-1"})
        assert r.status_code == 303 and r.headers["location"].startswith("/vetting")
    async with session() as s:
        rows = (await s.scalars(select(Channel).order_by(Channel.subscribers.desc().nullslast(), Channel.id))).all()
    # the two biggest public news channels, both waiting for a join
    assert [(ch.username, ch.status) for ch in rows] == [("kun_uz", "monitoring"), ("daryo", "monitoring")]


async def test_geo_and_language_reach_work_channels():
    await init_db()
    # rating rows come without language; the channel is taken to work before the full catalog arrives
    await catalog.ingest({"items": [["@pressauz", "Пресса.uz", "300 000"]]})
    async with session() as s:
        cid = (await s.scalars(select(CatalogChannel.id))).first()
    await catalog.to_work([cid], monitor=True)
    async with session() as s:
        ch = (await s.scalars(select(Channel))).first()
        assert (ch.country, ch.language_id) == ("uz", None)
    await catalog.ingest({"items": [["@pressauz", "Пресса.uz", "300 000", "40k", "900", 2, 3, ""]]})
    async with session() as s:
        ch = (await s.scalars(select(Channel))).first()
        assert (ch.country, ch.language_id) == ("uz", 3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.get("/channels?country=uz&language=3")
        assert r.status_code == 200 and "Пресса.uz" in r.text and "Русский" in r.text
        r = await c.get("/channels?language=13")
        assert "Пресса.uz" not in r.text
        r = await c.get("/catalog?country=uz&language=3")
        assert r.status_code == 200 and "Узбекистан" in r.text


async def test_empty_filters_and_channel_page_with_partial_tgstat():
    from datetime import timedelta

    from tgads.models import Post, ViewSample, utcnow
    await init_db()
    async with session() as s:
        ch = Channel(username="partial_uz", title="Partial", subscribers=1000, status="monitoring", joined=True,
                     tgstat={"subscribers": 1000})          # no avg_reach / err / ci, as copied from the catalog
        s.add(ch)
        await s.flush()
        p = Post(channel_id=ch.id, msg_id=1, date=utcnow() - timedelta(days=2), views=900, tracked=True)
        s.add(p)
        await s.flush()
        for t, v in ((30, 10), (60, 40), (120, 60), (180, 80), (240, 100), (300, 120), (900, 250), (3600, 500), (86400, 880), (172800, 900)):
            s.add(ViewSample(post_id=p.id, t_sec=t, views=v))
        await s.commit()
        ch_id = ch.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        for url in ("/catalog?category=0&language=-1&country=&kind=&min_subs=&max_subs=&q=&sort=subs",
                    "/channels?min_subs=", f"/channels/{ch_id}", "/vetting"):
            r = await c.get(url)
            assert r.status_code == 200, (url, r.text[:2000])
        r = await c.post("/catalog/top", data={"n": "5", "min_subs": "", "max_subs": ""})
        assert r.status_code == 303
        page = (await c.get(f"/channels/{ch_id}")).text
        assert "Просмотры по минутам" in page and "4 мин" in page and "880" in page
        vet = (await c.get("/vetting")).text
        assert "Partial" in vet and "250" in vet and "500" in vet
