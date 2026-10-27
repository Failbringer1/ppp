"""Engine, sessions and key-value settings."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_config
from .models import Base, Setting

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

DEFAULT_SETTINGS: dict[str, Any] = {
    # money
    "usd_uzs": 12_700.0,
    "rub_uzs": 150.0,
    "target_cpm_uzs": 20_000.0,
    # module 3: bot detection thresholds (see analysis.views.Thresholds)
    "thresholds": {},
    "min_posts_for_verdict": 3,
    "posts_to_track_per_channel": 5,
    "auto_leave_after_check": True,
    # account limits
    "joins_per_day": 30,
    "dms_per_day": 15,
    "dm_delay_min": 60,
    "dm_delay_max": 240,
    "dm_hours": [9, 21],          # Tashkent working hours for outreach
    "collect_delay_sec": 8,       # pause between channel collections
    "poll_interval_sec": 20,      # getPeerDialogs fallback poll
    # module 5 templates; {a|b} picks a random variant
    "outreach_templates": {
        "uz": "{Assalomu alaykum|Salom|Assalomu alaykum, yaxshimisiz}! {channel} kanalingizda reklama "
              "{narxi qancha|qancha turadi}? {1/24 va 1/48 formatlar qiziq|Qaysi formatlar bor}. "
              "{Rahmat|Oldindan rahmat}!",
        "ru": "{Здравствуйте|Добрый день|Привет}! {Сколько стоит|Подскажите цену на} рекламу в {channel}? "
              "{Интересуют форматы 1/24 и 1/48|Какие форматы есть}? {Спасибо|Заранее спасибо}!",
    },
    "outreach_language": "uz",
    "search_keywords": "yangiliklar, kino, futbol, ish, ielts, retsept, avto, uzum, biznes, kripto, islom, "
                       "новости ташкент, работа ташкент, кино uz, футбол uz",
    "tgstat_base": "https://uz.tgstat.com",
    "tgstat_pages_per_day": 300,
    "curve_day": None,
    "curve_night": None,
}


def engine():
    global _engine, _sessionmaker
    if _engine is None:
        cfg = get_config()
        _engine = create_async_engine(cfg.db_url, connect_args={"timeout": 30})

        @event.listens_for(_engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def init_db() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:  # noqa: ANN001
    """Tiny migration: columns added to models after the DB was created become nullable columns."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(conn.dialect)}')


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as s:
        yield s


async def get_setting(s: AsyncSession, key: str) -> Any:
    row = await s.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return row.value


async def get_settings(s: AsyncSession) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in (await s.scalars(select(Setting))).all():
        out[row.key] = row.value
    return out


async def set_setting(s: AsyncSession, key: str, value: Any) -> None:
    row = await s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value
