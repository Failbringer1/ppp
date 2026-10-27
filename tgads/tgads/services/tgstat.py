"""TGStat scraper: ratings/catalog pages -> new channels, channel pages -> stats.

Polite by design: one request at a time, 4-9 s between requests, a daily page
limit and a 12 h cache. When Cloudflare answers with a challenge, the page is
fetched through a headless Chromium (Playwright) if it is installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx
from sqlalchemy import select

from ..analysis.tgstat import ChannelStats, is_challenge, parse_channel_page, parse_list
from ..config import get_config
from ..db import get_setting, get_settings, session, set_setting
from ..models import Channel, utcnow
from ..tg.pool import today_key
from .research import add_channel_ref

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8,uz;q=0.7",
}

_lock = asyncio.Lock()
_last_request = 0.0


class TgstatError(Exception):
    pass


def _dir() -> Path:
    d = get_config().data_dir / "tgstat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(url: str) -> Path:
    return _dir() / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".html")


def with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q["page"] = str(page)
    return urlunparse(parts._replace(query=urlencode(q)))


async def _count_page() -> None:
    async with session() as s:
        limit = int(await get_setting(s, "tgstat_pages_per_day") or 300)
        counter = await get_setting(s, "tgstat_counter") or {}
        n = int(counter.get(today_key(), 0))
        if n >= limit:
            raise TgstatError(f"Дневной лимит страниц TGStat исчерпан ({limit}), продолжу завтра")
        await set_setting(s, "tgstat_counter", {today_key(): n + 1})
        await s.commit()


async def fetch(url: str, max_age_hours: float = 12) -> str:
    """Page HTML from cache or the network. Raises TgstatError when blocked."""
    global _last_request
    path = _cache_path(url)
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return path.read_text(encoding="utf-8", errors="replace")
    async with _lock:
        wait = _last_request + random.uniform(4, 9) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        await _count_page()
        page, status = "", 0
        if get_config().tgstat_browser != "1":
            try:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as c:
                    r = await c.get(url)
                page, status = r.text, r.status_code
            except httpx.HTTPError as e:
                log.info("tgstat http error %s: %s", url, e)
            finally:
                _last_request = time.monotonic()
        if status in (403, 429, 503) or is_challenge(page) or not page:
            page = await _browser_fetch(url)
            _last_request = time.monotonic()
        elif status >= 400:
            raise TgstatError(f"TGStat ответил {status} на {url}")
    path.write_text(page, encoding="utf-8")
    return page


async def _browser_fetch(url: str) -> str:
    mode = get_config().tgstat_browser
    if mode == "0":
        raise TgstatError("TGStat закрыт проверкой Cloudflare, а браузерный режим выключен (TGADS_TGSTAT_BROWSER=0)")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise TgstatError("TGStat закрыт проверкой Cloudflare. Нужен браузерный режим: "
                          "pip install playwright && playwright install --with-deps chromium") from None
    async with async_playwright() as p:
        # persistent profile keeps the Cloudflare clearance cookie between runs
        ctx = await p.chromium.launch_persistent_context(
            str(_dir() / "browser"), headless=True, locale="ru-RU", user_agent=HEADERS["User-Agent"],
            args=["--disable-blink-features=AutomationControlled"])
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(25):
                html = await page.content()
                if not is_challenge(html):
                    return html
                await asyncio.sleep(1.5)
            raise TgstatError("Cloudflare не пропустил даже браузер. Попробуйте позже или вставьте HTML страницы вручную.")
        finally:
            await ctx.close()


async def _base() -> str:
    async with session() as s:
        return (await get_setting(s, "tgstat_base") or "https://uz.tgstat.com").rstrip("/")


async def rating_url(category: str = "", sort: str = "members") -> str:
    base = await _base()
    path = "/ratings/channels" + (f"/{category}" if category else "")
    return f"{base}{path}?sort={sort}" if sort else base + path


async def import_list(url: str, max_pages: int = 3) -> str:
    """Scrape a ratings / catalog / search page and queue every channel on it."""
    seen: set[str] = set()
    added = updated = 0
    for n in range(1, max_pages + 1):
        page = await fetch(with_page(url, n))
        entries = [e for e in parse_list(page) if e.ref.lower() not in seen]
        if not entries:
            break
        a, u = await _store_entries(entries, url)
        added, updated = added + a, updated + u
        seen |= {e.ref.lower() for e in entries}
    if not seen:
        raise TgstatError("На странице не нашлось ни одного канала — проверьте адрес или откройте «Отладка TGStat»")
    return f"TGStat: найдено {len(seen)} каналов, новых {added}, обновлено {updated}"


async def _store_entries(entries, source_url: str) -> tuple[int, int]:
    added = updated = 0
    async with session() as s:
        for e in entries:
            if not e.ref.startswith("@"):
                continue  # private channel without a username: TGStat id is useless for Telegram
            existing = (await s.scalars(select(Channel).where(Channel.username == e.ref[1:]))).first()
            ch = existing or await add_channel_ref(s, e.ref, "tgstat", source_url, status="queued")
            if ch is None:
                continue
            added += existing is None
            updated += existing is not None
            if e.title and not ch.title:
                ch.title = e.title
            if e.subscribers and not ch.subscribers:
                ch.subscribers = int(e.subscribers)
            data = dict(ch.tgstat or {})
            data.update({k: v for k, v in (("subscribers", e.subscribers), ("title", e.title)) if v})
            ch.tgstat = data
        await s.commit()
    return added, updated


def _apply_stats(ch: Channel, st: ChannelStats, url: str) -> None:
    data = {k: v for k, v in st.to_json().items() if v not in (None, "", [])}
    data.update(url=url, fetched_at=utcnow().isoformat(timespec="minutes"))
    ch.tgstat = data
    if st.title and not ch.title:
        ch.title = st.title
    if st.subscribers and not ch.subscribers:
        ch.subscribers = int(st.subscribers)


async def enrich(channel_ids: list[int]) -> str:
    """Download TGStat stat pages of channels (only those with a public @username)."""
    base = await _base()
    async with session() as s:
        rows = [(c.id, c.username) for c in (await s.scalars(select(Channel).where(Channel.id.in_(channel_ids)))).all()
                if c.username]
    ok = empty = 0
    for ch_id, uname in rows:
        url = f"{base}/channel/@{uname}/stat"
        try:
            page = await fetch(url)
        except TgstatError as e:
            return f"Обновлено {ok} из {len(rows)}; остановлено: {e}"
        st = parse_channel_page(page)
        async with session() as s:
            ch = await s.get(Channel, ch_id)
            _apply_stats(ch, st, url)
            await s.commit()
        if st.found:
            ok += 1
        else:
            empty += 1
    return f"Данные TGStat: {ok} каналов" + (f", без цифр: {empty} (см. «Отладка TGStat»)" if empty else "")


async def import_html(page: str, source: str = "вставка") -> str:
    """HTML pasted by the user (saved from the browser): a list page or a channel page."""
    entries = parse_list(page)
    st = parse_channel_page(page)
    if st.found >= 2 and st.ref and st.ref.startswith("@"):
        async with session() as s:
            ch = await add_channel_ref(s, st.ref, "tgstat", source, status="queued")
            _apply_stats(ch, st, source)
            await s.commit()
        return f"Канал {st.ref}: подписчики {st.subscribers}, охват {st.avg_reach}, ERR {st.err}, ИЦ {st.ci}"
    if not entries:
        raise TgstatError("В HTML не нашлось каналов TGStat")
    added, updated = await _store_entries(entries, source)
    return f"Из HTML: каналов {len(entries)}, новых {added}, обновлено {updated}"


def debug_pages(limit: int = 15) -> list[dict]:
    """Recently downloaded pages and what the parsers see in them."""
    out = []
    files = sorted(_dir().glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for f in files:
        page = f.read_text(encoding="utf-8", errors="replace")
        st = parse_channel_page(page)
        title = st.title or f.name
        out.append({"file": f.name, "size": len(page), "mtime": f.stat().st_mtime, "title": title,
                    "challenge": is_challenge(page), "channels": len(parse_list(page)), "stats": st.to_json()})
    return out


async def settings_snapshot() -> dict:
    async with session() as s:
        st = await get_settings(s)
    return {"base": st.get("tgstat_base"), "limit": st.get("tgstat_pages_per_day"),
            "today": (st.get("tgstat_counter") or {}).get(today_key(), 0)}
