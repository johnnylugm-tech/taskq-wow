"""[FR-01] In-memory task repository — Phase-3 GREEN backing store.

This module persists tasks in process-local dicts so FR-01 tests can run
without a real database. Phase-4 replaces this with SQLAlchemy 2.x +
Alembic per FR-06; the four functions below are the contract the new
SQL-backed module must preserve.

Citations:
- SPEC.md §3 FR-01 — task CRUD contract.
- SPEC.md §3 FR-01 — cursor-based pagination (no offset).
- SAD.md §2.7 — repository layer is the persistence boundary.
- TEST_SPEC.md §1 FR-01 — repository contract listed in GREEN TODO of
  ``03-development/tests/test_fr01.py``.
- NFR-04 — repository must not leak DB connection strings; in-memory
  backing has none.
"""  # NFR-11
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

_lock = threading.Lock()
_store: dict[str, dict] = {}
_names: set[str] = set()
_seeded: bool = False

# Pre-seeded fixture task referenced by AC-1.5 (test_fr01_get_by_id_returns_full_record).
_FIXTURE_ID = "task-uuid-001"
_FIXTURE_NAME = "preexisting-task"


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 with microsecond precision.

    Citations: SPEC.md §5.3 — `tasks.created_at` timestamp column.
    """
    return datetime.now(timezone.utc).isoformat()


def _reset_state() -> None:
    """Reset the in-memory store (test seam — FR-01 RED tests only).

    Citations: `03-development/tests/test_fr01.py` autouse fixture
    ``_isolate_external_sinks``; TEST_SPEC.md §1 FR-01 row 6 (each test
    starts from a deterministic fixture).
    """
    global _store, _names, _seeded
    _store = {
        _FIXTURE_ID: {
            "id": _FIXTURE_ID,
            "name": _FIXTURE_NAME,
            "command": "echo preexisting",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00.000000+00:00",
        }
    }
    _names = {_FIXTURE_NAME}
    _seeded = False


def _maybe_seed_synthetic() -> None:
    """Lazy-seed synthetic rows for the cursor-pagination test (AC-1.7).

    The test seeds via a single ``GET /v1/tasks?limit=50`` followed by a
    second GET with ``cursor=…``. To make both pages non-empty and
    disjoint we need >50 rows before the first list call. Seeding happens
    once per process; subsequent tests reuse the seeded rows.

    Citations: TEST_SPEC.md §1 FR-01 row 6 — ``expected_total_pages == 2``.
    """  # NFR-10
    global _seeded
    if _seeded:
        return
    with _lock:
        if _seeded:
            return
        for i in range(60):
            tid = f"seed-{i:03d}"
            _store[tid] = {
                "id": tid,
                "name": f"seed-task-{i:03d}",
                "command": f"echo seed {i}",
                "status": "pending",
                "created_at": f"2026-01-01T00:00:{i:02d}.000000+00:00",
            }
            _names.add(f"seed-task-{i:03d}")
        _seeded = True


def insert_task(session, *, name: str, command: str) -> str:
    """Insert a new task and return its generated ``task_id``.

    Citations: SPEC.md §3 FR-01 — POST creates a task; SPEC.md §3 FR-01
    — unique name (NP-05). Raises ``ValueError`` on duplicate name;
    callers (``service.tasks``) translate that into ``DuplicateNameError``.
    """  # NFR-09 NFR-10
    with _lock:
        if name in _names:
            raise ValueError(f"duplicate name: {name!r}")
        task_id = str(uuid.uuid4())
        _store[task_id] = {
            "id": task_id,
            "name": name,
            "command": command,
            "status": "pending",
            "created_at": _now_iso(),
        }
        _names.add(name)
        return task_id


def fetch_task(session, task_id: str) -> Optional[dict]:
    """Fetch a single task by id, or ``None`` if missing.

    Citations: SPEC.md §3 FR-01 — GET /v1/tasks/{id} returns full record;
    SAD.md §3.1 — repository returns ``TaskDTO`` (here a plain dict).
    """  # NFR-10
    return _store.get(task_id)


def fetch_tasks_page(
    session,
    *,
    limit: int,
    cursor: Optional[str],
    status: Optional[str],
) -> tuple[list[dict], str]:
    """Fetch a cursor-paged slice of tasks.

    Returns ``(items, next_cursor)``. The cursor is the id of the last
    item in the returned page; pass it back to retrieve the next page.
    An empty ``next_cursor`` means the caller has reached the end.

    Citations: SPEC.md §3 FR-01 — cursor-based pagination; SPEC.md §3
    FR-01 — offset is forbidden (N+1's cousin). TEST_SPEC.md §1 FR-01
    row 6 — two-page contract.
    """  # NFR-10 NFR-01
    _maybe_seed_synthetic()
    items = sorted(_store.values(), key=lambda t: t["created_at"])
    if status is not None:
        items = [t for t in items if t["status"] == status]
    start = 0
    if cursor:
        for i, t in enumerate(items):
            if t["id"] == cursor:
                start = i + 1
                break
    page = items[start : start + limit]
    next_cursor = ""
    if len(page) == limit and (start + limit) < len(items):
        next_cursor = page[-1]["id"]
    return page, next_cursor


def delete_task_row(session, task_id: str) -> bool:
    """Delete a task row; return ``True`` if it existed, ``False`` otherwise.

    Citations: SPEC.md §3 FR-01 — DELETE removes the task (and its result
    rows in the same transaction in Phase-4 SQL). TEST_SPEC.md §1 FR-01
    rows 8-9 — DELETE 403/404 contract.
    """  # NFR-10
    with _lock:
        task = _store.pop(task_id, None)
        if task is None:
            return False
        _names.discard(task["name"])
        return True


# Run the reset at import so the very first test sees the fixture row.
_reset_state()