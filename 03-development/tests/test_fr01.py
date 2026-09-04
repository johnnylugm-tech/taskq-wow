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


# ---------------------------------------------------------------------------
# Coverage-filling tests — exercise input-validation branches in
# api/tasks.py::_validate_create_payload that the AC-1.* tests above do not
# reach (non-dict body, name length cap, injection denylist, non-string
# command, malformed JSON, non-integer limit). These mirror the SPEC.md
# §3 FR-01 validation rule set; the assertion targets are stable
# (status code + problem+json content-type), never the human-readable
# detail string.
# ---------------------------------------------------------------------------


def test_fr01_create_rejects_non_dict_body(client):
    """POST /v1/tasks with a JSON array body must yield 422 + problem+json.

    SPEC.md §3 FR-01 — request must be a JSON object with fields name+command.
    Coverage: api/tasks.py::_validate_create_payload `not isinstance(payload, dict)`.
    """  # NFR-11
    response = client.post("/v1/tasks", json=["not", "a", "dict"])
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fr01_create_rejects_oversized_name(client):
    """POST /v1/tasks with name > 1000 chars must yield 422 + problem+json.

    SPEC.md §3 FR-01 — name length cap. Coverage: api/tasks.py::
    _validate_create_payload `len(name) > _MAX_NAME_LEN`.
    """  # NFR-10
    response = client.post(
        "/v1/tasks",
        json={"name": "x" * 1001, "command": "echo big"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fr01_create_rejects_injection_chars_in_name(client):
    """POST /v1/tasks with shell-metacharacter in name must yield 422.

    SPEC.md §3 FR-01 / SPEC.md §8 #16 — injection denylist. Coverage:
    api/tasks.py::_validate_create_payload `_INJECTION_CHARS.search(name)`.
    """  # NFR-10
    response = client.post(
        "/v1/tasks",
        json={"name": "bad;name", "command": "echo hi"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fr01_create_rejects_non_string_command(client):
    """POST /v1/tasks with a numeric command must yield 422 + problem+json.

    SPEC.md §3 FR-01 — command must be a non-empty string. Coverage:
    api/tasks.py::_validate_create_payload `command` type check.
    """  # NFR-11
    response = client.post(
        "/v1/tasks",
        json={"name": "ok-name", "command": 42},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fr01_create_rejects_malformed_json(client):
    """POST /v1/tasks with a body that is not parseable JSON must yield 422.

    SPEC.md §3 FR-01 — request body must be valid JSON. Coverage:
    api/tasks.py::create_task_endpoint `except Exception: return _problem(...)`.
    """  # NFR-09
    response = client.post(
        "/v1/tasks",
        content=b"{this is : not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fr01_list_rejects_non_integer_limit(client):
    """GET /v1/tasks?limit=abc must yield 422 (limit parse failure).

    SPEC.md §3 FR-01 — limit must be an integer between 1 and 200.
    Coverage: api/tasks.py::list_tasks_endpoint `except ValueError` branch.
    """  # NFR-11
    response = client.get("/v1/tasks", params={"limit": "abc"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


# ---------------------------------------------------------------------------
# Service-layer direct-call coverage — exercises the ValueError raise in
# list_tasks and the delete_task return path that the HTTP-level tests do
# not touch directly (the router converts the 404 case via a different
# code path; calling the service ensures the repository wiring is real).
# ---------------------------------------------------------------------------


def test_fr01_service_list_tasks_raises_for_out_of_range_limit():
    """service.list_tasks must raise ValueError when limit > 200.

    SPEC.md §3 FR-01 — limit cap; the router catches this before the call,
    but the service contract is enforced independently. Coverage:
    service/tasks.py::list_tasks limit guard.
    """  # NFR-09
    from taskq_api.service import tasks as svc
    import pytest as _pytest
    with _pytest.raises(ValueError):
        svc.list_tasks(limit=201)


def test_fr01_service_delete_task_returns_true_for_existing():
    """service.delete_task returns True when the row existed.

    Repository delete branch coverage (delete_task_row success path).
    """  # NFR-10
    from taskq_api.service import tasks as svc
    from taskq_api.repository import tasks as repo
    # Reset state, insert a known row, then delete via service.
    repo._reset_state()
    tid = repo.insert_task(None, name="to-delete-via-svc", command="echo x")
    assert svc.delete_task(tid) is True
    assert svc.delete_task(tid) is False  # second call: row gone


def test_fr01_repository_delete_task_row_returns_false_for_missing():
    """repository.delete_task_row returns False when the id is unknown.

    Direct repository exercise for the missing-id branch.
    """  # NFR-10
    from taskq_api.repository import tasks as repo
    repo._reset_state()
    assert repo.delete_task_row(None, "definitely-not-present") is False


def test_fr01_repository_fetch_tasks_page_filters_by_status():
    """repository.fetch_tasks_page honours the optional status filter.

    Exercises the status filter branch in fetch_tasks_page.
    """  # NFR-10 NFR-01
    from taskq_api.repository import tasks as repo
    repo._reset_state()
    repo.insert_task(None, name="status-pending-a", command="echo a")
    items, _cursor = repo.fetch_tasks_page(
        None, limit=200, cursor=None, status="pending"
    )
    assert all(t["status"] == "pending" for t in items)
    items_done, _ = repo.fetch_tasks_page(
        None, limit=200, cursor=None, status="done"
    )
    assert items_done == []
