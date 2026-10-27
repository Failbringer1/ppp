"""AI job queue: every AI call is a row in ai_jobs; a handler applies the answer."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session
from ..models import AiJob, utcnow
from . import provider

log = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, AiJob, str], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


async def enqueue(s: AsyncSession, kind: str, title: str, system_prompt: tuple[str, str],
                  ref_id: int | None = None, params: dict | None = None) -> AiJob:
    system, prompt = system_prompt
    job = AiJob(kind=kind, title=title, system=system, prompt=prompt, ref_id=ref_id, params=params or {})
    s.add(job)
    await s.flush()
    return job


async def apply_answer(s: AsyncSession, job: AiJob, text: str) -> None:
    job.result = text
    fn = HANDLERS.get(job.kind)
    try:
        if fn:
            await fn(s, job, text)
        job.status = "done"
        job.error = None
    except Exception as e:  # handler failed to parse the answer: keep it for a retry/manual fix
        log.exception("AI handler %s failed", job.kind)
        job.status = "error"
        job.error = f"Ошибка обработки ответа: {e}"
    job.finished_at = utcnow()


async def _run_one(job_id: int) -> None:
    async with session() as s:
        job = await s.get(AiJob, job_id)
        if job is None:
            return
        system, prompt = job.system, job.prompt
    try:
        text = await provider.complete(system, prompt)
    except provider.ManualAnswerRequired:
        async with session() as s:
            job = await s.get(AiJob, job_id)
            job.status = "manual"
            await s.commit()
        return
    except Exception as e:
        log.warning("AI job %s failed: %s", job_id, e)
        async with session() as s:
            job = await s.get(AiJob, job_id)
            job.status = "error"
            job.error = str(e)[:2000]
            job.finished_at = utcnow()
            await s.commit()
        return
    async with session() as s:
        job = await s.get(AiJob, job_id)
        await apply_answer(s, job, text)
        await s.commit()


async def worker_loop(concurrency: int = 2) -> None:
    running: set[asyncio.Task] = set()
    while True:
        try:
            running = {t for t in running if not t.done()}
            free = concurrency - len(running)
            if free > 0:
                async with session() as s:
                    jobs = (await s.scalars(
                        select(AiJob).where(AiJob.status == "pending").order_by(AiJob.id).limit(free))).all()
                    for j in jobs:
                        j.status = "running"
                    await s.commit()
                    ids = [j.id for j in jobs]
                for jid in ids:
                    running.add(asyncio.create_task(_run_one(jid)))
        except Exception:
            log.exception("AI worker loop error")
        await asyncio.sleep(3)


async def reset_stuck() -> None:
    """Jobs left 'running' by a crashed process go back to the queue."""
    async with session() as s:
        for j in (await s.scalars(select(AiJob).where(AiJob.status == "running"))).all():
            j.status = "pending"
        await s.commit()
