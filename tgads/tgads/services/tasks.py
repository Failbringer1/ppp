"""Fire-and-forget background tasks started from the panel, with visible status."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable

from ..models import utcnow

log = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    id: int
    name: str
    started: datetime = field(default_factory=utcnow)
    finished: datetime | None = None
    result: str = ""
    error: str = ""
    task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.finished is None


_tasks: dict[int, TaskInfo] = {}
_seq = 0


def spawn(coro: Awaitable, name: str) -> TaskInfo:
    global _seq
    _seq += 1
    info = TaskInfo(id=_seq, name=name)

    async def runner() -> None:
        try:
            res = await coro
            info.result = "" if res is None else str(res)
        except Exception as e:
            log.exception("task %s failed", name)
            info.error = str(e) or type(e).__name__
        finally:
            info.finished = utcnow()

    info.task = asyncio.create_task(runner())
    _tasks[info.id] = info
    # keep the last 50
    for old in sorted(_tasks)[:-50]:
        if not _tasks[old].running:
            del _tasks[old]
    return info


def recent() -> list[TaskInfo]:
    return sorted(_tasks.values(), key=lambda t: t.id, reverse=True)[:15]
