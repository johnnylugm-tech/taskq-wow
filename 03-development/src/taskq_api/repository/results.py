"""[FR-02][FR-06] Run-result repository — SQLAlchemy-backed persistence.

Run results live in the ``task_results`` table declared by
``repository.session`` (the v3 schema of SPEC.md line 98). The Phase-3
in-memory list this module used to keep has been replaced by ORM
statements against that table, so the association ``fetch_tasks_page``
eager-loads with ``selectinload`` is now the same storage the runner
writes to — one persistence seam rather than two.

Both functions honour the FR-06 session contract: a caller that already
owns a request-scoped ``Session`` passes it in and keeps the transaction
boundary; a caller that passes ``None`` borrows a short ``session_scope()``
that commits on success and rolls back on exception (SPEC line 125).

Every statement is ORM / parameterized — no string concatenation
(SPEC.md line 126).

Citations:
- SPEC.md line 98 — results written to ``task_results`` (v3 schema):
  ``exit_code`` / ``stdout_tail`` / ``stderr_tail`` / ``duration_ms`` /
  ``finished_at``.
- SPEC.md line 99 — GET /v1/tasks/{id}/runs returns the historical records
  newest first.
- SPEC.md line 124 — data access goes through repository/ only (AC-6.1).
- SPEC.md line 125 — the transaction boundary is a context manager (AC-6.2).
- SPEC.md line 126 — ORM / parameterized statements only (AC-6.3).
- SAD.md §2.7 — repository layer is the persistence boundary.
- TEST_SPEC.md §1 FR-02 — repository contract listed in the GREEN TODO
  of ``03-development/tests/test_fr02.py``.
- NFR-04 — the connection string stays inside ``repository.session``.
"""  # NFR-06 NFR-11
from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from taskq_api.repository.session import (
    TaskResultRow,
    active_session,
    session_scope,
)


def _row_to_dict(row: TaskResultRow) -> dict:
    """Map a ``TaskResultRow`` to the plain-dict DTO callers expect.

    The ORM identity must not escape the repository boundary (SAD.md §3.1),
    so the row is copied into a plain dict here.

    Citations: SPEC.md line 98 — the v3 ``task_results`` columns.
    """  # NFR-06 NFR-11
    return {
        "run_id": row.run_id,
        "task_id": row.task_id,
        "exit_code": row.exit_code,
        "stdout_tail": row.stdout_tail,
        "stderr_tail": row.stderr_tail,
        "duration_ms": row.duration_ms,
        "finished_at": row.finished_at,
    }


def _reset_state() -> None:
    """Truncate the run history (test seam — FR-02 autouse fixture).

    Citations: ``03-development/tests/test_fr02.py`` autouse fixture
    ``_reset_fr02_state``; TEST_SPEC.md §1 FR-02 — each test starts from
    a clean run history so ordering assertions are deterministic.
    """  # NFR-10 NFR-11
    with session_scope() as session:
        session.execute(sa.delete(TaskResultRow))


def insert_result(
    session: Optional[Session],
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

    The row is flushed but NOT committed when the caller owns the session:
    the surrounding ``session_scope()`` decides whether the result survives
    (AC-6.2).

    Citations: SPEC.md line 98 — v3 schema columns written here;
    SPEC.md line 125 — commit/rollback belongs to the context manager.
    """  # NFR-09 NFR-10
    with active_session(session) as active:
        active.add(
            TaskResultRow(
                run_id=run_id,
                task_id=task_id,
                exit_code=exit_code,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
                finished_at=finished_at,
            )
        )
        active.flush()
        return run_id


def fetch_results_for_task(session: Optional[Session], task_id: str) -> list[dict]:
    """Return the run history for ``task_id``, newest first.

    Ordering is done by the database (``finished_at`` descending, ``run_id``
    breaking ties) rather than in Python, so a single parameterized
    statement answers the request.

    Citations: SPEC.md line 99 — "GET /v1/tasks/{id}/runs (scope read) →
    該任務的歷史執行紀錄,新到舊排序"; TEST_SPEC.md §1 FR-02 AC-2.5.
    """  # NFR-09 NFR-10
    history_stmt = (
        sa.select(TaskResultRow)
        .where(TaskResultRow.task_id == task_id)
        .order_by(TaskResultRow.finished_at.desc(), TaskResultRow.run_id.desc())
    )
    with active_session(session) as active:
        rows = active.execute(history_stmt).scalars().all()
        return [_row_to_dict(row) for row in rows]
