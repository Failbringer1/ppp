from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from ...config import get_config
from ...db import session
from ...models import Account, AiJob, Channel, Conversation, Purchase
from ...services import tasks
from ...services.tracking import metrics
from ...tg.pool import pool
from ..app import render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    cfg = get_config()
    async with session() as s:
        by_status = dict((await s.execute(select(Channel.status, func.count()).group_by(Channel.status))).all())
        by_verdict = dict((await s.execute(select(Channel.verdict, func.count())
                                           .where(Channel.status.in_(("monitoring", "checked", "approved")))
                                           .group_by(Channel.verdict))).all())
        accounts = (await s.scalars(select(Account))).all()
        unread = await s.scalar(select(func.coalesce(func.sum(Conversation.unread), 0)))
        conv_by_status = dict((await s.execute(select(Conversation.status, func.count())
                                               .group_by(Conversation.status))).all())
        manual_jobs = await s.scalar(select(func.count()).select_from(AiJob).where(AiJob.status == "manual"))
        purchases = (await s.scalars(select(Purchase).where(Purchase.status != "cancelled"))).all()
        spent = sum(p.price_uzs or 0 for p in purchases)
        joins = 0
        alive = 0
        for p in purchases:
            m = await metrics(s, p)
            joins += m.joins
            alive += m.alive
    warnings = []
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        warnings.append("Не заданы TGADS_TG_API_ID и TGADS_TG_API_HASH в .env — без них Telegram не подключится "
                        "(получить на my.telegram.org → API development tools).")
    if not accounts:
        warnings.append("Нет ни одного Telegram-аккаунта — добавьте на странице «Аккаунты».")
    if not cfg.panel_password:
        warnings.append("Панель без пароля (TGADS_PANEL_PASSWORD). На VPS обязательно задайте пароль.")
    return render(request, "dashboard.html", by_status=by_status, by_verdict=by_verdict, accounts=accounts,
                  online=set(pool.clients), unread=unread, conv_by_status=conv_by_status,
                  manual_jobs=manual_jobs, spent=spent, joins=joins, alive=alive, n_purchases=len(purchases),
                  warnings=warnings, tasks=tasks.recent())
