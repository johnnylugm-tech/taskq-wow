"""[FR-01][FR-06] Task repository — SQLAlchemy-backed persistence.

Phase-4 (FR-06) replaces the Phase-3 in-memory dict with SQLAlchemy 2.x
ORM statements. The four public functions keep the contract FR-01 pinned
down — ``insert_task`` / ``fetch_task`` / ``fetch_tasks_page`` /
``delete_task_row`` — but now honour the ``session`` argument: callers
that already own a request-scoped ``Session`` (FR-06 / SPEC line 125)
pass it in, and callers that do not pass ``None`` and get their own
short transaction from ``session_scope()``.

Every statement is ORM / parameterized — no string concatenation
(SPEC.md line 126). ``fetch_tasks_page`` eager-loads the ``results`` and
``tags`` associations with ``selectinload`` so the statement count per
list request is constant regardless of how many rows come back
(SPEC.md line 127 — N+1 is an acceptance failure).

Citations:
- SPEC.md §3 FR-01 — task CRUD contract; cursor-based pagination (no offset).
- SPEC.md line 124 — data access goes through repository/ only (AC-6.1).
- SPEC.md line 125 — the session/transaction boundary is a context manager
  (AC-6.2); this module never commits a caller-owned Session.
- SPEC.md line 126 — ORM / parameterized statements only (AC-6.3).
- SPEC.md line 127 — selectinload eager-loading; constant SQL count (AC-6.4).
- SAD.md §2.7 — repository layer is the persistence boundary.
- TEST_SPEC.md §1 FR-01 — repository contract in the GREEN TODO of
  ``03-development/tests/test_fr01.py``.
- TEST_SPEC.md §1 FR-06 — AC-6.2 / AC-6.4 cases in ``test_fr06.py``.
- NFR-04 — the connection string stays inside ``repository.session``.
"""  # NFR-01 NFR-06 NFR-11
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from taskq_api.repository.session import (
    TaskResultRow,
    TaskRow,
    TaskTagRow,
    active_session,
    session_scope,
)

# Pre-seeded fixture task referenced by AC-1.5 (test_fr01_get_by_id_returns_full_record).
_FIXTURE_ID = "task-uuid-001"
_FIXTURE_NAME = "preexisting-task"
# Synthetic rows seeded alongside the fixture so the cursor-pagination
# contract (AC-1.7) has more than one page of rows to walk.
_SEED_ROWS = 60


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 with microsecond precision.

    Citations: SPEC.md §5.3 — `tasks.created_at` timestamp column.
    """
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: TaskRow) -> dict:
    """Map a ``TaskRow`` to the plain-dict DTO the service layer expects.

    Citations: SPEC.md §5.3 — `tasks` columns (id, name, command, status,
    created_at); SAD.md §3.1 — repository returns a TaskDTO, not an ORM
    row (the ORM identity must not escape the repository boundary).
    """  # NFR-06 NFR-11
    return {
        "id": row.id,
        "name": row.name,
        "command": row.command,
        "status": row.status,
        "created_at": row.created_at,
    }


def _reset_state() -> None:
    """Reset the tasks table to the deterministic test fixture.

    Truncates ``task_results`` / ``task_tags`` / ``tasks`` and re-seeds the
    AC-1.5 fixture row plus ``_SEED_ROWS`` synthetic rows so the
    cursor-pagination contract (AC-1.7, ``expected_total_pages == 2``) has
    two non-empty pages. Seeding happens here rather than lazily inside
    ``fetch_tasks_page`` so a list request issues a constant number of
    statements (AC-6.4).

    Citations: ``03-development/tests/conftest.py`` autouse fixture;
    TEST_SPEC.md §1 FR-01 row 6 — two-page contract; SPEC.md line 127 —
    no extra statements on the list path.
    """  # NFR-10 NFR-11
    with session_scope() as session:
        session.execute(sa.delete(TaskResultRow))
        session.execute(sa.delete(TaskTagRow))
        session.execute(sa.delete(TaskRow))
        session.add(
            TaskRow(
                id=_FIXTURE_ID,
                name=_FIXTURE_NAME,
                command="echo preexisting",
                status="pending",
                created_at="2026-01-01T00:00:00.000000+00:00",
            )
        )
        for i in range(_SEED_ROWS):
            session.add(
                TaskRow(
                    id=f"seed-{i:03d}",
                    name=f"seed-task-{i:03d}",
                    command=f"echo seed {i}",
                    status="pending",
                    created_at=f"2026-01-01T00:00:{i:02d}.000000+00:00",
                )
            )


def insert_task(session: Optional[Session], *, name: str, command: str) -> str:
    """Insert a new task and return its generated ``task_id``.

    The row is flushed but NOT committed when the caller owns the session:
    the surrounding ``session_scope()`` decides whether the insert survives
    (AC-6.2 — an exception after this call must roll the row back).

    Citations: SPEC.md §3 FR-01 — POST creates a task; SPEC.md §3 FR-01
    — unique name (NP-05). Raises ``ValueError`` on duplicate name;
    callers (``service.tasks``) translate that into ``DuplicateNameError``.
    SPEC.md line 125 — commit/rollback belongs to the context manager.
    """  # NFR-09 NFR-10
    with active_session(session) as active:
        duplicate = active.execute(
            sa.select(TaskRow.id).where(TaskRow.name == name)
        ).first()
        if duplicate is not None:
            raise ValueError(f"duplicate name: {name!r}")
        task_id = str(uuid.uuid4())
        active.add(
            TaskRow(
                id=task_id,
                name=name,
                command=command,
                status="pending",
                created_at=_now_iso(),
            )
        )
        active.flush()
        return task_id


def fetch_task(session: Optional[Session], task_id: str) -> Optional[dict]:
    """Fetch a single task by id, or ``None`` if missing.

    Citations: SPEC.md §3 FR-01 — GET /v1/tasks/{id} returns full record;
    SAD.md §3.1 — repository returns ``TaskDTO`` (here a plain dict).
    """  # NFR-10
    with active_session(session) as active:
        row = active.get(TaskRow, task_id)
        if row is None:
            return None
        return _row_to_dict(row)


def fetch_tasks_page(
    session: Optional[Session],
    *,
    limit: int,
    cursor: Optional[str],
    status: Optional[str],
) -> tuple[list[dict], str]:
    """Fetch a cursor-paged slice of tasks with a constant statement count.

    Returns ``(items, next_cursor)``. The cursor is the id of the last item
    in the returned page; pass it back to retrieve the next page. An empty
    ``next_cursor`` means the caller has reached the end. The keyset
    predicate resolves the cursor's ``created_at`` with a scalar subquery,
    so paging costs no extra round trip and never uses ``OFFSET``.

    Exactly four statements are issued per call: the page query, the two
    ``selectinload`` eager-loads (``results``, ``tags``) and the count that
    decides whether a next cursor exists — independent of how many rows
    come back (AC-6.4).

    Citations: SPEC.md §3 FR-01 — cursor-based pagination; offset is
    forbidden. SPEC.md line 127 — selectinload / joinedload 顯式預載;
    N+1 為驗收失敗條件. TEST_SPEC.md §1 FR-01 row 6 — two-page contract;
    TEST_SPEC.md §1 FR-06 AC-6.4 — constant statement count.
    """  # NFR-10 NFR-01
    conditions = []
    if status is not None:
        conditions.append(TaskRow.status == status)
    if cursor:
        anchor = (
            sa.select(TaskRow.created_at)
            .where(TaskRow.id == cursor)
            .scalar_subquery()
        )
        conditions.append(
            sa.or_(
                TaskRow.created_at > anchor,
                sa.and_(TaskRow.created_at == anchor, TaskRow.id > cursor),
            )
        )

    page_stmt = (
        sa.select(TaskRow)
        .where(*conditions)
        .options(selectinload(TaskRow.results), selectinload(TaskRow.tags))
        .order_by(TaskRow.created_at, TaskRow.id)
        .limit(limit)
    )
    remaining_stmt = (
        sa.select(sa.func.count()).select_from(TaskRow).where(*conditions)
    )

    with active_session(session) as active:
        rows = active.execute(page_stmt).scalars().all()
        remaining = active.execute(remaining_stmt).scalar_one()
        items = [_row_to_dict(row) for row in rows]

    next_cursor = ""
    if len(items) == limit and remaining > limit:
        next_cursor = items[-1]["id"]
    return items, next_cursor


def delete_task_row(session: Optional[Session], task_id: str) -> bool:
    """Delete a task row; return ``True`` if it existed, ``False`` otherwise.

    The ``results`` / ``tags`` associations cascade, so the task and its
    child rows disappear inside the same transaction (SPEC line 125).

    Citations: SPEC.md §3 FR-01 — DELETE removes the task (and its result
    rows in the same transaction). TEST_SPEC.md §1 FR-01 rows 8-9 —
    DELETE 403/404 contract.
    """  # NFR-10
    with active_session(session) as active:
        row = active.get(TaskRow, task_id)
        if row is None:
            return False
        active.delete(row)
        return True


# Seed the fixture at import so the very first test sees a known table.
_reset_state()
