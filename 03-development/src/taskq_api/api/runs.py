"""[FR-02] HTTP router for ``/v1/tasks/{id}/run`` and ``/v1/tasks/{id}/runs``.

This router wires the FR-02 task-execution endpoints. Handlers stay thin
(≤40 lines each per NFR-11) and delegate business logic to
``taskq_api.service.runner``. Error responses follow RFC 7807 problem+json
(FR-10).

The scope check uses the Phase-3 ``X-Test-Scope`` header convention (one of
``none|read|write|admin``) until FR-03/FR-04 land; absent header means
``read`` per the FR-01 convention. Replace with ``require_scope(...)`` from
``taskq_api.api.dependencies`` when FR-03/04 land.

Citations:
- SPEC.md line 95 — POST /v1/tasks/{id}/run (scope write) → 202 + run_id.
- SPEC.md line 99 — GET /v1/tasks/{id}/runs (scope read) → history newest-first.
- SPEC.md line 105 — scope不足 → 403, body不得洩漏資源是否存在 (NP-08).
- SAD.md §2.7 — handlers ≤40 lines each (NFR-11); authz ordering is the
  router's responsibility (SAD.md §3.1, NP-08 / FR-04).
- NFR-02 — problem+json bodies must not echo the resource id (no existence
  leak via the body).
"""  # NFR-11
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from taskq_api.repository import results as repo_results
from taskq_api.repository import tasks as repo_tasks
from taskq_api.service.runner import enqueue_run

router = APIRouter(tags=["runs"])


def _problem(*, status: int, type_: str, title: str, detail: str) -> JSONResponse:
    """Build an RFC 7807 problem+json response (FR-10).

    Citations: SPEC.md §3 FR-10 — non-2xx returns
    ``application/problem+json``; SPEC.md §8 #4 — 422/404/403/409 shapes.
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


def _resolve_scope(request: Request) -> str:
    """Return the Phase-3 scope stub value, defaulting to ``read``.

    Citations: FR-01 api/tasks.py — Phase-3 stub for FR-03/04 scope
    enforcement. When FR-03/04 land, replace this with the real
    ``require_scope(...)`` dependency from ``taskq_api.api.dependencies``.
    """  # NFR-11
    return request.headers.get("x-test-scope", "read")


@router.post("/tasks/{task_id}/run")
async def create_run_endpoint(task_id: str, request: Request) -> JSONResponse:
    """POST /v1/tasks/{id}/run — enqueue a run (FR-02 / AC-2.1).

    Citations: SPEC.md line 95 — POST → 202 + run_id; SPEC.md line 96 —
    async subprocess execution is scheduled by the runner (background).
    """  # NFR-10 NFR-11
    scope = _resolve_scope(request)
    if scope not in ("write", "admin"):
        # NP-08 / NFR-02 — 403 body MUST NOT echo ``task_id``.
        return _problem(
            status=403,
            type_="/errors/forbidden",
            title="Forbidden",
            detail="write scope required to run a task",
        )
    task = repo_tasks.fetch_task(None, task_id)
    if task is None:
        return _problem(
            status=404,
            type_="/errors/not-found",
            title="Not Found",
            detail="task not found",
        )
    run_id = enqueue_run(task_id)
    return JSONResponse(status_code=202, content={"run_id": run_id})


@router.get("/tasks/{task_id}/runs")
async def list_runs_endpoint(task_id: str, request: Request) -> JSONResponse:
    """GET /v1/tasks/{id}/runs — list run history (FR-02 / AC-2.5).

    Citations: SPEC.md line 99 — "GET .../runs (scope read) → 該任務的
    歷史執行紀錄,新到舊排序"; SPEC.md line 105 — scope不足 → 403,
    body不得洩漏資源是否存在 (NP-08).
    """  # NFR-10 NFR-11
    scope = _resolve_scope(request)
    if scope not in ("read", "write", "admin"):
        # NP-08 / NFR-02 — 403 body MUST NOT echo ``task_id``.
        return _problem(
            status=403,
            type_="/errors/forbidden",
            title="Forbidden",
            detail="read scope required to list runs",
        )
    rows = repo_results.fetch_results_for_task(None, task_id)
    return JSONResponse(status_code=200, content={"items": rows})