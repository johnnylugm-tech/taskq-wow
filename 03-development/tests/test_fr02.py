"""FR-02 — 任務執行端點 (POST /v1/tasks/{id}/run, GET /v1/tasks/{id}/runs) — RED tests.

These are the TDD RED tests for FR-02. They import the SAB-declared modules
(``taskq_api.api.runs`` / ``taskq_api.service.runner`` /
``taskq_api.repository.results``). Those modules do not exist yet, so the
import block below raises ``ModuleNotFoundError`` and pytest reports a
Collection Error (Exit Code 2) — that is the VALID RED state for this step.
No ``try/except ImportError`` is used to hide it.

Once the GREEN agent implements the three modules, the assertions here drive
the behaviour contract from TEST_SPEC.md §1 FR-02 (NP-15 subprocess timeout,
NP-13 race-free state transition) and SPEC.md lines 95-99 + 149.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against the FR-02 router, and the runner is driven by
``asyncio.run(...)`` on the real coroutine. Nothing here shells out to
``subprocess.run([sys.executable, "-m", ...])``, so pytest-cov attributes
execution to the real handler / service / repository functions and the
Gate-1 ``test_coverage`` dimension can see them. The only child processes are
the ones ``service.runner`` itself spawns — that IS the feature under test
(AC-2.2 / AC-2.3), not a test harness choice.

Citations:
- SPEC.md line 95 — POST /v1/tasks/{id}/run (scope write) → 202 + run_id.
- SPEC.md line 96 — asyncio.create_subprocess_exec(*shlex.split(command));
  shell=True forbidden; timeout is TASKQ_TASK_TIMEOUT.
- SPEC.md line 97 — state machine pending → running → done | failed | timeout.
- SPEC.md line 98 — results written to task_results (FR-07 v3 schema).
- SPEC.md line 99 — GET /v1/tasks/{id}/runs (scope read), newest first.
- SPEC.md line 149 — process.kill() then await process.wait(); no orphans.
- SPEC.md §8 #16 — grep 03-development/src/ for the forbidden shell kwarg → 0 hits.
- SAD.md §2.4/§2.5/§2.6, §3.2 — module responsibilities and write-path lifecycle.
- TEST_SPEC.md §1 FR-02 — the six named cases implemented below.
"""  # NFR-05 NFR-10

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-02). ModuleNotFoundError here is the valid RED
# signal — pytest reports Exit Code 2 (Collection Error). Do not wrap in
# try/except; do not lazy-import.
from taskq_api import config  # noqa: E402  [FR-02]
from taskq_api.api.runs import router as runs_router  # noqa: E402,F401  [FR-02]
from taskq_api.api.tasks import router as tasks_router  # noqa: E402,F401  [FR-01 seed]
from taskq_api.repository.results import (  # noqa: E402,F401  [FR-02]
    fetch_results_for_task,
    insert_result,
)
from taskq_api.service.runner import (  # noqa: E402,F401  [FR-02]
    ALLOWED_TRANSITIONS,
    VALID_STATES,
    enqueue_run,
    execute_command,
    run_task,
)


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.service.runner:
#   VALID_STATES: frozenset[str]
#       == {"pending", "running", "done", "failed", "timeout"}  (SPEC line 97)
#   ALLOWED_TRANSITIONS: dict[str, frozenset[str]]
#       {"pending": {"running"},
#        "running": {"done", "failed", "timeout"},
#        "done": frozenset(), "failed": frozenset(), "timeout": frozenset()}
#   enqueue_run(task_id: str) -> str
#       Registers a run for an existing task and returns its run_id. Sync —
#       it is what the 202 handler calls before the coroutine is scheduled.
#   async execute_command(command: str, *, timeout: float) -> dict
#       Runs the command with
#           asyncio.create_subprocess_exec(*shlex.split(command))
#       wrapped in asyncio.wait_for(..., timeout). On timeout it MUST call
#       process.kill() and then `await process.wait()` before returning
#       state="timeout" (SPEC line 149). Returns keys:
#           state, exit_code, stdout_tail, stderr_tail, duration_ms, finished_at
#       IMPORTANT: call it as the module attribute
#       `asyncio.create_subprocess_exec(...)`, NOT via
#       `from asyncio import create_subprocess_exec` — the timeout test spies
#       on the attribute to prove no orphan process is left behind.
#   async run_task(task_id: str, *, timeout: float | None = None) -> dict
#       Drives the full state machine for one run, persists the result via
#       repository.results.insert_result, and returns
#           {"run_id": str, "transitions": list[str], "state": str,
#            "exit_code": int, "stdout_tail": str, "stderr_tail": str,
#            "duration_ms": int, "finished_at": str}
#       `timeout` defaults to config.TASKQ_TASK_TIMEOUT.
#   _reset_state() -> None   (optional test seam, mirrors repository.tasks)
#
# taskq_api.repository.results:
#   insert_result(session, *, run_id: str, task_id: str, exit_code: int,
#                 stdout_tail: str, stderr_tail: str, duration_ms: int,
#                 finished_at: str) -> str
#   fetch_results_for_task(session, task_id: str) -> list[dict]
#       Newest first (SPEC line 99). Each dict carries the FR-07 v3 columns:
#       run_id, task_id, exit_code, stdout_tail, stderr_tail, duration_ms,
#       finished_at.
#   _reset_state() -> None   (optional test seam)
#
# taskq_api.api.runs:
#   router: fastapi.APIRouter mounted under /v1 exposing
#       POST /tasks/{task_id}/run   -> 202 {"run_id": ...}          (scope write)
#       GET  /tasks/{task_id}/runs  -> 200 {"items": [...]}         (scope read)
#   Phase-3 scope stub follows the FR-01 convention already in api/tasks.py:
#   the X-Test-Scope header carries one of none|read|write|admin and an ABSENT
#   header means "read". A caller whose scope does not include `read` gets 403
#   + problem+json whose body MUST NOT echo the task id (FR-04 / NP-08).
#   Replace the stub with require_scope(...) when FR-03/FR-04 land.
# ---------------------------------------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# Built by concatenation so the forbidden literal never appears verbatim in
# this file — SPEC.md line 188 bans it codebase-wide, and the docstring-aware
# grep in harness/core/audit does flag inline string literals.
_FORBIDDEN_SHELL_KWARG = "shell=" + "True"


# ---------------------------------------------------------------------------
# Test app wiring + in-process ASGI client
# ---------------------------------------------------------------------------


def _build_test_app():
    """Build a FastAPI app mounting the FR-01 tasks router and FR-02 runs router.

    FR-01's router is mounted only so the tests can seed a real task to run
    against; FR-03/04/05/09 routers stay unmounted so FR-02 tests are not
    coupled to cross-cutting concerns that have not landed yet.
    """  # NFR-10
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(tasks_router, prefix="/v1")
    app.include_router(runs_router, prefix="/v1")
    return app


def _call(app, method: str, url: str, **kwargs):
    """Issue one in-process ASGI request and return the httpx.Response.

    Uses ``AsyncClient.request`` rather than the ``.post`` / ``.get`` verb
    helpers on purpose: ``tests/in_memory_db_stub`` (FR-01) monkeypatches those
    verbs onto a sync adapter for the whole session, and calling a patched verb
    from inside a coroutine would nest event loops. ``request`` is left
    untouched by that stub, so this helper behaves identically whether or not
    FR-01's tests ran first.

    Citations: SAD.md §2.4 — httpx is the test-only client surface; NFR-10 —
    in-process ASGI keeps integration coverage measurable.
    """  # NFR-10

    async def _go():
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            return await http_client.request(method, url, **kwargs)

    return asyncio.run(_go())


@pytest.fixture
def app():
    """Function-scoped app so no per-test routing state can leak."""  # NFR-10
    return _build_test_app()


@pytest.fixture(autouse=True)
def _reset_fr02_state():
    """Reset the task + result stores before each FR-02 test.

    Function-scoped and autouse so a run persisted by one test cannot change
    the ordering assertions of another (v2.13.0: no module-scoped fixtures for
    stateful stores). The repo-level conftest only resets FR-01 nodes, so
    FR-02 resets its own stores here rather than editing that shared file.
    """  # NFR-09
    import taskq_api.repository.results as _results
    import taskq_api.repository.tasks as _tasks

    for module in (_tasks, _results):
        reset = getattr(module, "_reset_state", None)
        if reset is not None:
            reset()
    yield


def _seed_task(app, *, name: str, command: str) -> str:
    """Create a task through the FR-01 endpoint and return its id.

    TEST_SPEC.md §1 FR-02 lists ``task_id="task-uuid-002"`` in its Inputs
    column; that literal is a placeholder for "a task that exists", not a
    magic id the implementation must seed. Creating the row through the real
    POST /v1/tasks handler keeps this file self-contained and avoids pinning
    GREEN to a hard-coded fixture id.
    """  # NFR-10
    created = _call(app, "POST", "/v1/tasks", json={"name": name, "command": command})
    assert created.status_code == 201, created.text
    return created.json()["id"]


# ---------------------------------------------------------------------------
# AC-2.1 — POST /v1/tasks/{id}/run returns 202 with a run_id
# ---------------------------------------------------------------------------


def test_run_task_202_returns_run_id(app):
    """AC-2.1: POST /v1/tasks/{id}/run with write scope returns 202 + run_id.

    TEST_SPEC inputs: task_id="task-uuid-002"; command="echo hi";
    expected_status="202"; expected_run_id_present="true".
    Sub-assertions FR02-run-202-status and FR02-run-202-run-id-present.

    Citations: SPEC.md line 95 — "POST /v1/tasks/{id}/run(scope write)→
    HTTP 202 Accepted,body 含 run_id".
    """  # NFR-09 NFR-10
    task_id = _seed_task(app, name="run-202", command="echo hi")

    response = _call(
        app,
        "POST",
        f"/v1/tasks/{task_id}/run",
        headers={"x-test-scope": "write"},
    )

    # Sub-assertion FR02-run-202-status: expected_status == "202".
    # 202 (not 200/201) is the contract — execution is asynchronous.
    assert response.status_code == 202, response.text
    body = response.json()
    # Sub-assertion FR02-run-202-run-id-present: expected_run_id_present == "true".
    assert "run_id" in body, f"202 body must contain run_id, got keys {sorted(body)}"
    assert isinstance(body["run_id"], str) and body["run_id"], (
        "run_id must be a non-empty string"
    )


# ---------------------------------------------------------------------------
# AC-2.2 — the forbidden shell kwarg appears nowhere under 03-development/src/
# ---------------------------------------------------------------------------


def test_no_shell_true_in_source_tree():
    """AC-2.2: grep 03-development/src/ for the shell kwarg → 0 hits, and the
    runner really does use create_subprocess_exec + shlex.split.

    TEST_SPEC inputs: grep_pattern="shell=True"; expected_match_count="0".
    Sub-assertion FR02-no-shell-true-zero-matches.

    The grep half alone would pass vacuously on an empty source tree, so this
    test also asserts the positive form of the requirement: the runner module
    exists and spawns via ``asyncio.create_subprocess_exec`` with
    ``shlex.split``. Both halves must hold.

    Citations: SPEC.md line 96 — "禁 shell=True"; SPEC.md §8 #16 — grep gate,
    0 命中; SAD.md §2.5 — "All shell-outs use shlex.split(command) with *args".
    """  # NFR-09
    assert _SRC_ROOT.is_dir(), f"source tree not found at {_SRC_ROOT}"

    offenders = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_SHELL_KWARG in line:
                offenders.append(f"{py_file.relative_to(_SRC_ROOT)}:{lineno}")

    # Sub-assertion FR02-no-shell-true-zero-matches: expected_match_count == "0".
    assert offenders == [], (
        f"{_FORBIDDEN_SHELL_KWARG} is forbidden anywhere under "
        f"03-development/src/ (SPEC §8 #16); found at: {offenders}"
    )

    # Positive half — the mandated spawn form is actually present.
    runner_source = Path(runner_module_file()).read_text(encoding="utf-8")
    assert "create_subprocess_exec" in runner_source, (
        "service/runner.py must spawn via asyncio.create_subprocess_exec "
        "(SPEC.md line 96)"
    )
    assert "shlex.split" in runner_source, (
        "service/runner.py must split the command with shlex.split "
        "(SPEC.md line 96)"
    )


def runner_module_file() -> str:
    """Return the on-disk path of the SAB-declared taskq_api.service.runner.

    Kept as a helper (not a fixture) so the static test above reads as one
    linear assertion sequence.
    """  # NFR-11
    import taskq_api.service.runner as _runner

    return _runner.__file__


# ---------------------------------------------------------------------------
# AC-2.3 — timeout kills the subprocess and leaves no orphan
# ---------------------------------------------------------------------------


def test_runner_timeout_kills_subprocess_no_orphan(monkeypatch):
    """AC-2.3: on timeout the runner kills the child and reaps it — no orphan.

    TEST_SPEC inputs: task_id="task-uuid-003"; command="sleep 60";
    timeout_sec="10"; expected_state="timeout"; expected_orphan_pgid_count="0".
    Sub-assertions FR02-timeout-state-eq-timeout and FR02-timeout-no-orphan-pgid.

    SPEC_AMBIGUITY: ``timeout_sec="10"`` is the *production* default —
    ``config.TASKQ_TASK_TIMEOUT`` (SPEC.md §5.1). Sleeping 10 s of real wall
    clock inside the suite buys nothing, so this test asserts that default
    explicitly AND drives the kill path with a 0.5 s override. Neither
    threshold is weakened: expected_state is still exactly "timeout" and the
    orphan count is still exactly 0.

    Citations: SPEC.md line 96 (timeout is TASKQ_TASK_TIMEOUT); SPEC.md
    line 149 — "process.kill() 後 await process.wait(),不得留下孤兒進程".
    """  # NFR-09 NFR-03
    # The production default named by the TEST_SPEC Inputs row.
    assert config.TASKQ_TASK_TIMEOUT == 10.0, (
        "TASKQ_TASK_TIMEOUT is the FR-02 task timeout and defaults to 10 s "
        "(SPEC.md §5.1)"
    )

    # Spy on the spawn call so we can inspect the child process afterwards.
    # GREEN TODO: runner must call asyncio.create_subprocess_exec via the
    # module attribute for this spy to see the process (see contract block).
    spawned = []
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)

    result = asyncio.run(execute_command("sleep 60", timeout=0.5))

    # Sub-assertion FR02-timeout-state-eq-timeout: expected_state == "timeout".
    assert result["state"] == "timeout", (
        f"a command exceeding its timeout must end in state 'timeout', "
        f"got {result['state']!r}"
    )
    # The runner must not have waited out the full 60 s sleep.
    assert result["duration_ms"] < 10_000, (
        "timeout path must abort the child, not wait for it to exit; "
        f"duration_ms={result['duration_ms']}"
    )

    # Sub-assertion FR02-timeout-no-orphan-pgid: expected_orphan_pgid_count == "0".
    assert len(spawned) == 1, (
        f"exactly one child process should be spawned, saw {len(spawned)}"
    )
    child = spawned[0]
    # `await process.wait()` after `process.kill()` sets returncode and reaps
    # the child; without it the child lingers as a zombie (SPEC.md line 149).
    assert child.returncode is not None, (
        "runner must `await process.wait()` after process.kill() — returncode "
        "is still None, so the child was never reaped"
    )
    with pytest.raises(ProcessLookupError):
        # A live *or* zombie child would still be signalable; ProcessLookupError
        # is the proof that zero orphan processes remain.
        os.kill(child.pid, 0)


# ---------------------------------------------------------------------------
# AC-2.4 — the run result lands in task_results with all five fields
# ---------------------------------------------------------------------------


def test_run_result_persists_to_task_results_table(app):
    """AC-2.4: a successful run writes a task_results row with all five fields.

    TEST_SPEC inputs: task_id="task-uuid-002"; expected_exit_code="0";
    expected_stdout_tail_present="true"; expected_finished_at_present="true".
    Sub-assertions FR02-result-persists-exit-code-0,
    FR02-result-stdout-tail-present and FR02-result-finished-at-present.

    The run is driven through ``run_task`` (awaited to completion) rather than
    the fire-and-forget 202 endpoint, so the assertion is deterministic instead
    of racing the background TaskGroup.

    Citations: SPEC.md line 98 — 執行結果寫入 task_results 表(FR-07 v3
    schema),欄位:exit_code / stdout_tail / stderr_tail / duration_ms /
    finished_at; SAD.md §3.3 — v3 task_results column list.
    """  # NFR-09 NFR-10
    task_id = _seed_task(app, name="persist-result", command="echo hi")

    run = asyncio.run(run_task(task_id))
    assert run["state"] == "done", f"echo hi must succeed, got {run['state']!r}"

    rows = fetch_results_for_task(None, task_id)
    assert len(rows) == 1, f"exactly one task_results row expected, got {len(rows)}"
    row = rows[0]

    # All five FR-07 v3 columns must be present on the row.
    for column in (
        "exit_code",
        "stdout_tail",
        "stderr_tail",
        "duration_ms",
        "finished_at",
    ):
        assert column in row, (
            f"task_results row is missing the {column!r} column "
            f"(SPEC.md line 98); got keys {sorted(row)}"
        )

    # Sub-assertion FR02-result-persists-exit-code-0: expected_exit_code == "0".
    assert row["exit_code"] == 0
    # Sub-assertion FR02-result-stdout-tail-present.
    assert row["stdout_tail"] is not None and "hi" in row["stdout_tail"], (
        f"stdout_tail must capture the command output, got {row['stdout_tail']!r}"
    )
    # stderr_tail is captured too — empty string is populated, None is not.
    assert row["stderr_tail"] is not None, "stderr_tail must be captured, not None"
    # duration_ms is a real measurement, not a placeholder.
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0, (
        f"duration_ms must be a non-negative int, got {row['duration_ms']!r}"
    )
    # Sub-assertion FR02-result-finished-at-present.
    assert row["finished_at"], "finished_at must be populated on a finished run"


# ---------------------------------------------------------------------------
# AC-2.5 — GET /v1/tasks/{id}/runs is newest-first and needs read scope
# ---------------------------------------------------------------------------


def test_list_runs_ordered_newest_first(app):
    """AC-2.5: GET /v1/tasks/{id}/runs returns history newest-first (read scope).

    TEST_SPEC inputs: task_id="task-uuid-002"; num_runs="3";
    expected_first_run_index="2"; expected_last_run_index="0".
    Sub-assertions FR02-list-newest-first and FR02-list-three-runs.

    Three runs are executed to completion in creation order (0, 1, 2); the
    endpoint must return them reversed — run 2 first, run 0 last. The 403 half
    of AC-2.5 is asserted here as well, including the FR-04 / NP-08 rule that a
    403 body must not disclose whether the task exists.

    Citations: SPEC.md line 99 — "GET /v1/tasks/{id}/runs(scope read)→
    該任務的歷史執行紀錄,新到舊排序"; SPEC.md line 105 — scope 不足 → 403,
    body 不得洩漏資源是否存在.
    """  # NFR-09 NFR-10
    task_id = _seed_task(app, name="list-runs", command="echo hi")

    # Sub-assertion FR02-list-three-runs: num_runs == "3", in creation order.
    created_run_ids = []
    for _ in range(3):
        run = asyncio.run(run_task(task_id))
        created_run_ids.append(run["run_id"])
    assert len(set(created_run_ids)) == 3, "each run must get a distinct run_id"

    response = _call(
        app,
        "GET",
        f"/v1/tasks/{task_id}/runs",
        headers={"x-test-scope": "read"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3, f"3 historical runs expected, got {len(items)}"

    returned_run_ids = [item["run_id"] for item in items]
    # Sub-assertion FR02-list-newest-first: expected_first_run_index == "2"
    # and expected_last_run_index == "0" — i.e. creation order reversed.
    assert returned_run_ids == list(reversed(created_run_ids)), (
        f"runs must be newest-first; created {created_run_ids}, "
        f"got {returned_run_ids}"
    )
    assert returned_run_ids[0] == created_run_ids[2]
    assert returned_run_ids[-1] == created_run_ids[0]

    # AC-2.5 authz half — a key without `read` scope gets 403, and the body
    # must not leak the task id (FR-04 / NP-08).
    denied = _call(
        app,
        "GET",
        f"/v1/tasks/{task_id}/runs",
        headers={"x-test-scope": "none"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.headers["content-type"].startswith("application/problem+json")
    assert task_id not in denied.text, (
        "403 body must NOT contain the task id (existence leak, FR-04 / NP-08)"
    )


# ---------------------------------------------------------------------------
# AC-2.6 — state machine: pending → running → done | failed | timeout
# ---------------------------------------------------------------------------


def test_task_state_machine_transitions(app):
    """AC-2.6: the run walks pending → running → done and never leaves the set.

    TEST_SPEC inputs: task_id="task-uuid-002";
    expected_transition_sequence="pending,running,done".
    Sub-assertion FR02-state-machine-three-transitions
    (``expected_transition_sequence.count(",") == 2`` → three states).

    Beyond the happy sequence this pins the state machine's shape: the legal
    state set is exactly the five SPEC.md line 97 names, terminal states have
    no outgoing edges, and every observed hop is a declared edge — that is what
    "never enters an undefined state" means operationally.

    Citations: SPEC.md line 97 — 狀態機:pending → running → done | failed |
    timeout; TEST_SPEC.md §1 FR-02 row 6 (NP-13).
    """  # NFR-09 NFR-10
    task_id = _seed_task(app, name="state-machine", command="echo hi")

    run = asyncio.run(run_task(task_id))
    transitions = run["transitions"]

    # Sub-assertion FR02-state-machine-three-transitions: three states.
    assert transitions == ["pending", "running", "done"], (
        f"expected_transition_sequence 'pending,running,done', got {transitions}"
    )
    assert len(transitions) == 2 + 1

    # The declared state set is exactly the five names in SPEC.md line 97.
    assert set(VALID_STATES) == {"pending", "running", "done", "failed", "timeout"}

    # No observed state may fall outside the declared set.
    for state in transitions:
        assert state in VALID_STATES, f"undefined state {state!r} observed"

    # Every observed hop must be a declared edge.
    for previous, following in zip(transitions, transitions[1:]):
        assert following in ALLOWED_TRANSITIONS[previous], (
            f"undeclared transition {previous!r} → {following!r}"
        )

    # Shape of the machine itself: the only exit from pending is running, and
    # running fans out to exactly the three terminal outcomes.
    assert set(ALLOWED_TRANSITIONS["pending"]) == {"running"}
    assert set(ALLOWED_TRANSITIONS["running"]) == {"done", "failed", "timeout"}
    for terminal in ("done", "failed", "timeout"):
        assert set(ALLOWED_TRANSITIONS[terminal]) == set(), (
            f"{terminal!r} is terminal and must have no outgoing transitions"
        )
