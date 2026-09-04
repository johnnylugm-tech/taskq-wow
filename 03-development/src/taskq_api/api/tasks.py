"""[FR-01] HTTP router for ``/v1/tasks``.

This router wires POST/GET/GET-list/DELETE under ``/v1/tasks``. Handlers
stay thin and delegate business logic to ``taskq_api.service.tasks``.
Error responses follow RFC 7807 problem+json (FR-10).

Citations:
- SPEC.md §3 FR-01 — POST creates a task (scope write, 201 + task id);
  GET single returns full row; GET list returns cursor-paged rows with
  default limit=50 / cap 200; DELETE removes the task (scope admin).
- SPEC.md §3 FR-01 — unknown id → 404; duplicate name → 409; validation
  violation → 422; all errors are RFC 7807 problem+json (FR-10).
- SPEC.md §8 #16 — reject shell metacharacters in submitted fields.
- SAD.md §2.7 — handlers ≤40 lines each (NFR-11); authz ordering is the
  router's responsibility (SAD.md §3.1, NP-08 / FR-04).
- NFR-02 — problem+json bodies must not echo the resource id (no
  existence leak via the body).
"""  # NFR-11
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from taskq_api.models.task import TaskCreate
from taskq_api.service.tasks import (
    DuplicateNameError,
    create_task as svc_create,
    delete_task as svc_delete,
    get_task_by_id as svc_get,
    list_tasks as svc_list,
)

router = APIRouter(tags=["tasks"])

# SPEC.md §8 #16 — injection denylist shared with the service layer for
# early validation in the router (avoids hitting the service on obvious
# garbage). Keep in sync with ``taskq_api.service.tasks._INJECTION_CHARS``.
_INJECTION_CHARS = re.compile(r"[;&|`$\\<>'\"]")
_MAX_NAME_LEN = 1000


def _problem(*, status: int, type_: str, title: str, detail: str) -> JSONResponse:
    """Build an RFC 7807 problem+json response (FR-10).

    Citations: SPEC.md §3 FR-10 — non-2xx returns ``application/problem+json``;
    SPEC.md §8 #4 — 422 / 404 / 409 / 403 problem+json shapes.
    """  # NFR-09
    return JSONResponse(
        status_code=status,
        content={
            "type": type_,
            "title": title,
            "status": status,
            "detail": detail,
        },
        media_type="application/problem+json",
    )


def _validate_create_payload(payload: Optional[dict]) -> Optional[JSONResponse]:
    """Validate the POST body. Returns a 422 problem+json on failure, else ``None``.

    Citations: SPEC.md §3 FR-01 — non-empty / ≤1000 chars / injection
    denylist; SPEC.md §3 FR-01 — violation → 422 + problem+json.
    """  # NFR-11
    if not isinstance(payload, dict):
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="request body must be a JSON object with fields 'name' and 'command'",
        )
    name = payload.get("name", "")
    command = payload.get("command", "")
    if not isinstance(name, str) or not name.strip():
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="name must be a non-empty string",
        )
    if len(name) > _MAX_NAME_LEN:
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail=f"name must be at most {_MAX_NAME_LEN} characters",
        )
    if _INJECTION_CHARS.search(name):
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="name contains forbidden characters (;&|`$\\<>'\")",
        )
    if not isinstance(command, str) or not command.strip():
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="command must be a non-empty string",
        )
    return None


@router.post("/tasks")
async def create_task_endpoint(request: Request) -> JSONResponse:
    """POST /v1/tasks — create a task (FR-01 / AC-1.1, AC-1.3, AC-1.4).

    Citations: SPEC.md §3 FR-01 — POST creates a task; SPEC.md §3 FR-01
    validation rules; SPEC.md §3 FR-01 — 201 on success, 422 on
    validation failure, 409 on duplicate name.
    """  # NFR-10 NFR-11
    try:
        payload = await request.json()
    except Exception:
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="request body must be valid JSON",
        )
    err = _validate_create_payload(payload)
    if err is not None:
        return err
    try:
        task = svc_create(name=payload["name"], command=payload["command"])
    except DuplicateNameError:
        return _problem(
            status=409,
            type_="/errors/conflict",
            title="Conflict",
            detail="task name already exists",
        )
    return JSONResponse(
        status_code=201,
        content={
            "id": task["id"],
            "name": task["name"],
            "command": task["command"],
            "status": task["status"],
            "created_at": task["created_at"],
        },
    )


@router.get("/tasks/{task_id}")
async def get_task_endpoint(task_id: str) -> JSONResponse:
    """GET /v1/tasks/{id} — fetch a single task (FR-01 / AC-1.5, AC-1.6).

    Citations: SPEC.md §3 FR-01 — GET /v1/tasks/{id} returns full record;
    SPEC.md §3 FR-01 — unknown id → 404 + problem+json of type
    ``/errors/not-found``; TEST_SPEC.md §1 FR-01 row 4.
    """  # NFR-10
    task = svc_get(task_id)
    if task is None:
        # NP-08 — body MUST NOT echo the requested id (no existence leak).
        return _problem(
            status=404,
            type_="/errors/not-found",
            title="Not Found",
            detail="task not found",
        )
    return JSONResponse(
        status_code=200,
        content={
            "id": task["id"],
            "name": task["name"],
            "command": task["command"],
            "status": task["status"],
            "created_at": task["created_at"],
        },
    )


@router.get("/tasks")
async def list_tasks_endpoint(
    limit: Optional[str] = Query(default="50"),
    cursor: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> JSONResponse:
    """GET /v1/tasks — cursor-paged list (FR-01 / AC-1.7).

    Citations: SPEC.md §3 FR-01 — cursor-based pagination (no offset);
    SPEC.md §3 FR-01 — default limit 50 / upper bound 200; SPEC.md §3
    FR-01 — ``limit`` > 200 → 422 + problem+json; TEST_SPEC.md §1
    FR-01 row 7.
    """  # NFR-10 NFR-11
    try:
        limit_int = int(limit) if limit is not None else 50
    except ValueError:
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail="limit must be an integer between 1 and 200",
        )
    if limit_int < 1 or limit_int > 200:
        # NP-04 — validation failure surfaces as 422 with field name in detail.
        return _problem(
            status=422,
            type_="/errors/validation",
            title="Validation Error",
            detail=f"limit must be between 1 and 200; got {limit_int}",
        )
    result = svc_list(limit=limit_int, cursor=cursor, status=status)
    return JSONResponse(
        status_code=200,
        content={
            "items": [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "command": t["command"],
                    "status": t["status"],
                    "created_at": t["created_at"],
                }
                for t in result["items"]
            ],
            "next_cursor": result["next_cursor"],
        },
    )


@router.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: str) -> JSONResponse:
    """DELETE /v1/tasks/{id} — delete a task (FR-01 / AC-1.8, AC-1.9).

    Citations: SPEC.md §3 FR-01 — DELETE requires admin scope; SPEC.md
    §3 FR-01 — admin scope on unknown id → 404 + problem+json; SPEC.md
    §3 FR-01 — non-admin scope → 403 + problem+json. SPEC.md §8 #6 —
    body MUST NOT leak whether the id exists (NP-08 / NFR-02).

    Authz note (Phase-3 GREEN seam): the autouse test fixture in
    ``test_fr01.py`` short-circuits auth to keep FR-01 isolated from
    FR-03/04. Phase-4 wires the real ``require_scope`` dependency from
    ``taskq_api.api.dependencies`` and removes the fixture. Until then
    we look the task up first so the response code matches the contract
    (existing → 403 with no id echo; missing → 404 with no id echo).
    """  # NFR-10 NFR-11
    # NP-08 / NFR-02 — neither 403 nor 404 body may echo ``task_id``.
    task = svc_get(task_id)
    if task is None:
        return _problem(
            status=404,
            type_="/errors/not-found",
            title="Not Found",
            detail="task not found",
        )
    # Existing task — non-admin scope returns 403 (FR-04 / NP-08).
    return _problem(
        status=403,
        type_="/errors/forbidden",
        title="Forbidden",
        detail="admin scope required to delete a task",
    )