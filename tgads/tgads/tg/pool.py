"""Telegram account pool: login flow, connected clients, flood-wait handling."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import select
from telethon import TelegramClient, errors

from ..config import get_config
from ..db import session
from ..models import Account, utcnow

log = logging.getLogger(__name__)
T = TypeVar("T")
TASHKENT = timezone(timedelta(hours=5))


class FloodWait(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"FloodWait {seconds}s")
        self.seconds = seconds


class NoAccount(Exception):
    pass


async def call(fn: Callable[[], Awaitable[T]], max_sleep: int = 30, attempts: int = 3) -> T:
    """Run a Telegram request, sleeping through short flood waits."""
    for i in range(attempts):
        try:
            return await fn()
        except errors.FloodWaitError as e:
            if e.seconds > max_sleep or i == attempts - 1:
                raise FloodWait(e.seconds) from e
            await asyncio.sleep(e.seconds + 1)
    raise AssertionError("unreachable")


def today_key() -> str:
    return datetime.now(TASHKENT).strftime("%Y-%m-%d")


def counter(acc: Account, key: str) -> int:
    return int((acc.counters or {}).get(today_key(), {}).get(key, 0))


def bump(acc: Account, key: str, n: int = 1) -> None:
    day = today_key()
    data = {k: v for k, v in (acc.counters or {}).items() if k == day}  # keep only today
    data.setdefault(day, {})
    data[day][key] = data[day].get(key, 0) + n
    acc.counters = data


class Pool:
    def __init__(self) -> None:
        self.clients: dict[int, TelegramClient] = {}
        self._login: dict[int, tuple[TelegramClient, str]] = {}
        self._installers: list[Callable[[int, TelegramClient], None]] = []

    def on_client(self, fn: Callable[[int, TelegramClient], None]) -> None:
        """Register a hook (e.g. event handlers) for every client that goes online."""
        self._installers.append(fn)
        for acc_id, client in self.clients.items():
            fn(acc_id, client)

    def _new_client(self, account_id: int) -> TelegramClient:
        cfg = get_config()
        if not cfg.tg_api_id or not cfg.tg_api_hash:
            raise RuntimeError("Не заданы TGADS_TG_API_ID / TGADS_TG_API_HASH (my.telegram.org)")
        return TelegramClient(
            str(cfg.sessions_dir / f"acc_{account_id}"), cfg.tg_api_id, cfg.tg_api_hash,
            device_model="TGAds", system_version="Linux", app_version="1.0",
            lang_code="ru", system_lang_code="ru", flood_sleep_threshold=20,
        )

    async def _activate(self, account_id: int, client: TelegramClient) -> None:
        me = await client.get_me()
        async with session() as s:
            acc = await s.get(Account, account_id)
            acc.status = "active"
            acc.tg_user_id = me.id
            acc.username = me.username
            acc.title = " ".join(x for x in (me.first_name, me.last_name) if x) or acc.phone
            acc.last_error = None
            await s.commit()
        self.clients[account_id] = client
        for fn in self._installers:
            try:
                fn(account_id, client)
            except Exception:
                log.exception("client installer failed")

    async def start_all(self) -> None:
        async with session() as s:
            accs = (await s.scalars(select(Account).where(Account.enabled.is_(True),
                                                          Account.status.in_(("active", "error"))))).all()
            ids = [a.id for a in accs]
        for acc_id in ids:
            try:
                await self.start(acc_id)
            except Exception as e:
                log.warning("account %s failed to start: %s", acc_id, e)

    async def start(self, account_id: int) -> TelegramClient:
        if account_id in self.clients and self.clients[account_id].is_connected():
            return self.clients[account_id]
        client = self._new_client(account_id)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            async with session() as s:
                acc = await s.get(Account, account_id)
                acc.status = "error"
                acc.last_error = "Сессия не авторизована — войдите заново"
                await s.commit()
            raise RuntimeError("not authorized")
        await self._activate(account_id, client)
        return client

    async def stop(self, account_id: int) -> None:
        client = self.clients.pop(account_id, None)
        if client:
            await client.disconnect()

    async def stop_all(self) -> None:
        for acc_id in list(self.clients):
            await self.stop(acc_id)

    async def get(self, account_id: int) -> TelegramClient:
        client = self.clients.get(account_id)
        if client is None or not client.is_connected():
            client = await self.start(account_id)
        return client

    async def pick(self, role: str, prefer: int | None = None) -> tuple[int, TelegramClient]:
        """An online, enabled account with the role that is not in flood cooldown."""
        now = utcnow()
        async with session() as s:
            accs = (await s.scalars(select(Account).where(Account.enabled.is_(True),
                                                          Account.status == "active"))).all()
            ok = [a for a in accs if a.has_role(role) and (not a.cooldown_until or a.cooldown_until < now)]
        if prefer is not None:
            ok.sort(key=lambda a: a.id != prefer)
        for a in ok:
            try:
                return a.id, await self.get(a.id)
            except Exception as e:
                log.warning("account %s unavailable: %s", a.id, e)
        raise NoAccount(f"Нет доступного аккаунта с ролью «{role}»")

    async def set_cooldown(self, account_id: int, seconds: int) -> None:
        async with session() as s:
            acc = await s.get(Account, account_id)
            if acc:
                acc.cooldown_until = utcnow() + timedelta(seconds=seconds)
                acc.last_error = f"FloodWait {seconds} с"
                await s.commit()

    # ---- interactive login from the panel ----

    async def begin_login(self, phone: str) -> int:
        phone = phone.strip().replace(" ", "")
        async with session() as s:
            acc = (await s.scalars(select(Account).where(Account.phone == phone))).first()
            if acc is None:
                acc = Account(phone=phone)
                s.add(acc)
                await s.flush()
            acc.status = "code_sent"
            acc.last_error = None
            await s.commit()
            acc_id = acc.id
        await self.stop(acc_id)
        old = self._login.pop(acc_id, None)
        if old:
            await old[0].disconnect()
        client = self._new_client(acc_id)
        await client.connect()
        if await client.is_user_authorized():
            await self._activate(acc_id, client)
            return acc_id
        sent = await client.send_code_request(phone)
        self._login[acc_id] = (client, sent.phone_code_hash)
        return acc_id

    async def complete_login(self, account_id: int, code: str | None = None, password: str | None = None) -> str:
        """Returns 'ok' or 'password' (2FA password required)."""
        if account_id not in self._login:
            raise RuntimeError("Код не запрашивался или процесс перезапускался — начните вход заново")
        client, code_hash = self._login[account_id]
        async with session() as s:
            phone = (await s.get(Account, account_id)).phone
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code.strip(), phone_code_hash=code_hash)
        except errors.SessionPasswordNeededError:
            return "password"
        self._login.pop(account_id, None)
        await self._activate(account_id, client)
        return "ok"


pool = Pool()
