"""Fire-and-forget tasks, held until they finish. The event loop keeps only a weak reference to a task,
so one nobody holds can be garbage-collected mid-run (asyncio docs, create_task), silently dropping a
presence publish or a request close."""

import asyncio
import logging

log = logging.getLogger("voice")
_bg_tasks: set = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("background task failed: %r", t.exception())
    task.add_done_callback(_done)
    return task
