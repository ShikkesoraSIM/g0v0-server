"""In-process queue for admin-triggered recalculations.

Wraps ``tools/recalculate.py`` (the existing CLI) in a single-concurrent
job queue so admins can fire-and-forget a "recalculate user X's PP"
without blocking the request and without two recalcs ever stepping on
each other.

State is in-process by design:
  * recalcs are infrequent admin operations (one per user per session
    at most), not a workhorse pipeline that needs Redis/Celery
  * the existing tools/recalculate.py is a one-shot subprocess that
    already commits its own DB writes — sticking it behind a Redis
    worker would just add a second source of truth for "is anything
    running"

Drawback: with multiple uvicorn workers each worker has its own queue
and concurrent-limit, so two admins on different workers could each
start a recalc at the same moment. Acceptable here — recalculate.py
itself uses a hard-coded internal concurrency limit and the worst
case is doubled DB pressure for a few minutes, not data corruption.
If we ever scale to >1 worker with heavy admin traffic, swap the
deque + Lock here for a Redis-backed queue.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.utils import utcnow


TaskStatus = Literal["pending", "running", "completed", "failed"]
TaskKind = Literal["user_pp"]

# History ring buffer size. We surface this through the status endpoint
# so the admin can see the last few recalcs (success / failure / when).
_HISTORY_LIMIT = 25

# Hard timeout per subprocess. Recalculating a single user is normally
# under a minute; capping at 15 min protects against a runaway job
# wedging the queue forever.
_TASK_TIMEOUT_SEC = 15 * 60


@dataclass(slots=True)
class RecalcTask:
    id: int
    kind: TaskKind
    target_user_id: int
    target_username: str | None
    actor_username: str | None
    enqueued_at: datetime
    status: TaskStatus = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    stdout_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "target_user_id": self.target_user_id,
            "target_username": self.target_username,
            "actor_username": self.actor_username,
            "status": self.status,
            "enqueued_at": self.enqueued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "stdout_tail": self.stdout_tail,
        }


@dataclass(slots=True)
class _State:
    """Module-level singleton holding queue + history. Wrapped in a
    dataclass purely for readability — this is just three buckets of
    state guarded by ``lock``."""

    queue: deque[RecalcTask] = field(default_factory=deque)
    history: deque[RecalcTask] = field(default_factory=lambda: deque(maxlen=_HISTORY_LIMIT))
    current: RecalcTask | None = None
    next_id: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_task: asyncio.Task | None = None


_STATE = _State()


def _resolve_recalc_script() -> Path:
    """Path to tools/recalculate.py relative to the repo root. The
    container layout puts the project at /app, so this resolves to
    /app/tools/recalculate.py at runtime."""
    here = Path(__file__).resolve()  # .../app/service/recalculation_service.py
    return here.parents[2] / "tools" / "recalculate.py"


async def _execute(task: RecalcTask) -> None:
    """Run the per-task subprocess. Captures the last ~3KB of stdout
    so the status endpoint can surface progress / errors without
    flooding the response."""
    task.status = "running"
    task.started_at = utcnow()

    script = _resolve_recalc_script()
    if not script.exists():
        task.status = "failed"
        task.error = f"Recalc script not found at {script}"
        task.completed_at = utcnow()
        return

    cmd = [
        sys.executable,
        str(script),
        "performance",
        "--user-id",
        str(task.target_user_id),
    ]
    # Inherit env so the subprocess sees DATABASE_URL / REDIS_URL / etc.
    env = os.environ.copy()

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            ),
            timeout=30,  # spawn-timeout, not run-timeout
        )
    except Exception as spawn_err:
        task.status = "failed"
        task.error = f"Failed to spawn recalc subprocess: {spawn_err!r} cmd={shlex.join(cmd)}"
        task.completed_at = utcnow()
        return

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_TASK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        task.status = "failed"
        task.error = f"Recalc timed out after {_TASK_TIMEOUT_SEC}s"
        task.completed_at = utcnow()
        return

    text = stdout_bytes.decode("utf-8", errors="replace")
    # Keep just the tail so a chatty subprocess can't blow up the JSON
    # response. 3000 chars is plenty for the last few summary lines.
    task.stdout_tail = text[-3000:]
    task.completed_at = utcnow()
    if proc.returncode == 0:
        task.status = "completed"
    else:
        task.status = "failed"
        task.error = f"Subprocess exited with code {proc.returncode}"


async def _worker_loop() -> None:
    """Pulls one task at a time from the queue and runs it. Sleeps
    briefly when the queue is empty rather than busy-spinning, then
    exits so the next enqueue restarts a fresh loop. This avoids
    keeping a permanent task alive for a feature used a few times
    a day."""
    while True:
        async with _STATE.lock:
            if not _STATE.queue:
                _STATE.current = None
                _STATE.worker_task = None
                return
            task = _STATE.queue.popleft()
            _STATE.current = task

        try:
            await _execute(task)
        except Exception as run_err:  # pragma: no cover — defensive
            task.status = "failed"
            task.error = f"Worker crashed mid-task: {run_err!r}"
            task.completed_at = utcnow()

        async with _STATE.lock:
            _STATE.history.append(task)
            _STATE.current = None


async def enqueue_user_recalc(
    target_user_id: int,
    target_username: str | None,
    actor_username: str | None,
) -> RecalcTask:
    """Add a per-user PP recalc to the queue and start the worker if
    it isn't already running. Returns the task immediately so the
    request can return — the actual subprocess kicks off async."""
    async with _STATE.lock:
        task = RecalcTask(
            id=_STATE.next_id,
            kind="user_pp",
            target_user_id=target_user_id,
            target_username=target_username,
            actor_username=actor_username,
            enqueued_at=utcnow(),
        )
        _STATE.next_id += 1
        _STATE.queue.append(task)
        if _STATE.worker_task is None or _STATE.worker_task.done():
            _STATE.worker_task = asyncio.create_task(_worker_loop())
    return task


async def get_status() -> dict:
    """Snapshot of the queue + currently-running job + recent history
    for the admin status panel. Safe to poll on a short interval —
    holds the lock for microseconds."""
    async with _STATE.lock:
        return {
            "running": _STATE.current.to_dict() if _STATE.current else None,
            "pending": [t.to_dict() for t in _STATE.queue],
            "pending_count": len(_STATE.queue),
            "recent": [t.to_dict() for t in reversed(_STATE.history)],
        }
