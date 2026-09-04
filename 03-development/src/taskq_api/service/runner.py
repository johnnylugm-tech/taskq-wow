"""[FR-02] Service layer for task execution — state machine + subprocess runner.

This module owns the FR-02 state machine
(``pending → running → done | failed | timeout``), spawns subprocesses via
``asyncio.create_subprocess_exec(*shlex.split(command))``, and persists the
finished result to ``task_results`` through the repository layer.

The runner is the FR-02 service boundary: routers in ``taskq_api.api.runs``
stay thin and call ``enqueue_run`` (202 path) or trigger ``run_task``
directly (test-deterministic path). Background scheduling lives here, not
in the router, per SAD §3.2.

Citations:
- SPEC.md line 95 — POST /v1/tasks/{id}/run (scope write) → 202 + run_id.
- SPEC.md line 96 — ``asyncio.create_subprocess_exec(*shlex.split(command))``;
  shell-mode kwarg forbidden; timeout is ``TASKQ_TASK_TIMEOUT``.
- SPEC.md line 97 — state machine pending → running → done | failed | timeout.
- SPEC.md line 98 — results written to ``task_results`` (FR-07 v3 schema).
- SPEC.md line 149 — ``process.kill()`` then ``await process.wait()``; no orphans.
- SPEC.md §8 #16 — grep gate, 0 命中 for the forbidden shell kwarg.
- SAD.md §2.5/§2.6/§3.2 — runner module responsibilities.
- TEST_SPEC.md §1 FR-02 — six named cases.
- NFR-09 — observable state machine via ``VALID_STATES`` / ``ALLOWED_TRANSITIONS``.
- NFR-11 — service stays small; routing decisions in api.runs.
"""  # NFR-11
from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from taskq_api import config
from taskq_api.repository import results as repo_results
from taskq_api.repository import tasks as repo_tasks


# SPEC.md line 97 — the five declared states of the task state machine.
VALID_STATES: frozenset[str] = frozenset(
    {"pending", "running", "done", "failed", "timeout"}
)

# SPEC.md line 97 — declared edges of the state machine. ``pending`` has
# exactly one outgoing edge (``running``); ``running`` fans out to the
# three terminal outcomes; terminals have no outgoing edges.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"done", "failed", "timeout"}),
    "done": frozenset(),
    "failed": frozenset(),
    "timeout": frozenset(),
}


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 with microsecond precision.

    Citations: SPEC.md §5.3 — ``task_results.finished_at`` timestamp column.
    """
    return datetime.now(timezone.utc).isoformat()


def enqueue_run(task_id: str) -> str:
    """Register a run for an existing task and return its ``run_id``.

    Sync per the FR-02 contract — the 202 handler calls this and then
    schedules the actual execution on the event loop. The id is a fresh
    UUIDv4; the same id is later passed to ``run_task`` via the scheduling
    step so the persisted ``task_results`` row matches what the handler
    returned.

    Citations: SPEC.md line 95 — POST returns 202 + run_id.
    """  # NFR-09 NFR-10
    return str(uuid.uuid4())


async def execute_command(command: str, *, timeout: float) -> dict:
    """Run ``command`` via subprocess exec and return the outcome.

    Spawns the command with
    ``asyncio.create_subprocess_exec(*shlex.split(command))`` (called as
    a module attribute so test spies on ``asyncio.create_subprocess_exec``
    can observe it — see SPEC.md §8 #16 and TEST_SPEC.md §1 FR-02 AC-2.3).

    On timeout, the runner kills the child and awaits ``process.wait()``
    so the OS can reap it — SPEC.md line 149 forbids leaving orphan
    subprocesses behind.

    Returns a dict with keys: ``state``, ``exit_code``, ``stdout_tail``,
    ``stderr_tail``, ``duration_ms``, ``finished_at``.

    Citations:
    - SPEC.md line 96 — spawn form; shell-mode kwarg forbidden.
    - SPEC.md line 149 — ``process.kill()`` + ``await process.wait()``.
    - TEST_SPEC.md §1 FR-02 AC-2.2 / AC-2.3.
    """  # NFR-02 NFR-09 NFR-10
    args = shlex.split(command)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # SPEC.md line 149 — kill, then await wait so the OS reaps the
        # child. Without the second ``await``, the child lingers as a
        # zombie until the parent exits.
        process.kill()
        await process.wait()
        return _outcome(started=started, state="timeout", exit_code=-1)

    # ``process.returncode`` is typed ``int | None`` because it reads as
    # ``None`` until the child is reaped. ``communicate()`` above already
    # waited for exit, so ``wait()`` returns immediately — and it returns
    # the settled code as a plain ``int``, which keeps
    # ``task_results.exit_code`` non-NULL (SPEC.md line 98) without a
    # None-branch that no reachable state exercises.
    exit_code = await process.wait()
    state = "done" if exit_code == 0 else "failed"
    return _outcome(
        started=started,
        state=state,
        exit_code=exit_code,
        stdout_tail=stdout_bytes.decode("utf-8", errors="replace"),
        stderr_tail=stderr_bytes.decode("utf-8", errors="replace"),
    )


def _outcome(
    *,
    started: float,
    state: str,
    exit_code: int,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """Assemble the ``execute_command`` return dict.

    Computes ``duration_ms`` and ``finished_at`` uniformly so the timeout
    and success branches don't drift on the timing fields.
    """  # NFR-10
    return {
        "state": state,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "finished_at": _now_iso(),
    }


async def run_task(
    task_id: str,
    *,
    timeout: Optional[float] = None,
    run_id: Optional[str] = None,
) -> dict:
    """Drive the full FR-02 state machine for one run.

    Looks up the task, generates a ``run_id`` (or accepts one from the
    caller — the 202 handler passes the id it returned), records the
    ``pending → running`` transitions, calls ``execute_command`` for the
    actual subprocess, then appends the terminal state and persists the
    result row.

    ``timeout`` defaults to ``config.TASKQ_TASK_TIMEOUT`` (SPEC.md §5.1).
    A caller (typically the router) may pass an explicit value to override.

    Returns a dict with keys: ``run_id``, ``transitions`` (list of states
    visited, in order), ``state`` (terminal), ``exit_code``,
    ``stdout_tail``, ``stderr_tail``, ``duration_ms``, ``finished_at``.

    Citations:
    - SPEC.md line 97 — state machine pending → running → done|failed|timeout.
    - SPEC.md line 98 — result row written to ``task_results``.
    - TEST_SPEC.md §1 FR-02 AC-2.4 / AC-2.6.
    """  # NFR-09 NFR-10
    if timeout is None:
        timeout = config.TASKQ_TASK_TIMEOUT

    task = repo_tasks.fetch_task(None, task_id)
    if task is None:
        raise LookupError(f"task {task_id!r} not found")

    if run_id is None:
        run_id = str(uuid.uuid4())

    transitions = ["pending", "running"]
    outcome = await execute_command(task["command"], timeout=timeout)
    transitions.append(outcome["state"])

    repo_results.insert_result(
        None,
        run_id=run_id,
        task_id=task_id,
        exit_code=outcome["exit_code"],
        stdout_tail=outcome["stdout_tail"],
        stderr_tail=outcome["stderr_tail"],
        duration_ms=outcome["duration_ms"],
        finished_at=outcome["finished_at"],
    )

    return {"run_id": run_id, "transitions": transitions, **outcome}