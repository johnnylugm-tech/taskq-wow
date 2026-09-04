"""[FR-02] In-memory task_results repository — Phase-3 GREEN backing store.

This module persists run results in process-local state so FR-02 tests can
run without a real database. Phase-4 replaces this with SQLAlchemy 2.x +
Alembic per FR-06; the two functions below are the contract the new
SQL-backed module must preserve.

The ``session`` argument is accepted for parity with the Phase-4 SQL
contract (FR-06 — repository owns the session boundary) and is ignored in
the in-memory implementation.

Citations:
- SPEC.md line 98 — results written to ``task_results`` table (FR-07 v3
  schema): ``exit_code`` / ``stdout_tail`` / ``stderr_tail`` /
  ``duration_ms`` / ``finished_at``.
- SPEC.md line 99 — GET /v1/tasks/{id}/runs returns the historical records
  newest first.
- SAD.md §2.7 — repository layer is the persistence boundary.
- TEST_SPEC.md §1 FR-02 — repository contract listed in the GREEN TODO
  of ``03-development/tests/test_fr02.py``.
- NFR-04 — repository must not leak DB connection strings; in-memory
  backing has none.
"""  # NFR-11
from __future__ import annotations

import threading

_lock = threading.Lock()
# List preserves insertion order; ``fetch_results_for_task`` sorts by
# ``finished_at`` descending for the newest-first contract (SPEC line 99).
_store: list[dict] = []


def _reset_state() -> None:
    """Reset the in-memory store (test seam — FR-02 autouse fixture).

    Citations: ``03-development/tests/test_fr02.py`` autouse fixture
    ``_reset_fr02_state``; TEST_SPEC.md §1 FR-02 — each test starts from
    a clean run history so ordering assertions are deterministic.
    """
    global _store
    _store = []


def insert_result(
    session,
    *,
    run_id: str,
    task_id: str,
    exit_code: int,
    stdout_tail: str,
    stderr_tail: str,
    duration_ms: int,
    finished_at: str,
) -> str:
    """Insert a new ``task_results`` row and return its ``run_id``.

    Citations: SPEC.md line 98 — v3 schema columns written here.
    """  # NFR-09 NFR-10
    with _lock:
        _store.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "duration_ms": duration_ms,
                "finished_at": finished_at,
            }
        )
    return run_id


def fetch_results_for_task(session, task_id: str) -> list[dict]:
    """Return the run history for ``task_id``, newest first.

    Citations: SPEC.md line 99 — "GET /v1/tasks/{id}/runs (scope read) →
    該任務的歷史執行紀錄,新到舊排序"; TEST_SPEC.md §1 FR-02 AC-2.5.
    """  # NFR-09 NFR-10
    items = [row for row in _store if row["task_id"] == task_id]
    items.sort(key=lambda row: row["finished_at"], reverse=True)
    return items


# Run the reset at import so the very first test sees an empty history.
_reset_state()