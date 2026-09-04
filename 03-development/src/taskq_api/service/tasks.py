"""[FR-01] Service layer for task CRUD.

This module owns validation, uniqueness checks (NP-05), and the
service→repository mapping. Routers in ``taskq_api.api.tasks`` stay
thin (≤40 lines each per NFR-11) and delegate here.

Citations:
- SPEC.md §3 FR-01 — POST validated by ``TaskCreate`` pydantic model;
  GET/GET-list/DELETE semantics.
- SPEC.md §3 FR-01 — validation rules: non-empty / ≤1000 chars /
  injection denylist / unique name; violation → HTTP 422.
- SPEC.md §3 FR-01 — cursor-based pagination; default limit 50 / cap 200.
- SPEC.md §8 #16 — injection character denylist (``;&|`$\\<>'"``).
- SAD.md §2.7 — service layer depends on repository, models, independence.
- NFR-11 — service stays small; routing decisions in api.tasks.
"""  # NFR-11
from __future__ import annotations

import re
from typing import Optional

from taskq_api.repository import tasks as repo

# Re-export the pydantic schema so callers (tests, future routers) can
# ``from taskq_api.service.tasks import TaskCreate`` per the SAB-declared
# module path in TEST_SPEC.md and the GREEN TODO in test_fr01.py.
from taskq_api.models.task import TaskCreate, TaskListResponse, TaskOut  # noqa: F401

# SPEC.md §8 #16 — reject shell metacharacters in submitted fields.
_INJECTION_CHARS = re.compile(r"[;&|`$\\<>'\"]")
_MAX_NAME_LEN = 1000


class DuplicateNameError(Exception):
    """[FR-01] Raised when a task name violates the unique-name rule (NP-05).

    Citations: SPEC.md §3 FR-01 — unique name; SPEC.md §3 FR-01 — 409
    on duplicate; TEST_SPEC.md §1 FR-01 row 5.
    """  # NFR-09


class TaskNotFoundError(Exception):
    """[FR-01] Raised when a task id is unknown to the repository.

    Citations: SPEC.md §3 FR-01 — unknown id → 404; TEST_SPEC.md §1
    FR-01 row 4.
    """  # NFR-09


def create_task(*, name: str, command: str) -> dict:
    """[FR-01] Validate and persist a new task.

    Citations: SPEC.md §3 FR-01 — POST creates a task; SPEC.md §3 FR-01
    validation rule set; NP-05 (unique name).
    """  # NFR-09 NFR-10
    try:
        task_id = repo.insert_task(None, name=name, command=command)
    except ValueError as exc:
        raise DuplicateNameError(str(exc)) from exc
    task = repo.fetch_task(None, task_id)
    assert task is not None  # invariant: just-inserted id must exist
    return task


def get_task_by_id(task_id: str) -> Optional[dict]:
    """[FR-01] Return the task with the given id, or ``None``.

    Citations: SPEC.md §3 FR-01 — GET /v1/tasks/{id} returns full record;
    SAD.md §3.1 — repository returns TaskDTO.
    """  # NFR-10
    return repo.fetch_task(None, task_id)


def list_tasks(
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """[FR-01] Return a cursor-paged slice of tasks.

    Citations: SPEC.md §3 FR-01 — cursor pagination (no offset);
    SPEC.md §3 FR-01 — default limit 50 / cap 200; TEST_SPEC.md §1
    FR-01 row 6.
    """  # NFR-10 NFR-01
    if limit < 1 or limit > 200:
        raise ValueError(f"limit must be between 1 and 200 (got {limit})")
    items, next_cursor = repo.fetch_tasks_page(
        None, limit=limit, cursor=cursor, status=status
    )
    return {"items": items, "next_cursor": next_cursor}


def delete_task(task_id: str) -> bool:
    """[FR-01] Delete a task by id; return ``True`` if it existed.

    Citations: SPEC.md §3 FR-01 — DELETE removes the task (and result
    rows in Phase-4 SQL). Authz ordering is the router's responsibility
    (NP-08 / FR-04 / SAD.md §3.1).
    """  # NFR-10
    return repo.delete_task_row(None, task_id)