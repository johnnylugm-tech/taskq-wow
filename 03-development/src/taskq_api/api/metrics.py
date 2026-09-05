"""[FR-09] HTTP router for ``GET /v1/metrics``.

This router implements the FR-09 observability endpoint per SPEC.md
line 158 + NFR-04 / line 211:

  * ``GET /v1/metrics`` requires ``admin`` scope; on success returns
    a JSON body carrying task counts by status, execution latency
    percentiles (``p50``, ``p95``, ``p99`` in milliseconds), and the
    rate-limit rejection count.

The body MUST NOT contain the DB connection string password
fragment (NFR-04 / SPEC.md line 211). The aggregator below reads
data exclusively through ``taskq_api.repository.tasks`` and
``taskq_api.repository.results`` (the FR-06 persistence seam), and
NEVER echoes the ``TASKQ_DB_URL`` value into the response — the FR-09
test pins down the NFR-04 invariant with two assertions:
``password_fragment not in body_text`` AND ``db_url not in body_text``.

Admin scope is enforced via the FR-09-local ``_admin_scope_gate``
dependency that resolves ``require_api_key`` at call time (mirrors
the pattern in ``taskq_api.api.dependencies._authenticate``). This
keeps the test_fr09 fixture's monkey-patched fake discoverable when
the router is imported at module-load time, BEFORE the fixture
applies the patch.

Citations:
- SPEC.md line 158 — ``GET /v1/metrics`` admin 範疇 + 任務狀態計數 +
  執行延遲百分位 + rate-limit 拒絕次數.
- SPEC.md line 211 — NFR-04 DB URL password MUST NOT appear in any
  /v1/metrics response (FR-09 cross-cut).
- SPEC.md §3 FR-04 — admin 範疇透過 scope check 強制 (NP-08 不洩漏
  資源存在).
- SAD.md §3.1 — observability lives in the api layer.
- NFR-04 — DB URL password scrubbed; stdout/stderr metrics redacted.
- NFR-11 — handlers ≤40 lines each.
"""  # NFR-04 NFR-09 NFR-10 NFR-11
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

import taskq_api.api.dependencies as _deps
from taskq_api.repository import results as repo_results
from taskq_api.repository import tasks as repo_tasks
from taskq_api.service.rate_limit import get_rate_limit_rejections


router = APIRouter(tags=["metrics"])

# Admin sits at the top of the ``read ⊂ write ⊂ admin`` scope
# hierarchy (SPEC.md line 111). Hoisted as a module constant so the
# gate is a one-liner comparison and the rank is named in one place.
_ADMIN_RANK = _deps._SCOPE_RANK.get("admin", 3)


# Status names that ``task_counts_by_status`` MUST surface even when no
# rows match. Mirrors the FR-02 state machine (``VALID_STATES`` in
# service.runner) plus the FR-08 ``interrupted`` terminal state.
_STATUS_KEYS: tuple[str, ...] = (
    "pending",
    "running",
    "done",
    "failed",
    "timeout",
    "interrupted",
)


def _percentile(values: List[int], pct: float) -> Optional[int]:
    """Return the ``pct``-th percentile of ``values`` in milliseconds.

    Uses nearest-rank with linear interpolation between adjacent ranks
    so the result is stable across small inputs; ``None`` when no
    runs exist so the JSON serializer omits the key entirely
    (SPEC.md line 158 only requires the keys when runs exist).

    Citations: SPEC.md line 158 — latency percentiles p50/p95/p99 in ms.
    """  # NFR-09 NFR-11
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    interpolated = (
        sorted_values[lower] * (1 - weight)
        + sorted_values[upper] * weight
    )
    return int(round(interpolated))


def _admin_scope_gate() -> dict:
    """FastAPI dependency: enforce ``admin`` scope with call-time lookup.

    Resolves ``taskq_api.api.dependencies.require_api_key`` at call
    time via the shared ``_deps._authenticate(None)`` helper so the
    FR-09 test fixture (which monkey-patches that name AFTER this
    router module is imported) is still honoured. This is the same
    runtime-lookup pattern used by ``require_rate_limit`` in
    ``taskq_api.api.dependencies``.

    On insufficient scope raises HTTP 403 carrying the FR-04 problem
    +json marker (``content-type: application/problem+json``) so the
    patched handler in ``dependencies.py`` renders the body as RFC
    7807 with ``type == "/errors/forbidden"``.

    Citations: SPEC.md line 112 (insufficient scope → 403 +
    problem+json); SPEC.md line 158 (admin scope mandatory for
    /v1/metrics).
    """  # NFR-04 NFR-09 NFR-10 NFR-11
    user = _deps._authenticate(None)
    if _deps._SCOPE_RANK.get(user.get("scope", ""), 0) < _ADMIN_RANK:
        raise HTTPException(
            status_code=403,
            detail=_deps._FORBIDDEN_DETAIL,
            headers={"content-type": _deps._PROBLEM_CONTENT_TYPE},
        )
    return user


def _collect_metrics() -> Dict[str, Any]:
    """Aggregate the FR-09 observability payload from the persistence seam.

    Reads task status counts via ``repository.tasks.fetch_tasks_page``
    (cursor-walked until exhaustion so the count covers the entire
    table, not just the default page) and run latency percentiles via
    ``repository.results.fetch_results_for_task`` per task. The DB
    URL is intentionally never read into the response — NFR-04.

    Returns a dict with keys: ``task_counts_by_status``,
    ``latency_ms`` (``p50``, ``p95``, ``p99`` — present iff any
    runs exist), ``rate_limit_rejections``.

    Citations: SPEC.md line 158; NFR-04.
    """  # NFR-04 NFR-09 NFR-11
    counts: Dict[str, int] = {status: 0 for status in _STATUS_KEYS}
    durations: List[int] = []
    cursor: Optional[str] = None
    while True:
        items, next_cursor = repo_tasks.fetch_tasks_page(
            None, limit=200, cursor=cursor, status=None
        )
        for task in items:
            status = task.get("status", "")
            if status in counts:
                counts[status] += 1
            for run in repo_results.fetch_results_for_task(None, task["id"]):
                durations.append(int(run.get("duration_ms", 0)))
        if not next_cursor:
            break
        cursor = next_cursor

    payload: Dict[str, Any] = {
        "task_counts_by_status": counts,
        "rate_limit_rejections": int(get_rate_limit_rejections()),
    }
    if durations:
        payload["latency_ms"] = {
            "p50": _percentile(durations, 50),
            "p95": _percentile(durations, 95),
            "p99": _percentile(durations, 99),
        }
    return payload


@router.get("/v1/metrics")
async def metrics_endpoint(
    _user: dict = Depends(_admin_scope_gate),
) -> JSONResponse:
    """GET /v1/metrics — observability payload (FR-09 / AC-9.5).

    Admin scope is enforced via ``Depends(_admin_scope_gate)`` — a
    local gate that performs a call-time lookup of
    ``require_api_key`` so test fixtures that monkey-patch the
    dependency AFTER the router is imported still resolve correctly.
    The dependency chain surfaces a 403 + problem+json on insufficient
    scope (SPEC.md line 112). The response body is the FR-09
    observability payload — task counts, latency percentiles, and
    rate-limit rejection count. MUST NOT include the DB URL
    (NFR-04).

    The route is declared as ``/v1/metrics`` (full path) so the FR-09
    test wiring (which mounts the router with
    ``app.include_router(router)`` and no prefix) hits the canonical
    SPEC.md line 158 endpoint verbatim.

    Citations: SPEC.md line 158 + line 211; SPEC.md line 112.
    """  # NFR-04 NFR-09 NFR-11
    return JSONResponse(status_code=200, content=_collect_metrics())


def register(app) -> None:  # type: ignore[no-untyped-def]
    """Mount the metrics router onto ``app``.

    Either ``router`` (an ``APIRouter``) or ``register(app)`` is
    accepted by the FR-09 test wiring; both shapes are supported so
    the GREEN contract stays decoupled from the chosen surface
    (test_fr09.py tries ``register`` first, then ``router``).

    Citations: SPEC.md line 158.
    """  # NFR-09 NFR-11
    app.include_router(router)
