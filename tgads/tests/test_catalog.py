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
