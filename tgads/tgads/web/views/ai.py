from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from ...ai.jobs import apply_answer
from ...db import session
from ...models import AiJob
from ..app import back, render

router = APIRouter(prefix="/ai")


@router.get("")
async def index(request: Request, status: str = ""):
    async with session() as s:
        stmt = select(AiJob)
        if status:
            stmt = stmt.where(AiJob.status == status)
        jobs = (await s.scalars(stmt.order_by(AiJob.id.desc()).limit(200))).all()
    return render(request, "ai.html", jobs=jobs, status=status)


@router.get("/{job_id}")
async def job(request: Request, job_id: int):
    async with session() as s:
        j = await s.get(AiJob, job_id)
    return render(request, "ai_job.html", j=j)


@router.post("/{job_id}/answer")
async def answer(job_id: int, text: str = Form(...)):
    async with session() as s:
        j = await s.get(AiJob, job_id)
        await apply_answer(s, j, text)
        await s.commit()
        ok = j.status == "done"
    if ok:
        return back(f"/ai/{job_id}", msg="Ответ принят и применён")
    return back(f"/ai/{job_id}", err="Ответ сохранён, но не разобрался — проверьте формат (нужен JSON, если его просили)")


@router.post("/{job_id}/retry")
async def retry(job_id: int):
    async with session() as s:
        j = await s.get(AiJob, job_id)
        j.status, j.error = "pending", None
        await s.commit()
    return back(f"/ai/{job_id}", msg="Задача снова в очереди")
