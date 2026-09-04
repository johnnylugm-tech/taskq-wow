"""[FR-01] Pydantic schemas for the task resource.

Citations:
- SPEC.md §3 FR-01 — POST body validated by ``TaskCreate`` pydantic model;
  validation rules: non-empty / ≤1000 chars / injection denylist / unique
  name. Violation → HTTP 422 + problem+json.
- SPEC.md §5.3 — `tasks` table columns `id, command, name, status,
  created_at` (the GET-by-id response shape).
- SAD.md §2.7 — models layer is the dependency sink; no imports from
  api/service/repository allowed.
"""  # NFR-11
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    """[FR-01] Input schema for ``POST /v1/tasks``.

    Citations: SPEC.md §3 FR-01 (POST body shape), SPEC.md §3 FR-01
    validation rule set, TEST_SPEC.md §1 FR-01 row 1 (inputs).
    """  # NFR-11

    name: str = Field(..., min_length=1, max_length=1000)
    command: str = Field(..., min_length=1)


class TaskOut(BaseModel):
    """[FR-01] Output schema for ``GET /v1/tasks/{id}``.

    Citations: SPEC.md §5.3 — `tasks` columns, TEST_SPEC.md §1 FR-01 row 3
    (expected_field_set = id,command,name,status,created_at).
    """  # NFR-11

    id: str
    name: str
    command: str
    status: str
    created_at: str


class TaskListResponse(BaseModel):
    """[FR-01] Output schema for ``GET /v1/tasks`` cursor-paged list.

    Citations: SPEC.md §3 FR-01 — cursor-based pagination; SPEC.md §3
    FR-01 — default ``limit`` 50 / upper bound 200; TEST_SPEC.md §1
    FR-01 row 6 (``items`` / `next_cursor`).
    """  # NFR-11

    items: list[TaskOut]
    next_cursor: Optional[str] = None