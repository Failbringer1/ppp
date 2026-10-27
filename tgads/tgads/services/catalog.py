"""TGStat catalog of a country: every channel with subscribers, reach, CI, category and language.

The catalog search of TGStat answers only to a logged-in user, and the panel has no TGStat login.
So the list is collected in the owner's browser (``catalog_collector.js``, run on uz.tgstat.com while
logged in) and posted here in a few big chunks to ``/catalog/ingest/<key>``.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from sqlalchemy import case, func, select

from ..analysis.tgstat import CATALOG_CATEGORIES, CATALOG_LANGUAGES, catalog_rows
from ..db import get_setting, session, set_setting
from ..models import CatalogChannel, utcnow
from .research import add_channel_ref

COLLECTOR_JS = Path(__file__).with_name("catalog_collector.js")
SUB_BUCKETS = [(0, 1_000), (1_000, 5_000), (5_000, 20_000), (20_000, 100_000), (100_000, 500_000), (500_000, None)]


class IngestError(ValueError):
    pass


async def ingest_key() -> str:
    """Secret part of the ingest URL; created on first use and kept in settings."""
    async with session() as s:
        key = await get_setting(s, "catalog_ingest_key")
        if not key:
            key = secrets.token_urlsafe(24)
            await set_setting(s, "catalog_ingest_key", key)
            await s.commit()
    return key


async def check_key(key: str) -> bool:
    async with session() as s:
        stored = await get_setting(s, "catalog_ingest_key")
    return bool(stored) and secrets.compare_digest(str(stored), key)


def parse_payload(body: bytes) -> dict:
    """Body of the collector form: enctype=text/plain gives 'd=<json>\\r\\n'."""
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("d="):
        text = text[2:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise IngestError(f"не JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise IngestError("нет списка items")
    return data


async def ingest(data: dict) -> tuple[int, int]:
    """Upsert rows; returns (added, updated)."""
    rows = catalog_rows(data["items"])
    country = str(data.get("country") or "uz")[:8]
    now = utcnow()
    added = updated = 0
    async with session() as s:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            existing = {c.ref: c for c in (await s.scalars(
                select(CatalogChannel).where(CatalogChannel.ref.in_([r.ref for r in chunk])))).all()}
            for r in chunk:
                c = existing.get(r.ref)
                if c is None:
                    c = CatalogChannel(ref=r.ref, country=country, created_at=now)
                    s.add(c)
                    existing[r.ref] = c
                    added += 1
                else:
                    updated += 1
                c.title = r.title or c.title
                for field in ("subscribers", "avg_reach", "ci", "category_id", "language_id"):
                    v = getattr(r, field)
                    if v is not None:
                        setattr(c, field, v)
                c.avatar = r.avatar or c.avatar
                c.seen_at = now
        await set_setting(s, "catalog_last_ingest", {"at": now.isoformat(timespec="minutes"), "rows": len(rows),
                                                     "added": added, "note": str(data.get("note") or "")[:200]})
        await s.commit()
    return added, updated


def category_name(cat_id: int | None) -> str:
    return CATALOG_CATEGORIES[cat_id][0] if cat_id in CATALOG_CATEGORIES else "—"


def language_name(lang_id: int | None) -> str:
    if lang_id is None:
        return "не определён"
    return CATALOG_LANGUAGES.get(lang_id, f"язык {lang_id}")


def filtered(category: int = 0, language: int = -1, kind: str = "", q: str = "", min_subs: int = 0,
             max_subs: int = 0):
    """Select of catalog rows by the page filters. language: -1 any, 0 not detected."""
    stmt = select(CatalogChannel)
    if category:
        stmt = stmt.where(CatalogChannel.category_id == category)
    if language == 0:
        stmt = stmt.where(CatalogChannel.language_id.is_(None))
    elif language > 0:
        stmt = stmt.where(CatalogChannel.language_id == language)
    if kind == "public":
        stmt = stmt.where(CatalogChannel.ref.like("@%"))
    elif kind == "private":
        stmt = stmt.where(CatalogChannel.ref.not_like("@%"))
    if q:
        like = f"%{q.lower().lstrip('@')}%"
        stmt = stmt.where(func.lower(CatalogChannel.title).like(like) | CatalogChannel.ref.like(like))
    if min_subs:
        stmt = stmt.where(CatalogChannel.subscribers >= min_subs)
    if max_subs:
        stmt = stmt.where(CatalogChannel.subscribers <= max_subs)
    return stmt


async def breakdown(stmt) -> dict:
    """Counts and totals of the filtered rows by category, language and size."""
    sub = stmt.subquery()
    async with session() as s:
        total = await s.scalar(select(func.count()).select_from(sub)) or 0

        async def group(col):
            res = await s.execute(select(col, func.count(), func.sum(sub.c.subscribers), func.avg(sub.c.avg_reach))
                                  .group_by(col).order_by(func.count().desc()))
            return [dict(id=r[0], count=r[1], subs=r[2] or 0, reach=r[3]) for r in res.all()]

        cats = await group(sub.c.category_id)
        langs = await group(sub.c.language_id)
        bucket_col = case(*[(sub.c.subscribers < hi, i) for i, (_, hi) in enumerate(SUB_BUCKETS) if hi],
                          else_=len(SUB_BUCKETS) - 1)
        sizes = dict((await s.execute(select(bucket_col, func.count()).where(sub.c.subscribers.is_not(None))
                                      .group_by(bucket_col))).all())
        last = await get_setting(s, "catalog_last_ingest")
    for c in cats:
        c["name"] = category_name(c["id"])
    for lang in langs:
        lang["name"] = language_name(lang["id"])
    size_rows = [dict(label=_bucket_label(lo, hi), count=sizes.get(i, 0)) for i, (lo, hi) in enumerate(SUB_BUCKETS)]
    return dict(total=total, categories=cats, languages=langs, sizes=size_rows, last=last)


def _bucket_label(lo: int, hi: int | None) -> str:
    def k(n: int) -> str:
        return f"{n // 1000}k" if n >= 1000 else str(n)
    return f"от {k(lo)}" if hi is None else f"{k(lo)}–{k(hi)}"


async def top_ids(stmt, n: int) -> list[int]:
    """Ids of the n biggest public channels of the filtered selection."""
    sub = stmt.where(CatalogChannel.ref.like("@%")).order_by(CatalogChannel.subscribers.desc().nullslast()).limit(n)
    async with session() as s:
        return [c.id for c in (await s.scalars(sub)).all()]


async def to_work(ids: list[int], monitor: bool = False) -> int:
    """Put selected public catalog channels into the work queue.

    monitor=False: collection by a Telegram account; monitor=True: a monitor account joins the channel
    (biggest first, within the daily join limit) and the bot check of module 3 starts.
    """
    n = 0
    async with session() as s:
        rows = (await s.scalars(select(CatalogChannel).where(CatalogChannel.id.in_(ids)))).all()
        for c in rows:
            if not c.is_public:
                continue
            ch = await add_channel_ref(s, c.ref, "tgstat", "каталог TGStat", status="queued")
            if ch is None:
                continue
            if monitor and ch.status not in ("monitoring", "checked", "approved"):
                ch.status = "monitoring"
            ch.title = ch.title or c.title
            ch.subscribers = c.subscribers or ch.subscribers
            data = dict(ch.tgstat or {})
            data.update({k: v for k, v in (("subscribers", c.subscribers), ("avg_reach", c.avg_reach), ("ci", c.ci),
                                           ("category", category_name(c.category_id))) if v not in (None, "—")})
            ch.tgstat = data
            n += 1
        await s.commit()
    return n


def collector_source(ingest_url: str) -> str:
    return COLLECTOR_JS.read_text(encoding="utf-8").replace("__INGEST_URL__", ingest_url)
