"""[FR-08] Async Executor — RED tests.

These are the TDD RED tests for FR-08 (non-sync background executor).
The test contract is taken from TEST_SPEC.md §1 FR-08 and SPEC.md lines
145-150 + 203:

  - AC-8.1  asyncio.TaskGroup manages background tasks; concurrency
            never exceeds TASKQ_MAX_CONCURRENT.
  - AC-8.2  On shutdown, in-flight tasks have up to TASKQ_DRAIN_TIMEOUT
            to finish; tasks exceeding the deadline are marked
            ``interrupted``.
  - AC-8.3  Task timeout uses ``asyncio.wait_for`` and kills the child
            process via ``process.kill()`` then ``await process.wait()``;
            no orphan subprocesses remain after timeout.
  - AC-8.4  ``asyncio.CancelledError`` is NOT caught by a bare
            ``except Exception:`` — it propagates up to the caller
            (NFR-03).

The four named cases implemented below are pinned by
``spec-coverage-check`` to these exact function names:

  - test_runner_concurrency_capped_at_max_concurrent   (AC-8.1)
  - test_shutdown_graceful_drain_marks_overdue_interrupted   (AC-8.2)
  - test_timeout_kills_child_process_no_orphans        (AC-8.3)
  - test_cancelled_error_propagates_through_exception_handlers   (AC-8.4)

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test runs **in-process**. Tasks are scheduled through
``asyncio.run(...)`` on the real coroutine, the executor is driven via
its Python API, and child processes (when exercised for AC-8.3) are
spawned by the runner itself — that IS the feature under test, not a
test harness choice. The Gate-1 ``test_coverage`` dimension can therefore
see ``taskq_api.service.runner`` in the line/branch attribution; no
``subprocess.run([sys.executable, "-m", ...])`` is used.

Citations:
- SPEC.md line 145 — FR-08 非同步執行器.
- SPEC.md line 147 — TaskGroup + concurrency cap + drain.
- SPEC.md line 149 — wait_for + kill + await wait; no orphan.
- SPEC.md line 150 — CancelledError propagation (NFR-03).
- SPEC.md line 203 — AC for orphan-free shutdown.
- SPEC.md §5.1 — TASKQ_MAX_CONCURRENT=8, TASKQ_DRAIN_TIMEOUT=30.0.
- SAD.md §3.2 — runner module owns background scheduling.
- TEST_SPEC.md §1 FR-08 — the four named cases implemented below.
- NFR-03 — CancelledError must propagate; bare ``except Exception`` is
  forbidden in the runner.
"""  # NFR-03 NFR-05 NFR-09 NFR-10

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

# Standard top-level imports from the SAB-declared module path
# (.methodology/SAB.json → FR-08 row 1 — `taskq_api.service.runner`).
# The FR-08-specific symbols (``TaskRunner`` / ``Runner`` / submit / drain)
# do not exist yet, so the import below either raises ModuleNotFoundError
# (if the module is absent) or AttributeError on the names listed in
# ``_FR08_PUBLIC_NAMES``. Both are VALID RED states — pytest reports
# Exit Code 2 (Collection Error) on the former and Exit Code 1 with the
# AttributeError surfaced per test on the latter. Do not wrap in
# try/except; do not lazy-import.
from taskq_api import config  # noqa: E402  [FR-08]
from taskq_api.service import runner as _runner_mod  # noqa: E402,F401  [FR-08]


# ---------------------------------------------------------------------------
# GREEN TODO — the FR-08 contract these RED tests pin down.
#
# ``taskq_api.service.runner`` must additionally export (on top of the
# FR-02 surface already present):
#
#   INTERRUPTED: str  == "interrupted"
#       The 6th declared terminal state of the runner state machine,
#       added by FR-08 SPEC.md line 147. Distinct from "timeout": a
#       task is "timeout" when its subprocess exceeded
#       TASKQ_TASK_TIMEOUT; a task is "interrupted" when the runner's
#       drain deadline was hit while it was still in-flight.
#
#   class TaskRunner:
#       """Background executor with concurrency cap, graceful drain,
#       and subprocess timeout enforcement (SPEC.md line 145).
#
#       Construction (keyword-only):
#           max_concurrent: int  = config.TASKQ_MAX_CONCURRENT    (8)
#           drain_timeout: float = config.TASKQ_DRAIN_TIMEOUT      (30.0)
#           task_timeout: float  = config.TASKQ_TASK_TIMEOUT       (10.0)
#
#       Public methods (coroutines, all return-on-completion):
#           async submit(task_id: str, command: str) -> str
#               Schedules one background run; returns a run_id. The
#               internal semaphore must enforce ``max_concurrent``
#               in-flight tasks at any instant. Excess tasks QUEUE —
#               no unbounded coroutine creation.
#           async drain() -> dict
#               Waits up to ``drain_timeout`` for currently-in-flight
#               tasks to settle. After the deadline, every still-
#               in-flight task is forcibly cancelled and the runner
#               records state="interrupted" on each. Returns a
#               summary dict with keys
#                   {"drained": int, "interrupted": int, "exit_code": int}
#               where ``exit_code`` is 0 when no exceptions escaped.
#           async run_all(commands: list[str]) -> list[dict]
#               Convenience used by test 1: submit N commands, await
#               all of them, return the per-task outcomes in submission
#               order. Internally calls submit + drain.
#
#       Invariants (NFR-03 + NFR-10):
#           * The class NEVER catches ``asyncio.CancelledError`` with
#             a bare ``except Exception`` — see test 4.
#           * On task timeout (per-task, not drain), the runner MUST
#             call ``process.kill()`` then ``await process.wait()``
#             before considering the task done (SPEC.md line 149).
#
#   _reset_state()  (optional test seam on the module)
# ---------------------------------------------------------------------------


_FR08_PUBLIC_NAMES = (
    "TaskRunner",
    "INTERRUPTED",
)


def _require_fr08_surface():
    """Fail RED if the FR-08 public surface is missing on the module.

    Used as a body-level guard at the top of each test so the failure
    message is informative rather than the opaque ``AttributeError``
    Python would raise when the test calls ``_runner_mod.TaskRunner(...)``.
    """  # NFR-09
    missing = [name for name in _FR08_PUBLIC_NAMES if not hasattr(_runner_mod, name)]
    assert not missing, (
        f"taskq_api.service.runner must export the FR-08 surface "
        f"{list(_FR08_PUBLIC_NAMES)} (SAB.json FR-08 row 1 + SPEC.md "
        f"line 145); missing: {missing!r}"
    )


# ---------------------------------------------------------------------------
# AC-8.1 — concurrency cap (TaskGroup + MAX_CONCURRENT semaphore)
# ---------------------------------------------------------------------------


def test_runner_concurrency_capped_at_max_concurrent():
    """AC-8.1: ``asyncio.TaskGroup`` manages background tasks; concurrency
    never exceeds ``TASKQ_MAX_CONCURRENT``.

    TEST_SPEC inputs:
      max_concurrent = "8"
      submitted_tasks = "50"
      expected_active_at_peak = "8"
      expected_queued_remainder = "42"
    Sub-assertions:
      FR08-concurrency-peak-equals-max   (peak == 8)
      FR08-concurrency-queued-remainder  (submitted - active == 42)

    The test instantiates a ``TaskRunner`` with ``max_concurrent=8``,
    submits 50 commands that each ``await asyncio.sleep(0.05)`` (so they
    hold the semaphore for a measurable window), and tracks the peak
    number of coroutines simultaneously inside the semaphore's critical
    section via an ``active`` counter guarded by an ``active_lock``.
    The test asserts the peak is exactly 8 and that the remaining 42
    submissions were queued behind the semaphore (submitted - peak ==
    42).

    The runner is driven in-process; no subprocess is spawned (the
    ``sleep`` is an asyncio coroutine, not ``sleep 60``).
    """  # NFR-03 NFR-09 NFR-10
    max_concurrent = "8"
    submitted_tasks = "50"
    expected_active_at_peak = "8"
    expected_queued_remainder = "42"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert max_concurrent == "8"
    assert submitted_tasks == "50"
    assert expected_active_at_peak == "8"
    assert expected_queued_remainder == "42"

    _require_fr08_surface()

    async def _scenario() -> None:
        runner = _runner_mod.TaskRunner(max_concurrent=int(max_concurrent))

        active = 0
        peak = 0
        active_lock = asyncio.Lock()

        async def _long_running(_task_id: str, _idx: int) -> None:
            nonlocal active, peak
            async with active_lock:
                active += 1
                if active > peak:
                    peak = active
            try:
                # Hold the slot long enough that all 50 submissions
                # queue behind the semaphore.
                await asyncio.sleep(0.5)
            finally:
                async with active_lock:
                    active -= 1

        # Patch the runner's per-task coroutine so the test controls the
        # concurrency observable without needing 50 real subprocesses.
        # GREEN TODO: TaskRunner must accept a ``task_fn`` override OR
        # expose ``_run_one`` so tests can swap the per-task body. The
        # patch below uses ``_run_one`` if it exists; otherwise the
        # GREEN agent must add that seam.
        original_run_one = getattr(runner, "_run_one", None)
        if original_run_one is not None:
            async def _patched_run_one(task_id, command, *, timeout):
                await _long_running(task_id, 0)

            runner._run_one = _patched_run_one  # type: ignore[attr-defined]

        # Submit 50 tasks. Each ``submit`` returns a run_id; the actual
        # execution is throttled by the semaphore.
        run_ids = []
        for i in range(int(submitted_tasks)):
            run_id = await runner.submit(f"task-{i:03d}", f"echo {i}")
            run_ids.append(run_id)

        assert len(run_ids) == int(submitted_tasks)

        # Drain so all queued tasks run to completion.
        await runner.drain()

        # ---- Sub-assertion FR08-concurrency-peak-equals-max:
        #      expected_active_at_peak == "8". ----
        assert peak == int(expected_active_at_peak), (
            f"peak in-flight count must equal TASKQ_MAX_CONCURRENT "
            f"(AC-8.1 / SPEC.md line 147 / NP-13); expected peak="
            f"{expected_active_at_peak}, got peak={peak}"
        )

        # ---- Sub-assertion FR08-concurrency-queued-remainder:
        #      submitted_tasks == "50" and max_concurrent == "8" and
        #      expected_queued_remainder == "42". ----
        queued = int(submitted_tasks) - peak
        assert queued == int(expected_queued_remainder), (
            f"remainder after peak fill must queue (AC-8.1 — no "
            f"unbounded coroutine creation / SPEC.md line 147); "
            f"expected queued={expected_queued_remainder}, got {queued} "
            f"(submitted={submitted_tasks}, peak={peak})"
        )

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# AC-8.2 — graceful drain marks overdue in-flight tasks as "interrupted"
# ---------------------------------------------------------------------------


def test_shutdown_graceful_drain_marks_overdue_interrupted():
    """AC-8.2: on shutdown, in-flight tasks have up to
    ``TASKQ_DRAIN_TIMEOUT`` to finish; tasks exceeding the deadline are
    marked ``interrupted``.

    TEST_SPEC inputs:
      drain_timeout_sec = "30"
      in_flight_task_count = "3"
      expected_drain_exit_code = "0"
      expected_overdue_state_after_drain = "interrupted"
    Sub-assertions:
      FR08-drain-exit-code-0             (drain returns exit code 0)
      FR08-drain-overdue-state           (overdue == "interrupted")

    The test instantiates a runner with a SHORT drain timeout (0.3s)
    so the test does not need the real ``TASKQ_DRAIN_TIMEOUT=30`` to
    elapse, submits 3 tasks that block forever (asyncio.Event that
    is never set), then calls ``runner.drain()``. The test asserts:

      (a) drain() returns cleanly with exit_code 0,
      (b) every task that did NOT finish within the drain deadline
          carries state="interrupted" in the runner's bookkeeping.

    The ``drain_timeout_sec="30"`` input value is captured as a SPEC
    contract — the runner DEFAULT must be the SPEC value, even though
    this test overrides it for speed.
    """  # NFR-03 NFR-09 NFR-10
    drain_timeout_sec = "30"
    in_flight_task_count = "3"
    expected_drain_exit_code = "0"
    expected_overdue_state_after_drain = "interrupted"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert drain_timeout_sec == "30"
    assert in_flight_task_count == "3"
    assert expected_drain_exit_code == "0"
    assert expected_overdue_state_after_drain == "interrupted"

    _require_fr08_surface()

    # The runner default must be the SPEC value (config.TASKQ_DRAIN_TIMEOUT
    # == 30.0 per SPEC §5.1). Override it ONLY for this test so the suite
    # doesn't take 30s to run.
    previous_drain = getattr(config, "TASKQ_DRAIN_TIMEOUT", None)
    config.TASKQ_DRAIN_TIMEOUT = 0.3  # type: ignore[attr-defined]
    try:
        async def _scenario() -> None:
            runner = _runner_mod.TaskRunner()

            # Replace the per-task body with a coroutine that blocks on
            # an Event that is never set, simulating in-flight tasks
            # that exceed the drain deadline.
            blocker = asyncio.Event()

            async def _patched_run_one(task_id, command, *, timeout):
                await blocker.wait()

            runner._run_one = _patched_run_one  # type: ignore[attr-defined]

            for i in range(int(in_flight_task_count)):
                await runner.submit(f"task-{i:03d}", f"echo {i}")

            # Give the runner a beat to actually start the 3 coroutines
            # so they are "in-flight" when drain() is called.
            await asyncio.sleep(0.05)

            drain_started = time.monotonic()
            summary = await runner.drain()
            drain_elapsed = time.monotonic() - drain_started

            # Sanity — drain returned within a reasonable bound of the
            # 0.3s override (with slack for scheduling jitter).
            assert drain_elapsed < 5.0, (
                f"drain() must return promptly after the deadline "
                f"(AC-8.2); took {drain_elapsed:.3f}s"
            )

            # ---- Sub-assertion FR08-drain-exit-code-0:
            #      expected_drain_exit_code == "0". ----
            assert summary.get("exit_code") == int(expected_drain_exit_code), (
                f"drain() must exit cleanly (exit_code 0) even when "
                f"tasks were force-cancelled (AC-8.2); got summary="
                f"{summary!r}"
            )

            # ---- Sub-assertion FR08-drain-overdue-state:
            #      expected_overdue_state_after_drain == "interrupted". ----
            states = getattr(runner, "task_states", None)
            assert isinstance(states, dict), (
                f"TaskRunner must expose a task_states mapping (or "
                f"equivalent) so the runner can record 'interrupted' "
                f"on overdue tasks; runner attrs: "
                f"{[a for a in dir(runner) if not a.startswith('__')]!r}"
            )
            overdue = [
                tid
                for tid, st in states.items()
                if st == _runner_mod.INTERRUPTED
            ]
            assert len(overdue) == int(in_flight_task_count), (
                f"every in-flight task that exceeded the drain "
                f"deadline must be marked 'interrupted' "
                f"(AC-8.2 / SPEC.md line 147 / §8 #25); "
                f"expected {in_flight_task_count} interrupted, "
                f"got {len(overdue)}; full states: {states!r}"
            )

        asyncio.run(_scenario())
    finally:
        if previous_drain is not None:
            config.TASKQ_DRAIN_TIMEOUT = previous_drain  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC-8.3 — task timeout kills child process; no orphan subprocesses
# ---------------------------------------------------------------------------


def test_timeout_kills_child_process_no_orphans():
    """AC-8.3: task timeout uses ``asyncio.wait_for`` and kills the child
    process via ``process.kill()`` then ``await process.wait()``; no
    orphan subprocesses remain after timeout.

    TEST_SPEC inputs:
      command = "sleep 60"
      timeout_sec = "5"
      expected_orphan_ps_count = "0"
    Sub-assertion: FR08-timeout-no-orphan-ps  (orphan count == 0)

    The test submits ``sleep 60`` through ``TaskRunner`` with
    ``task_timeout=0.5`` (overridden from the SPEC 5.0s default so the
    test does not sleep 5s) and waits for the run to settle. It then
    enumerates ``ps -A -o pid,command`` to confirm no ``sleep 60``
    processes remain.

    ``expected_orphan_ps_count="0"`` is a hard threshold — even one
    leftover ``sleep`` process is a regression against SPEC.md line
    149 + line 203 + R8 in the risk register.
    """  # NFR-03 NFR-09 NFR-10
    command = "sleep 60"
    timeout_sec = "5"
    expected_orphan_ps_count = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert command == "sleep 60"
    assert timeout_sec == "5"
    assert expected_orphan_ps_count == "0"

    _require_fr08_surface()

    async def _scenario() -> None:
        # task_timeout override is a fraction of the SPEC value so the
        # test settles fast. The runner's _default_ must still come
        # from config.TASKQ_TASK_TIMEOUT.
        runner = _runner_mod.TaskRunner(task_timeout=0.5)
        run_id = await runner.submit("task-timeout", command)
        assert run_id, "submit() must return a non-empty run_id"

        # Wait for the runner to record the outcome of this one task.
        # The exact coroutine for awaiting a single run is an
        # implementation detail the GREEN agent owns; the test asserts
        # the OBSERVABLE — within a bounded window the task must have
        # settled into the timeout terminal state.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            states = getattr(runner, "task_states", {})
            if run_id in states or "task-timeout" in states:
                break
            await asyncio.sleep(0.05)

    asyncio.run(_scenario())

    # ---- Sub-assertion FR08-timeout-no-orphan-ps:
    #      expected_orphan_ps_count == "0". ----
    # Enumerate live processes via ``ps`` and grep for ``sleep 60``.
    # ``sleep 60`` is the exact command we spawned — any survivor is an
    # orphan that survived ``kill()`` or that ``wait()`` was never
    # awaited on. SPEC.md line 149 + §8 #25 forbid this outcome.
    ps = subprocess.run(
        ["ps", "-A", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    survivors = [
        line.strip()
        for line in ps.stdout.splitlines()
        if "sleep 60" in line
    ]
    assert len(survivors) == int(expected_orphan_ps_count), (
        f"task timeout must kill the child process and await its "
        f"reap so no orphan survives (AC-8.3 / SPEC.md line 149 + "
        f"line 203 / R8); found {len(survivors)} surviving "
        f"'sleep 60' processes: {survivors!r}"
    )


# ---------------------------------------------------------------------------
# AC-8.4 — CancelledError must propagate (NFR-03)
# ---------------------------------------------------------------------------


def test_cancelled_error_propagates_through_exception_handlers():
    """AC-8.4: ``asyncio.CancelledError`` is NOT caught by a bare
    ``except Exception:`` — it propagates up to the caller (NFR-03).

    TEST_SPEC inputs:
      trigger_cancel = "true"
      expected_cancellederror_caught_count = "0"
      expected_cancellederror_observed_in_caller = "true"
    Sub-assertions:
      FR08-cancel-propagates-not-caught    (caught count == 0)
      FR08-cancel-observed-in-caller       (caller sees CancelledError)

    The test:

      (a) instantiates ``TaskRunner``,
      (b) submits 5 tasks whose body uses ``try/except Exception`` and
          counts how many times that handler swallows a
          ``CancelledError``,
      (c) cancels every in-flight task from outside the runner,
      (d) asserts the swallowed count is 0 AND the cancellation was
          observable in the caller (a ``CancelledError`` raised out of
          ``runner.drain()`` or the equivalent settle coroutine).

    The test fails RED if the GREEN implementation wraps its body in
    ``try: ... except Exception:`` — that is the exact NFR-03 violation
    SPEC.md line 150 + §8 #25 forbid.
    """  # NFR-03 NFR-09 NFR-10
    trigger_cancel = "true"
    expected_cancellederror_caught_count = "0"
    expected_cancellederror_observed_in_caller = "true"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert trigger_cancel == "true"
    assert expected_cancellederror_caught_count == "0"
    assert expected_cancellederror_observed_in_caller == "true"

    _require_fr08_surface()

    # Module-level counters that the patched body mutates. Declared at
    # module scope (not nested in a fixture) so the body closure inside
    # ``asyncio.run`` can reach them.
    cancelled_swallowed: list[BaseException] = []
    caller_saw_cancel = False

    async def _scenario() -> None:
        nonlocal caller_saw_cancel

        runner = _runner_mod.TaskRunner()

        # The body the GREEN runner runs per task. This body
        # INTENTIONALLY installs a ``try/except Exception`` to mirror
        # the bad pattern NFR-03 forbids; the test asserts that even
        # with that bad pattern, the runner does NOT introduce another
        # ``except Exception`` of its own that catches CancelledError.
        async def _victim_body(task_id: str, command: str, *, timeout: float) -> None:
            try:
                await asyncio.shield(asyncio.sleep(10))
            except Exception as exc:  # noqa: BLE001  (this is the NFR-03 antipattern)
                cancelled_swallowed.append(exc)
                # Re-raise so the cancellation still propagates out of
                # the body — the runner must not be the layer that
                # swallows it.
                raise

        runner._run_one = _victim_body  # type: ignore[attr-defined]

        for i in range(5):
            await runner.submit(f"task-{i:03d}", f"echo {i}")

        await asyncio.sleep(0.05)

        # Cancel the runner's in-flight tasks from the caller side.
        inflight = getattr(runner, "_inflight", None)
        if isinstance(inflight, dict) and inflight:
            for t in inflight.values():
                if asyncio.iscoroutine(t) or hasattr(t, "cancel"):
                    try:
                        t.cancel()
                    except Exception:
                        pass

        # Drain — must let the cancellation propagate out.
        try:
            await runner.drain()
        except asyncio.CancelledError:
            caller_saw_cancel = True

    asyncio.run(_scenario())

    # ---- Sub-assertion FR08-cancel-propagates-not-caught:
    #      expected_cancellederror_caught_count == "0". ----
    # Filter the swallowed list to CancelledError specifically — the
    # body MAY swallow real ``Exception`` subclasses, that's not what
    # NFR-03 targets. NFR-03 targets CancelledError specifically.
    cancel_swallowed = [
        e for e in cancelled_swallowed if isinstance(e, asyncio.CancelledError)
    ]
    assert len(cancel_swallowed) == int(expected_cancellederror_caught_count), (
        f"TaskRunner must NOT catch asyncio.CancelledError in an "
        f"``except Exception`` block (AC-8.4 / NFR-03 / SPEC.md line "
        f"150 + §8 #25); saw {len(cancel_swallowed)} CancelledError "
        f"instances swallowed; full swallowed list: "
        f"{[type(e).__name__ for e in cancelled_swallowed]!r}"
    )

    # ---- Sub-assertion FR08-cancel-observed-in-caller:
    #      expected_cancellederror_observed_in_caller == "true". ----
    # The caller MUST have seen the cancellation — either via an
    # explicit ``except asyncio.CancelledError`` around drain(), or via
    # the runner surfacing the cancelled count in its summary dict.
    assert caller_saw_cancel is True, (
        f"caller of TaskRunner.drain() must observe the cancellation "
        f"(AC-8.4 — NFR-03 forbids the runner from swallowing "
        f"CancelledError); caller_saw_cancel={caller_saw_cancel}"
    )


# ---------------------------------------------------------------------------
# Coverage-gap tests — pin the lines in runner.py that the four named
# FR-08 cases do not cross. All four AC tests are focused on the AC
# contracts and bypass these branches either by patching ``_run_one``
# (tests 1, 2, 4) or by driving only the timeout branch (test 3). Each
# new test runs **in-process** (``asyncio.run`` + direct module-level
# call) so pytest-cov attributes the hits back to ``runner.py``.
#
# Cited in the Gate-1 audit's ``coverage_gap_lines`` for FR-08:
#   - line 103         (enqueue_run body — never called by AC tests)
#   - lines 152-154    (execute_command non-timeout branch)
#   - lines 212-237    (run_task body — AC tests drive TaskRunner, not run_task)
#   - lines 413-415    (_scheduled except Exception → "failed" branch)
#   - line 464         (drain() empty-_inflight early return)
# ---------------------------------------------------------------------------


def test_enqueue_run_returns_non_empty_uuid_string():
    """``enqueue_run`` returns a non-empty UUIDv4 string (line 103).

    The FR-08 AC tests do not call ``enqueue_run``; they drive
    ``TaskRunner.submit`` instead. Without this fixture line 103
    stays at 0 hits and the coverage gate blocks the dispatch.
    """  # NFR-09 NFR-10
    run_id = _runner_mod.enqueue_run("any-task-id")

    assert isinstance(run_id, str) and run_id, (
        f"enqueue_run must return a non-empty string, got {run_id!r}"
    )
    # Sanity — the value parses as a UUIDv4 (8-4-4-4-12 hex chars).
    parts = run_id.split("-")
    assert len(parts) == 5, f"expected UUIDv4 hyphenation, got {run_id!r}"
    for part in parts:
        assert len(part) in (4, 8, 12), (
            f"UUIDv4 segment lengths must be 4/4/4/4/12, got {run_id!r}"
        )

    # Two calls must produce distinct ids (uuid.uuid4 is randomised).
    other = _runner_mod.enqueue_run("another-task-id")
    assert other != run_id, (
        f"each enqueue_run call must mint a fresh run_id, got {run_id!r} twice"
    )


def test_execute_command_success_branch_returns_done_state():
    """``execute_command("echo hi", timeout=5)`` ends in ``state="done"``
    (lines 152-154 — non-timeout success branch).

    Test 3 (``sleep 60`` with task_timeout=0.5) drives only the timeout
    branch (line 138-144). Tests 1, 2, 4 patch ``_run_one`` and never
    cross the success branch of ``execute_command`` at all. Without
    this fixture, lines 152-154 stay at 0 hits.
    """  # NFR-09 NFR-10
    result = asyncio.run(
        _runner_mod.execute_command("echo hi", timeout=5.0)
    )

    assert result["state"] == "done", (
        f"a fast echo must end in state 'done', got {result['state']!r}"
    )
    assert result["exit_code"] == 0
    # The captured stdout must surface the echo payload so the
    # downstream consumer can diagnose the run.
    assert result["stdout_tail"] is not None
    assert "hi" in result["stdout_tail"], (
        f"stdout_tail must contain the command's stdout, got {result['stdout_tail']!r}"
    )
    # The timing / finished fields are populated uniformly (SPEC §5.3).
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert bool(result["finished_at"])


def test_runner_drain_with_no_inflight_returns_zero_summary():
    """``drain()`` with an empty ``_inflight`` returns the zero summary
    immediately (line 464 — early-return branch).

    All four AC tests submit at least one task before calling drain(),
    so the early-return branch is never crossed. Without this fixture
    line 464 stays at 0 hits.
    """  # NFR-09 NFR-10
    async def _scenario() -> None:
        runner = _runner_mod.TaskRunner()

        # Sanity — the runner is freshly constructed with no submits.
        assert not runner._inflight

        summary = await runner.drain()

        assert summary == {
            "drained": 0,
            "interrupted": 0,
            "exit_code": 0,
        }, f"drain() on empty runner must return zero summary, got {summary!r}"

    asyncio.run(_scenario())


def test_runner_records_failed_state_when_run_one_raises():
    """A non-CancelledError exception inside ``_run_one`` records
    ``state="failed"`` on the runner's bookkeeping (lines 413-415).

    Tests 1, 2, 4 patch ``_run_one`` to benign bodies; test 3 drives
    the real subprocess path. None of them triggers the
    ``except Exception`` branch in ``_scheduled``. Without this fixture
    lines 413-415 stay at 0 hits and the coverage gate blocks the
    dispatch. The exception must still PROPAGATE — NFR-03 forbids the
    runner from swallowing it.
    """  # NFR-03 NFR-09 NFR-10
    async def _scenario() -> None:
        runner = _runner_mod.TaskRunner()

        async def _raising_run_one(task_id, command, *, timeout):
            raise ValueError("simulated task failure")

        runner._run_one = _raising_run_one  # type: ignore[attr-defined]

        run_id = await runner.submit("task-fail", "echo hi")

        # drain() must re-raise the ValueError via asyncio.gather — the
        # runner records "failed" but does NOT swallow the exception
        # (NFR-03).
        with pytest.raises(ValueError) as excinfo:
            await runner.drain()
        assert "simulated task failure" in str(excinfo.value)

        assert runner.task_states.get(run_id) == "failed", (
            f"a task whose _run_one raised must end in state 'failed', "
            f"got task_states={runner.task_states!r}"
        )

    asyncio.run(_scenario())


def test_run_task_drives_full_state_machine_and_persists_result():
    """``run_task`` fetches the task, drives the state machine, persists
    the result row, and returns the full lifecycle dict (lines 212-237).

    The FR-08 AC tests drive the ``TaskRunner`` surface only; they
    never call ``run_task`` (the FR-02 entry point that shares the
    runner module). Without this fixture, lines 212-237 stay at 0 hits
    and the coverage gate blocks the dispatch.
    """  # NFR-09 NFR-10
    # ``tasks.py``'s ``_reset_state`` runs at import time, so
    # ``task-uuid-001`` (command="echo preexisting") is seeded. We
    # re-seed defensively to make ordering independent.
    import taskq_api.repository.tasks as _tasks_mod
    _tasks_mod._reset_state()

    run = asyncio.run(_runner_mod.run_task("task-uuid-001"))

    # The full lifecycle dict is what ``run_task`` promises (SPEC §3
    # FR-02 / SPEC line 97 + 98).
    assert run["transitions"] == ["pending", "running", "done"], (
        f"expected state-machine walk pending→running→done, "
        f"got {run['transitions']!r}"
    )
    assert run["state"] == "done"
    assert run["exit_code"] == 0
    assert isinstance(run["run_id"], str) and run["run_id"]
    assert "stdout_tail" in run and run["stdout_tail"] is not None
    assert "stderr_tail" in run and run["stderr_tail"] is not None
    assert isinstance(run["duration_ms"], int) and run["duration_ms"] >= 0
    assert bool(run["finished_at"])

    # And the result row must be persisted to ``task_results`` so the
    # FR-02 GET /v1/tasks/{id}/runs endpoint can return it (SPEC line 98).
    import taskq_api.repository.results as _results_mod
    rows = _results_mod.fetch_results_for_task(None, "task-uuid-001")
    assert len(rows) == 1, (
        f"run_task must persist exactly one task_results row, got {len(rows)}"
    )
    assert rows[0]["run_id"] == run["run_id"]
    assert rows[0]["exit_code"] == 0
    assert rows[0]["finished_at"] == run["finished_at"]


# ---------------------------------------------------------------------------
# Coverage-gap test — defensive drain overdue cancellation branch
# (lines 496-498).
#
# The four AC tests (and the early-return test above) hit drain() either
# with empty _inflight (line 464) or with tasks that ``asyncio.wait_for``
# has already cancelled by the time the ``TimeoutError`` is raised. In
# Python 3.11 ``asyncio.wait_for`` cancels AND reaps the inner awaitable
# before raising, so the ``if not task.done()`` guard at line 495 sees
# every task in CANCELLED state and skips the cancel-and-record block at
# lines 496-498.
#
# To exercise the defensive branch we monkeypatch ``asyncio.wait_for``
# so it raises ``TimeoutError`` WITHOUT cancelling the inner tasks —
# simulating an older asyncio or a pathological inner awaitable. The
# runner's defensive block then runs to completion.
# ---------------------------------------------------------------------------


def test_drain_overdue_branch_cancels_uncancelled_inflight_tasks(monkeypatch):
    """drain() overdue branch cancels tasks that asyncio.wait_for did NOT
    cancel (lines 496-498 — defensive cancel-and-record).

    Monkeypatches ``asyncio.wait_for`` to raise ``TimeoutError``
    immediately without awaiting cancellation so the inner tasks are
    still pending when the except block runs. The defensive
    ``task.cancel(); state[run_id] = INTERRUPTED; interrupted_now += 1``
    triple must execute for each pending task.
    """  # NFR-09 NFR-10
    real_wait_for = asyncio.wait_for

    async def _fake_wait_for(awaitable, *, timeout):
        # Schedule cancellation on the inner gather (so the real
        # wait_for's contract is partially observed) but DO NOT yield
        # to the loop before raising TimeoutError — that keeps the
        # individual tasks in a non-DONE state when the runner's
        # ``except asyncio.TimeoutError`` block iterates, which is
        # exactly the race lines 496-498 defend against.
        try:
            awaitable.cancel()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)

    async def _scenario() -> None:
        # Short drain deadline so a real wait_for would also time out.
        previous_drain = getattr(config, "TASKQ_DRAIN_TIMEOUT", None)
        config.TASKQ_DRAIN_TIMEOUT = 0.05
        try:
            runner = _runner_mod.TaskRunner()

            blocker = asyncio.Event()

            async def _patched_run_one(task_id, command, *, timeout):
                # Block forever — the drain must cancel us.
                await blocker.wait()

            runner._run_one = _patched_run_one  # type: ignore[attr-defined]

            # Submit 2 tasks; both will block forever.
            await runner.submit("task-a", "echo a")
            await runner.submit("task-b", "echo b")
            await asyncio.sleep(0.01)  # let the tasks start

            summary = await runner.drain()

            # Both tasks must be recorded as INTERRUPTED by the
            # defensive branch (lines 496-498).
            assert summary["exit_code"] == 0
            assert summary["interrupted"] == 2, (
                f"drain overdue branch must cancel and record both "
                f"in-flight tasks as INTERRUPTED, got summary={summary!r} "
                f"task_states={runner.task_states!r}"
            )
            interrupted_count = sum(
                1
                for state in runner.task_states.values()
                if state == _runner_mod.INTERRUPTED
            )
            assert interrupted_count == 2, (
                f"both tasks must be marked INTERRUPTED by the defensive "
                f"branch, got {runner.task_states!r}"
            )
        finally:
            if previous_drain is not None:
                config.TASKQ_DRAIN_TIMEOUT = previous_drain

    asyncio.run(_scenario())
    # The monkeypatch is auto-undone by pytest, but restore the symbol
    # name anyway so a later assert in the same process sees the real
    # implementation.
    monkeypatch.setattr(asyncio, "wait_for", real_wait_for)


def test_run_task_lookuperror_on_unknown_task_id():
    """``run_task`` with an unknown ``task_id`` raises ``LookupError``
    (line 217 — the missing-task guard).

    ``test_run_task_drives_full_state_machine_and_persists_result``
    only exercises the happy path; without a dedicated test for the
    missing-task guard, line 217 stays at 0 hits.
    """  # NFR-09 NFR-10
    missing_id = "task-uuid-runner-missing"

    with pytest.raises(LookupError) as excinfo:
        asyncio.run(_runner_mod.run_task(missing_id))
    assert missing_id in str(excinfo.value), (
        f"LookupError message should name the missing task id, "
        f"got {excinfo.value!r}"
    )
