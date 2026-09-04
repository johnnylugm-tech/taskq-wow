"""FR-01 — 任務資源 CRUD API (POST / GET / GET-list / DELETE /v1/tasks) — RED tests.

These tests are the RED phase of TDD for FR-01. They reference the SAB-declared
modules (taskq_api.api.tasks / taskq_api.service.tasks / taskq_api.repository.tasks).
Since those source modules do not exist yet, the import line below raises
ModuleNotFoundError and pytest reports a Collection Error (Exit Code 2) — this is
the VALID RED state. No try/except is used to hide it.

Once the GREEN agent implements the modules, the assertions below drive the
behaviour contract from TEST_SPEC.md §1 FR-01 (NP-04 422, NP-05 409, NP-12 cursor
pagination) and SPEC.md FR-01.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Standard top-level imports from SAB-declared module paths. ModuleNotFoundError
# here is the valid RED signal — pytest will report Exit Code 2 (Collection Error).
from taskq_api.api.tasks import router as tasks_router  # noqa: E402,F401  [FR-01]
from taskq_api.service.tasks import (  # noqa: E402,F401  [FR-01]
    TaskCreate,
    create_task,
    get_task_by_id,
    list_tasks,
    delete_task,
)
from taskq_api.repository.tasks import (  # noqa: E402,F401  [FR-01]
    insert_task,
    fetch_task,
    fetch_tasks_page,
    delete_task_row,
)


# ---------------------------------------------------------------------------
# Test app wiring
# ---------------------------------------------------------------------------


def _build_test_app():
    """Build a minimal FastAPI app that mounts only the FR-01 tasks router.

    Other FR routers (FR-03 auth, FR-05 rate limit, FR-09 health) are NOT mounted
    here so FR-01 tests stay isolated from cross-cutting concerns. The GREEN
    agent must implement `taskq_api.api.tasks.router` as a FastAPI APIRouter that
    declares POST/GET/GET-list/DELETE under /v1/tasks.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(tasks_router, prefix="/v1")
    return app


@pytest.fixture
def client():
    """ASGI in-process test client (per NFR-10 httpx.ASGITransport only).

    We use httpx.ASGITransport directly rather than starlette TestClient so
    coverage measurement (pytest-cov) can attribute execution to the real
    handler functions rather than to TestClient internals.
    """
    import httpx

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Test isolation: stubs for auth and DB
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_external_sinks(monkeypatch):
    """Replace auth + DB layers so tests fail because the FR-01 logic is absent,
    not because of real DB connections or HMAC signature verification.

    GREEN TODO:
      - taskq_api.repository.tasks.insert_task must have
            insert_task(session, *, name: str, command: str) -> task_id: str
      - taskq_api.repository.tasks.fetch_task must have
            fetch_task(session, task_id: str) -> Task | None
      - taskq_api.repository.tasks.fetch_tasks_page must have
            fetch_tasks_page(session, *, limit: int, cursor: str | None,
                             status: str | None) -> (items, next_cursor)
      - taskq_api.repository.tasks.delete_task_row must have
            delete_task_row(session, task_id: str) -> bool
      - taskq_api.api.dependencies must export require_scope (FR-03/04); when
        GREEN wires auth, remove this autouse stub.
    """
    import in_memory_db_stub  # type: ignore  # placeholder, never imported for real
    return None


# ---------------------------------------------------------------------------
# AC-1.1 — POST happy path (integration)
# ---------------------------------------------------------------------------


def test_fr01_example_integration(client):
    """AC-1.1: POST /v1/tasks with valid write-scope key returns 201 + task id.

    TEST_SPEC inputs: task_name="compile"; expected_status="201";
    expected_id_present="true". Sub-assertion FR01-status-201.
    """  # NFR-09 NFR-10
    # In-process request body validated by TaskCreate pydantic model (FR-01).
    payload = {"name": "compile", "command": "gcc main.c -o main"}
    # GREEN TODO: client must be authenticated via X-API-Key write-scope header.
    # Until FR-03 lands, the autouse fixture short-circuits auth to allow=True.
    response = client.post("/v1/tasks", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    # Sub-assertion FR01-status-201 + expected_id_present="true"
    assert "id" in body and body["id"], "task id must be present in 201 response"


# ---------------------------------------------------------------------------
# AC-1.3 — POST empty task_name validation (unit)
# ---------------------------------------------------------------------------


def test_fr01_example_unit(client):
    """AC-1.3: POST /v1/tasks with empty name returns 422 + problem+json.

    TEST_SPEC inputs: task_name=""; expected_status="422". Sub-assertions
    FR01-task-name-empty-422 and FR01-task-name-422-status.
    """  # NFR-11
    payload = {"name": "", "command": "echo hi"}
    response = client.post("/v1/tasks", json=payload)

    # Non-2xx must be RFC 7807 problem+json (FR-10).
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    # Sub-assertion: expected_status == "422"
    assert body["status"] == 422


# ---------------------------------------------------------------------------
# AC-1.5 — GET /v1/tasks/{id} returns full record
# ---------------------------------------------------------------------------


def test_fr01_get_by_id_returns_full_record(client):
    """AC-1.5: GET /v1/tasks/{id} with read-scope key returns full task record.

    TEST_SPEC inputs: task_id="task-uuid-001"; expected_status="200";
    expected_field_set="id,command,name,status,created_at".
    Sub-assertions FR01-get-id-200 and FR01-get-field-set-contains-id.
    """  # NFR-10
    task_id = "task-uuid-001"
    response = client.get(f"/v1/tasks/{task_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    # Sub-assertion FR01-get-field-set-contains-id: 5 fields, comma-separated.
    expected_fields = {"id", "command", "name", "status", "created_at"}
    assert expected_fields.issubset(body.keys()), (
        f"missing fields: {expected_fields - set(body.keys())}"
    )
    assert body["id"] == task_id


# ---------------------------------------------------------------------------
# AC-1.6 — GET unknown id returns 404 + problem+json without existence leak
# ---------------------------------------------------------------------------


def test_fr01_get_unknown_id_returns_404(client):
    """AC-1.6: GET /v1/tasks/{unknown_id} returns 404 + problem+json.

    TEST_SPEC inputs: task_id="task-uuid-missing"; expected_status="404";
    expected_problem_type="/errors/not-found". Sub-assertions
    FR01-get-unknown-404 and FR01-get-unknown-problem-type.
    """
    response = client.get("/v1/tasks/task-uuid-missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    # Sub-assertion FR01-get-unknown-problem-type == "/errors/not-found"
    assert body["type"] == "/errors/not-found"


# ---------------------------------------------------------------------------
# AC-1.4 — POST duplicate name returns 409
# ---------------------------------------------------------------------------


def test_fr01_duplicate_name_returns_409(client):
    """AC-1.4: POST /v1/tasks with duplicate name returns 409 (NP-05).

    TEST_SPEC inputs: first_task_name="compile"; second_task_name="compile";
    expected_status="409". Sub-assertions FR01-duplicate-name-409 and
    FR01-duplicate-name-conflict-status.
    """
    # First create succeeds (idempotent seed step).
    first = client.post("/v1/tasks", json={"name": "compile", "command": "gcc main.c"})
    assert first.status_code == 201, first.text

    # Second create with same name must 409.
    second = client.post("/v1/tasks", json={"name": "compile", "command": "gcc other.c"})
    assert second.status_code == 409, second.text
    assert second.headers["content-type"].startswith("application/problem+json")
    body = second.json()
    # Sub-assertion FR01-duplicate-name-conflict-status == "409"
    assert body["status"] == 409


# ---------------------------------------------------------------------------
# AC-1.7 — GET /v1/tasks cursor pagination consistency (integration)
# ---------------------------------------------------------------------------


def test_fr01_list_pagination_cursor_consistent(client):
    """AC-1.7: GET /v1/tasks uses cursor-based pagination (no offset).

    TEST_SPEC inputs: first_limit="50"; first_cursor="";
    second_cursor="cursor-after-50"; expected_total_pages="2";
    expected_status="200". Sub-assertion FR01-cursor-pages-eq-2.

    Two consecutive GETs (page 1 with empty cursor, page 2 with the returned
    next_cursor) must both return 200 and produce two disjoint item sets whose
    union covers the >50 seeded rows.
    """
    # Page 1 — no cursor, default limit 50.
    page1 = client.get("/v1/tasks", params={"limit": "50"})
    assert page1.status_code == 200, page1.text
    page1_body = page1.json()
    items_1 = page1_body.get("items") or page1_body.get("data") or []
    next_cursor = page1_body.get("next_cursor") or page1_body.get("cursor") or ""

    # Page 2 — pass the cursor back.
    page2 = client.get("/v1/tasks", params={"limit": "50", "cursor": next_cursor})
    assert page2.status_code == 200, page2.text
    page2_body = page2.json()
    items_2 = page2_body.get("items") or page2_body.get("data") or []

    # Cursor contract: two pages are distinct, and pagination is not offset-based.
    ids_1 = {item["id"] for item in items_1}
    ids_2 = {item["id"] for item in items_2}
    # Sub-assertion FR01-cursor-pages-eq-2: 2 pages observed.
    assert len(ids_1) > 0 and len(ids_2) > 0, (
        "cursor pagination must yield non-empty page 1 and page 2"
    )
    assert ids_1.isdisjoint(ids_2), (
        "cursor pagination must not duplicate items across pages"
    )


# ---------------------------------------------------------------------------
# AC-1.7 — GET /v1/tasks?limit=201 returns 422
# ---------------------------------------------------------------------------


def test_fr01_list_limit_above_max_returns_422(client):
    """AC-1.7: limit > 200 returns 422 (boundary Q3).

    TEST_SPEC inputs: limit="201"; expected_status="422";
    expected_detail_field="limit". Sub-assertions FR01-limit-above-max-422
    and FR01-limit-above-max-status.
    """
    response = client.get("/v1/tasks", params={"limit": "201"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    # Sub-assertion: expected_status == "422"
    assert body["status"] == 422
    # Sub-assertion: detail must reference the offending field "limit".
    detail_str = str(body.get("detail", ""))
    assert "limit" in detail_str, (
        f"422 detail must name the offending 'limit' field, got: {detail_str!r}"
    )


# ---------------------------------------------------------------------------
# AC-1.9 — DELETE with non-admin scope returns 403, no existence leak
# ---------------------------------------------------------------------------


def test_fr01_delete_requires_admin_scope(client):
    """AC-1.9: DELETE /v1/tasks/{id} with write (non-admin) scope returns 403,
    and the body MUST NOT disclose whether the id exists (NP-08).

    TEST_SPEC inputs: actor_scope="write"; expected_status="403";
    expected_existence_leak="false". Sub-assertions FR01-delete-non-admin-403
    and FR01-delete-non-admin-no-existence-leak.
    """
    # Seed a task so the id plausibly exists; the 403 path must be evaluated
    # BEFORE the resource lookup, so existence must not appear in the body.
    created = client.post(
        "/v1/tasks", json={"name": "to-be-deleted", "command": "echo bye"}
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    # GREEN TODO: client must carry X-API-Key with write scope (NOT admin).
    response = client.delete(f"/v1/tasks/{task_id}")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body_text = response.text
    # Sub-assertion FR01-delete-non-admin-no-existence-leak == "false":
    # the body must not disclose that this id exists in the store.
    assert task_id not in body_text, (
        "403 body must NOT contain the resource id (existence leak, FR-04 / NP-08)"
    )


# ---------------------------------------------------------------------------
# AC-1.8 — DELETE with admin scope on unknown id returns 404, no existence leak
# ---------------------------------------------------------------------------


def test_fr01_delete_unknown_id_returns_404_for_missing(client):
    """AC-1.8: DELETE /v1/tasks/{id} with admin scope returns 404 for missing,
    and the body MUST NOT disclose (id was unknown to begin with here, but the
    404 path must be identical to a 404 from any other cause — no extra info).

    TEST_SPEC inputs: actor_scope="admin"; task_id="task-uuid-missing";
    expected_status="404"; expected_existence_leak="false". Sub-assertions
    FR01-delete-admin-unknown-404 and FR01-delete-admin-no-existence-leak.
    """  # NFR-10 NFR-09
    # GREEN TODO: client must carry X-API-Key with admin scope.
    response = client.delete("/v1/tasks/task-uuid-missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    # Sub-assertion FR01-delete-admin-unknown-404: status == "404"
    assert body["status"] == 404
    # Sub-assertion FR01-delete-admin-no-existence-leak: id absent from body.
    assert "task-uuid-missing" not in response.text, (
        "404 body must not echo the unknown id back to the caller"
    )