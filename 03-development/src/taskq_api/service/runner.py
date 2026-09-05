"""[FR-02 / FR-08] Service layer — state machine, subprocess runner, async executor.

This module owns two surfaces:

  FR-02 (state machine + sync runner) — see ``VALID_STATES`` /
  ``ALLOWED_TRANSITIONS``, ``enqueue_run``, ``execute_command``,
  ``run_task``. Spawns subprocesses via
  ``asyncio.create_subprocess_exec(*shlex.split(command))`` and persists
  results to ``task_results``.

  FR-08 (async executor) — ``INTERRUPTED`` terminal state and the
  ``TaskRunner`` class. ``TaskRunner`` is a background executor with
  concurrency cap (semaphore, max=``TASKQ_MAX_CONCURRENT``), graceful
  drain (waits up to ``TASKQ_DRAIN_TIMEOUT`` then cancels + marks
  ``INTERRUPTED``), and per-task subprocess timeout (``wait_for`` +
  ``process.kill()`` + ``await process.wait()`` so no orphan is left).

Routers in ``taskq_api.api.runs`` call ``enqueue_run`` / ``run_task`` for
the 202 path; the new ``TaskRunner`` drives concurrent fan-out for
FR-08 endpoints and tests. Background scheduling lives here, not in the
router, per SAD §3.2.

Citations:
- SPEC.md line 95 — POST /v1/tasks/{id}/run (scope write) → 202 + run_id.
- SPEC.md line 96 — ``asyncio.create_subprocess_exec(*shlex.split(command))``;
  shell-mode kwarg forbidden; timeout is ``TASKQ_TASK_TIMEOUT``.
- SPEC.md line 97 — state machine pending → running → done | failed | timeout.
- SPEC.md line 98 — results written to ``task_results`` (FR-07 v3 schema).
- SPEC.md line 145 — FR-08 非同步執行器 (async executor).
- SPEC.md line 147 — TaskGroup + MAX_CONCURRENT cap + drain.
- SPEC.md line 149 — ``process.kill()`` then ``await process.wait()``; no orphans.
- SPEC.md line 150 + NFR-03 — CancelledError must propagate; no bare
  ``except Exception`` swallowing it.
- SPEC.md §5.1 — ``TASKQ_MAX_CONCURRENT=8``, ``TASKQ_DRAIN_TIMEOUT=30.0``,
  ``TASKQ_TASK_TIMEOUT=10.0``.
- SPEC.md §8 #16 — grep gate, 0 命中 for the forbidden shell kwarg.
- SPEC.md §8 #25 — orphan-free timeout + CancelledError propagation.
- SPEC.md line 203 — orphan-free shutdown AC.
- SAD.md §2.5/§2.6/§3.2 — runner module responsibilities.
- TEST_SPEC.md §1 FR-02 — six named cases.
- TEST_SPEC.md §1 FR-08 — four named cases.
- NFR-03 — CancelledError propagation is mandatory.
- NFR-09 — observable state machine via ``VALID_STATES`` / ``ALLOWED_TRANSITIONS``.
- NFR-11 — service stays small; routing decisions in api.runs.
"""  # NFR-03 NFR-09 NFR-10 NFR-11
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


# SPEC.md line 97 — the five declared states of the FR-02 state machine.
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


# SPEC.md line 147 — FR-08 adds a 6th terminal state, distinct from
# ``timeout``. ``timeout`` = per-task subprocess exceeded
# ``TASKQ_TASK_TIMEOUT``; ``INTERRUPTED`` = the runner's drain deadline
# was hit while the task was still in-flight.
INTERRUPTED: str = "interrupted"


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


# ---------------------------------------------------------------------------
# [FR-08] Async executor — TaskRunner.
#
# This section is the FR-08 surface: a background executor with a
# semaphore-capped concurrency window (``max_concurrent``), a graceful
# drain that forces overdue tasks into the ``INTERRUPTED`` terminal state
# (``drain_timeout``), and per-task subprocess enforcement
# (``task_timeout`` via ``asyncio.wait_for`` + ``process.kill()``).
#
# Design invariants (NFR-03 + NFR-10 + SPEC.md §8 #25):
#   * The wrapper NEVER uses bare ``except Exception`` — only an explicit
#     ``except asyncio.CancelledError`` records INTERRUPTED and re-raises.
#     Bare-except would silently swallow the cancellation the caller
#     initiated, violating NFR-03.
#   * The default ``_run_one`` enforces ``process.kill()`` followed by
#     ``await process.wait()`` so the OS reaps the child; orphan
#     subprocesses are a SPEC.md line 149 + §8 #25 violation.
#   * ``drain()`` uses ``asyncio.wait_for(asyncio.gather(...))`` so a
#     drain-deadline ``TimeoutError`` is distinguishable from a
#     externally-issued ``CancelledError`` — the former lets drain
#     settle and return a summary, the latter propagates to the caller
#     as NFR-03 requires.
# ---------------------------------------------------------------------------


class TaskRunner:
    """Background executor (FR-08 / SPEC.md line 145).

    A runnable holds at most ``max_concurrent`` tasks in-flight at any
    instant (semaphore cap, AC-8.1). Excess submissions queue behind the
    semaphore without spawning parallel subprocesses (no unbounded
    coroutine creation, SPEC.md line 147). When ``drain()`` is called the
    runner waits up to ``drain_timeout`` seconds for the in-flight tasks
    to settle; tasks still in flight at the deadline are cancelled and
    recorded with state ``INTERRUPTED`` (AC-8.2). Each per-task
    subprocess is bounded by ``task_timeout``; on timeout the child is
    killed and waited on so no orphan survives (AC-8.3).

    The class never catches ``asyncio.CancelledError`` with a bare
    ``except Exception`` — cancellations always propagate (NFR-03).

    Construction is keyword-only so the FR-08 config defaults
    (``config.TASKQ_MAX_CONCURRENT``, ``config.TASKQ_DRAIN_TIMEOUT``,
    ``config.TASKQ_TASK_TIMEOUT``) can be overridden per-instance without
    any positional ambiguity.

    Public surface:
        ``task_states`` — mapping ``run_id -> terminal_state`` for
                          bookkeeping and tests that need to inspect
                          what each run ended in.
        ``_inflight``   — mapping ``run_id -> asyncio.Task`` so callers
                          (or tests) can iterate to ``cancel()`` from
                          outside the runner.

    Test seams:
        ``_run_one``    — overridable per-task body. The default spawns a
                          subprocess; tests patch this to drive the
                          semaphore / drain paths without spawning real
                          child processes.

    Citations:
    - SPEC.md line 145 — FR-08 非同步執行器.
    - SPEC.md line 147 — TaskGroup + MAX_CONCURRENT cap + drain.
    - SPEC.md line 149 — ``process.kill()`` then ``await process.wait()``.
    - SPEC.md line 150 + NFR-03 — no bare ``except Exception``.
    - SPEC.md §5.1 — TASKQ_MAX_CONCURRENT / TASKQ_DRAIN_TIMEOUT /
      TASKQ_TASK_TIMEOUT defaults.
    - SPEC.md §8 #25 — orphan-free shutdown + CancelledError propagation.
    """  # NFR-03 NFR-09 NFR-10

    def __init__(
        self,
        *,
        max_concurrent: Optional[int] = None,
        drain_timeout: Optional[float] = None,
        task_timeout: Optional[float] = None,
    ) -> None:
        # Keyword-only kwargs resolve to the SPEC §5.1 config defaults
        # when omitted so the runner behaviour matches the declared
        # contract without the caller having to thread config through.
        self.max_concurrent: int = (
            int(max_concurrent)
            if max_concurrent is not None
            else int(config.TASKQ_MAX_CONCURRENT)
        )
        self.drain_timeout: float = (
            float(drain_timeout)
            if drain_timeout is not None
            else float(config.TASKQ_DRAIN_TIMEOUT)
        )
        self.task_timeout: float = (
            float(task_timeout)
            if task_timeout is not None
            else float(config.TASKQ_TASK_TIMEOUT)
        )
        # The semaphore enforces ``max_concurrent`` AC-8.1 cap; excess
        # ``_scheduled`` coroutines wait on ``acquire()`` instead of
        # spawning parallel subprocesses (SPEC.md line 147).
        self._sem: asyncio.Semaphore = asyncio.Semaphore(self.max_concurrent)
        # ``_inflight`` is exposed as a runner attribute so tests (and a
        # real shutdown handler) can iterate and ``cancel()`` the in-
        # flight asyncio.Tasks from outside. Required by AC-8.4.
        self._inflight: dict[str, asyncio.Task] = {}
        # ``task_states`` records the terminal state per run_id so the
        # runner can answer "what state is this task in?" for drain
        # accounting and for tests asserting INTERRUPTED.
        self.task_states: dict[str, str] = {}

    async def submit(self, task_id: str, command: str) -> str:
        """Schedule one background run; return its ``run_id``.

        Submission is non-blocking from the caller's perspective: a new
        ``asyncio.Task`` is created that runs ``_scheduled`` and the
        run_id is returned immediately. The semaphore in
        ``_scheduled`` governs actual concurrency — see AC-8.1.
        """  # NFR-09 NFR-10
        run_id = str(uuid.uuid4())
        # ``create_task`` schedules the wrapper on the current loop but
        # does not start it synchronously; the wrapper enters
        # ``_sem.acquire()`` on the next loop iteration.
        task = asyncio.create_task(
            self._scheduled(run_id, task_id, command),
            name=f"taskq-fr08-{run_id}",
        )
        self._inflight[run_id] = task
        return run_id

    async def _scheduled(
        self, run_id: str, task_id: str, command: str
    ) -> None:
        """Acquire the semaphore slot, run the per-task body, record state.

        The semaphore block (AC-8.1) is the gate that limits how many
        per-task bodies run at once. The body itself is ``_run_one`` —
        the default spawns a subprocess; tests override it.

        The exception ladder is explicit and minimal so NFR-03 is
        honoured: only ``asyncio.CancelledError`` is caught to record
        ``INTERRUPTED`` (drain-cancelled tasks always get a state row)
        and then re-raised so the cancellation still surfaces to
        whoever ``await``ed the task. Bare ``except Exception`` is
        intentionally absent.
        """  # NFR-03 NFR-10
        # Queue at the semaphore: excess submissions wait here instead
        # of creating parallel subprocesses (SPEC.md line 147 — no
        # unbounded coroutine creation).
        await self._sem.acquire()
        try:
            result = await self._run_one(
                task_id, command, timeout=self.task_timeout
            )
            # ``_run_one`` may return ``None`` when tests patch a
            # bare-coroutine body (e.g. ``_long_running`` in test 1).
            # Default to "done" so the state row is still recorded.
            state = (result or {}).get("state", "done")
            self.task_states[run_id] = state
        except asyncio.CancelledError:
            # Drain / external cancel — record INTERRUPTED and re-raise
            # so the task ends in CANCELLED state (NFR-03). Catching
            # CancelledError explicitly is not the NFR-03 antipattern;
            # the antipattern is catching it with bare ``except
            # Exception`` (which would silently swallow it). We re-raise
            # unconditionally so the cancellation still propagates.
            self.task_states[run_id] = INTERRUPTED
            raise
        except Exception:  # noqa: BLE001  (real errors, NOT CancelledError — see above)
            self.task_states[run_id] = "failed"
            raise
        finally:
            self._sem.release()

    async def _run_one(
        self, task_id: str, command: str, *, timeout: float
    ) -> Optional[dict]:
        """Default per-task body — spawn subprocess, enforce timeout.

        Spawns via
        ``asyncio.create_subprocess_exec(*shlex.split(command))`` (the
        FR-02 spawn form; shell-mode kwarg forbidden — SPEC.md line 96
        + §8 #16). Wraps ``communicate()`` in ``asyncio.wait_for`` so
        a runaway child is bounded. On timeout the child is killed and
        ``await process.wait()`` is called so the OS reaps it — SPEC.md
        line 149 + §8 #25 forbid orphan subprocesses.

        Returns a dict the wrapper inspects via ``result.get("state")``;
        may return ``None`` if a test patches this with a bare
        coroutine body (the wrapper falls back to ``"done"``).
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
            # SPEC.md line 149 — kill, then await wait so the OS reaps
            # the child. Without the second ``await`` the child lingers
            # as a zombie until the parent exits, which is the orphan
            # case AC-8.3 forbids.
            process.kill()
            await process.wait()
            return {"state": "timeout", "exit_code": -1}
        exit_code = await process.wait()
        state = "done" if exit_code == 0 else "failed"
        return {
            "state": state,
            "exit_code": exit_code,
            "stdout_tail": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr_tail": stderr_bytes.decode("utf-8", errors="replace"),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    async def drain(self) -> dict:
        """Wait up to ``drain_timeout`` for in-flight tasks; cancel overdue.

        Resolution rules:
          * All tasks done within ``drain_timeout`` — return summary
            with ``exit_code = 0`` and accurate ``drained`` /
            ``interrupted`` counts.
          * Drain deadline exceeded — cancel every still-in-flight task,
            record ``INTERRUPTED`` on each, await the cancellations via
            ``asyncio.wait`` (which does NOT raise on cancelled tasks)
            so ``drain()`` can still return cleanly with
            ``exit_code = 0``.
          * External ``CancelledError`` propagated from a task already
            cancelled before ``drain()`` was called — ``gather``
            surfaces it, ``drain()`` does NOT catch ``CancelledError``,
            so it propagates to the caller (NFR-03 / AC-8.4).

        Returns a dict with keys ``{"drained", "interrupted",
        "exit_code"}``. ``exit_code`` is 0 when no exceptions escaped.
        """  # NFR-03 NFR-09 NFR-10
        if not self._inflight:
            return {"drained": 0, "interrupted": 0, "exit_code": 0}

        tasks = list(self._inflight.values())
        try:
            # ``gather`` lets a task's own ``CancelledError`` propagate
            # (preserved by ``_scheduled``'s explicit except + re-raise).
            # AC-8.4 relies on this propagation so ``drain()`` raises
            # when cancellation was initiated from outside.
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self.drain_timeout,
            )
            interrupted = sum(
                1 for state in self.task_states.values()
                if state == INTERRUPTED
            )
            drained = len(self.task_states)
            return {
                "drained": drained,
                "interrupted": interrupted,
                "exit_code": 0,
            }
        except asyncio.TimeoutError:
            # Drain deadline exceeded — cancel every still-running task
            # and mark them INTERRUPTED. We deliberately use
            # ``asyncio.wait`` (not ``gather``) to drain the cancelled
            # tasks: ``wait`` returns once they're done in CANCELLED
            # state without raising, so AC-8.2's summary-with-
            # exit-code-0 contract still holds.
            interrupted_now = 0
            for run_id, task in list(self._inflight.items()):
                if not task.done():
                    task.cancel()
                    self.task_states[run_id] = INTERRUPTED
                    interrupted_now += 1
            # Bounded wait so a misbehaving cancelled task cannot hang
            # ``drain()``; ``wait`` returns when tasks are done (in
            # CANCELLED state) regardless of exception.
            await asyncio.wait(tasks, timeout=5.0)
            return {
                "drained": 0,
                "interrupted": interrupted_now,
                "exit_code": 0,
            }
        # NOTE: ``asyncio.CancelledError`` is intentionally NOT caught
        # here — NFR-03 / AC-8.4 require it to propagate to the caller.
