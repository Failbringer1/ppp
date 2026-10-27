"""Background loops that run next to the web panel in one process."""
from __future__ import annotations

import asyncio
import logging

from .ai import jobs
from .services import content, monitor, outreach, research, tracking
from .tg.pool import pool

log = logging.getLogger(__name__)

_tasks: list[asyncio.Task] = []


async def _curves_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await monitor.rebuild_curves()
        except Exception:
            log.exception("curves rebuild")


async def start() -> None:
    await jobs.reset_stuck()
    pool.on_client(monitor.monitor.install)
    pool.on_client(outreach.install)
    try:
        await pool.start_all()
    except Exception:
        log.exception("starting Telegram accounts")
    loops = [
        jobs.worker_loop(),
        research.collector_loop(),
        monitor.monitor.poll_loop(),
        monitor.monitor.sample_loop(),
        monitor.join_loop(),
        outreach.sender_loop(),
        content.publisher_loop(),
        tracking.tracking_loop(),
        _curves_loop(),
    ]
    _tasks.extend(asyncio.create_task(c) for c in loops)


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    _tasks.clear()
    await pool.stop_all()
