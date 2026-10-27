"""FastAPI app: panel pages + background worker in the lifespan."""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import worker
from ..ai.provider import resolve_mode
from ..config import get_config
from ..db import init_db
from ..models import utcnow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

LOCAL_OFFSET = timedelta(hours=5)  # Asia/Tashkent, no DST


def _num(v, digits: int = 0) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _pct(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _local(dt: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    return (dt + LOCAL_OFFSET).strftime(fmt) if dt else "—"


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    sec = (utcnow() - dt).total_seconds()
    if sec < 90:
        return f"{int(sec)} с назад"
    if sec < 5400:
        return f"{int(sec / 60)} мин назад"
    if sec < 172800:
        return f"{int(sec / 3600)} ч назад"
    return f"{int(sec / 86400)} дн назад"


def _short(text: str | None, n: int = 120) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


templates.env.filters.update(num=_num, pct=_pct, local=_local, ago=_ago, short=_short)

VERDICTS = {"unknown": ("нет данных", "muted"), "clean": ("чистый", "ok"),
            "suspicious": ("подозрительный", "warn"), "boosted": ("накрутка", "bad")}
templates.env.globals.update(VERDICTS=VERDICTS)


def to_utc(local_str: str | None) -> datetime | None:
    """'2026-10-27T19:00' (Tashkent, from <input type=datetime-local>) -> naive UTC."""
    if not local_str:
        return None
    return datetime.fromisoformat(local_str) - LOCAL_OFFSET


def render(request: Request, name: str, **ctx) -> Response:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx["ai_mode"] = resolve_mode()
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if get_config().run_worker:
        await worker.start()
    yield
    await worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="TGAds", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        cfg = get_config()
        if cfg.panel_password:
            ok = False
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, cfg.panel_user) and secrets.compare_digest(pwd, cfg.panel_password)
                except Exception:
                    ok = False
            if not ok:
                return Response("Auth required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="tgads"'})
        return await call_next(request)

    from .views import accounts, ai, channels, content, dashboard, outreach, purchases, research, settings
    for mod in (dashboard, accounts, research, channels, content, outreach, purchases, ai, settings):
        app.include_router(mod.router)
    return app
