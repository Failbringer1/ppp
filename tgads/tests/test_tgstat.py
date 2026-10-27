from pathlib import Path

from tgads.analysis.tgstat import is_challenge, parse_channel_page, parse_list, parse_number

FIX = Path(__file__).parent / "fixtures"


def test_parse_number():
    assert parse_number("1 027 321") == 1_027_321
    assert parse_number("845,2k") == 845_200
    assert parse_number("1.2 млн") == 1_200_000
    assert parse_number("15.2%") == 0.152
    assert parse_number("1 254,3") == 1254.3
    assert parse_number("1,027,321") == 1_027_321
    assert parse_number("нет") is None


def test_parse_list():
    entries = parse_list((FIX / "tgstat_rating.html").read_text())
    assert [e.ref for e in entries] == ["@kunuzofficial", "@daryo", "AAAAAEcR8TLwDq4mDf2ehg"]
    kun, daryo, private = entries
    assert kun.title == "Kun.uz | Расмий канал" and kun.subscribers == 1_027_321
    assert daryo.title == "Daryo | Rasmiy kanal" and daryo.subscribers == 845_200
    assert private.subscribers == 12_300


def test_parse_channel_page():
    st = parse_channel_page((FIX / "tgstat_channel.html").read_text())
    assert st.ref == "@kunuzofficial"
    assert st.title == "Kun.uz | Расмий канал"
    assert st.subscribers == 1_027_321
    assert st.avg_reach == 156_112
    assert abs(st.err - 0.152) < 1e-9
    assert abs(st.ci - 1254.3) < 1e-6
    assert st.daily_reach == 2_345_678
    assert st.mentions == 3_456
    assert st.category == "Новости и СМИ"
    assert st.warnings and "накрутке" in st.warnings[0]
    assert st.found == 4


def test_challenge_detection():
    assert is_challenge("<title>Just a moment...</title>")
    assert not is_challenge((FIX / "tgstat_channel.html").read_text())


async def test_import_and_enrich(monkeypatch):
    import httpx
    from sqlalchemy import select

    from tgads.db import init_db, session
    from tgads.models import Channel
    from tgads.services import tgstat
    from tgads.web.app import create_app

    await init_db()
    pages = {"rating": (FIX / "tgstat_rating.html").read_text(), "channel": (FIX / "tgstat_channel.html").read_text()}

    async def fake_fetch(url, max_age_hours=12):
        if "/channel/" in url:
            return pages["channel"]
        return pages["rating"] if "page=" not in url else "<html></html>"

    monkeypatch.setattr(tgstat, "fetch", fake_fetch)
    url = await tgstat.rating_url("news")
    assert url == "https://uz.tgstat.com/ratings/channels/news?sort=members"
    res = await tgstat.import_list(url)
    assert "найдено 3" in res
    async with session() as s:
        kun = (await s.scalars(select(Channel).where(Channel.username == "kunuzofficial"))).one()
        assert kun.status == "queued" and kun.source == "tgstat" and kun.subscribers == 1_027_321
        kun_id = kun.id
    assert "1 каналов" in await tgstat.enrich([kun_id])
    async with session() as s:
        kun = await s.get(Channel, kun_id)
        assert kun.tgstat["avg_reach"] == 156_112 and kun.tgstat["warnings"]

    msg = await tgstat.import_html(pages["channel"])
    assert "@kunuzofficial" in msg

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/research")).status_code == 200
        assert (await c.get("/research/tgstat/debug")).status_code == 200
        r = await c.get(f"/channels/{kun_id}")
        assert r.status_code == 200 and "156 112" in r.text and "TGStat" in r.text


async def test_db_adds_missing_columns():
    from sqlalchemy import text

    from tgads.db import engine, init_db

    await init_db()
    async with engine().begin() as conn:
        await conn.execute(text("ALTER TABLE channels DROP COLUMN tgstat"))
    await init_db()
    async with engine().begin() as conn:
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(channels)"))).all()]
    assert "tgstat" in cols
