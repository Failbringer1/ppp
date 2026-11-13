from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ...db import session
from ...models import Account
from ...services import network, tasks
from ...tg.pool import counter, pool
from ..app import back, render

router = APIRouter(prefix="/accounts")
ROLES = [("monitor", "мониторинг/парсинг"), ("outreach", "переписка"), ("publisher", "публикация/ссылки")]


@router.get("")
async def index(request: Request):
    async with session() as s:
        accounts = (await s.scalars(select(Account).order_by(Account.id))).all()
    rows = [(a, counter(a, "joins"), counter(a, "dms")) for a in accounts]
    return render(request, "accounts.html", rows=rows, online=set(pool.clients), roles=ROLES)


@router.post("/login")
async def login(phone: str = Form(...)):
    try:
        acc_id = await pool.begin_login(phone)
    except Exception as e:
        return back("/accounts", err=f"Не удалось отправить код: {e}")
    if acc_id in pool.clients:
        return back("/accounts", msg="Аккаунт уже авторизован и подключён")
    return back(f"/accounts/{acc_id}/code")


@router.get("/{acc_id}/code")
async def code_form(request: Request, acc_id: int, password: int = 0):
    async with session() as s:
        acc = await s.get(Account, acc_id)
    return render(request, "account_code.html", acc=acc, need_password=bool(password))


@router.post("/{acc_id}/code")
async def code_submit(acc_id: int, code: str = Form(""), password: str = Form("")):
    try:
        res = await pool.complete_login(acc_id, code=code or None, password=password or None)
    except Exception as e:
        return back(f"/accounts/{acc_id}/code{'?password=1' if password else ''}", err=str(e))
    if res == "password":
        return back(f"/accounts/{acc_id}/code?password=1", msg="Нужен облачный пароль (2FA)")
    return back("/accounts", msg="Аккаунт подключён")


@router.post("/{acc_id}/update")
async def update(acc_id: int, roles: list[str] = Form([]), enabled: str = Form("")):
    async with session() as s:
        acc = await s.get(Account, acc_id)
        acc.roles = ",".join(r for r, _ in ROLES if r in roles)
        acc.enabled = bool(enabled)
        if not acc.enabled:
            await pool.stop(acc_id)
        await s.commit()
    return back("/accounts", msg="Сохранено")


@router.post("/{acc_id}/reconnect")
async def reconnect(acc_id: int):
    try:
        await pool.stop(acc_id)
        await pool.start(acc_id)
    except Exception as e:
        return back("/accounts", err=f"Не подключился: {e}")
    return back("/accounts", msg="Подключён")


@router.post("/{acc_id}/clear-cooldown")
async def clear_cooldown(acc_id: int):
    async with session() as s:
        acc = await s.get(Account, acc_id)
        acc.cooldown_until = None
        await s.commit()
    return back("/accounts", msg="Пауза снята")


@router.post("/{acc_id}/network/survey")
async def network_survey(acc_id: int, refs: str = Form(...)):
    """Channels where the account is an admin: rights, description, number of posts, the oldest posts."""
    return JSONResponse(await network.survey(acc_id, refs.split()))


@router.post("/{acc_id}/network/about")
async def network_about(acc_id: int, ref: str = Form(...), about: str = Form(...)):
    try:
        return JSONResponse({"ok": await network.set_about(acc_id, ref, about)})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)


@router.post("/{acc_id}/network/purge")
async def network_purge(acc_id: int, ref: str = Form(...), keep_id: int = Form(...)):
    tasks.spawn(network.purge(acc_id, ref, keep_id), f"Очистка канала {ref}")
    return JSONResponse({"ok": "started"})


@router.post("/{acc_id}/network/post")
async def network_post(acc_id: int, ref: str = Form(...), title: str = Form(...), text: str = Form(...), at: int = Form(0)):
    """Publish a post in a network channel; `at` (unix time) schedules it inside Telegram."""
    try:
        return JSONResponse(await network.post(acc_id, ref, title, text, at or None))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)


@router.post("/{acc_id}/network/scheduled")
async def network_scheduled(acc_id: int, refs: str = Form(...)):
    return JSONResponse(await network.scheduled(acc_id, refs.split()))
