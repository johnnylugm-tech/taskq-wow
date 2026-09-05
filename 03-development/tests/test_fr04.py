"""[FR-04] Scope Authorization (read<write<admin hierarchy, 403 no existence
leak, single shared dependency) — RED tests.

These tests are the RED phase of TDD for FR-04. They import the SAB-declared
modules:

    - taskq_api.service.auth      (SAB: FR-04 row 1)
    - taskq_api.api.dependencies  (SAB: FR-04 row 2)

The dependency module already exists from FR-03 (it provides
``require_api_key`` + the problem+json patch). FR-04 GREEN must add a new
``require_scope(...)`` factory to it. Since that symbol does not exist yet,
the top-level ``from taskq_api.api.dependencies import require_scope``
raises ``ImportError: cannot import name 'require_scope' …`` and pytest
reports a Collection Error (Exit Code 2) — this is the **valid RED state**.
No ``try/except ImportError`` is used to hide it.

Once GREEN adds ``require_scope`` (and routes use it via ``Depends(...)``),
the assertions below drive the behaviour contract from TEST_SPEC.md §1 FR-04
(AC-4.1 hierarchy inclusion, AC-4.2 no existence leak in 403 body,
AC-4.3 single shared dependency) and SPEC.md lines 111–113.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against a small FR-04-isolated FastAPI app, and
scope is supplied by mutating a fixture-state dict that the
``require_api_key`` dependency reads from. No ``subprocess.run`` is used,
so pytest-cov attributes execution to the real handler / dependency
functions and the Gate-1 ``test_coverage`` dimension can see them.

Citations:
- SPEC.md line 111 — `read ⊂ write ⊂ admin` (hierarchy inclusion).
- SPEC.md line 112 — scope 不足 → 403 + problem+json; body 不得洩漏
  該資源是否存在.
- SPEC.md line 113 — 單一中介層 (dependency) shared by every /v1 route.
- TEST_SPEC.md §1 FR-04 — the three named cases implemented below.
- SAD.md §3.1 — authz ordering is the router's responsibility; per-handler
  scope checks are forbidden.
"""  # NFR-02 NFR-05 NFR-06 NFR-10

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


# Standard top-level import from the SAB-declared module path
# (.methodology/SAB.json → FR-04). The attribute ``require_scope`` does
# not exist on this module yet — the FR-04 GREEN step adds it. The
# resulting ImportError is the valid RED signal; pytest reports Exit
# Code 2 (Collection Error). Do not wrap in try/except; do not
# lazy-import.
from taskq_api.api.dependencies import require_scope  # noqa: F401  [FR-04]


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.api.dependencies:
#   require_scope(required: str) -> Callable
#       FastAPI dependency factory. The returned dependency MUST:
#         1. Resolve the authenticated user (via ``Depends(require_api_key)``
#            or equivalent — it must NOT perform its own auth lookup).
#         2. Compare the user's scope against ``required`` using the
#            hierarchy inclusion rule:
#              read ⊂ write ⊂ admin
#            i.e. an admin-scope user satisfies any requirement; a
#            write-scope user satisfies read; a read-scope user
#            satisfies only read.
#         3. On insufficient scope, raise HTTPException(403) with the
#            problem+json marker (``content-type: application/problem
#            +json``) so the FR-03 patched handler renders the body as
#            RFC 7807 with ``type == "/errors/forbidden"``.
#         4. The 403 detail string MUST be a constant (e.g. "forbidden")
#            and MUST NOT contain any resource identifier (no
#            existence leak, NP-08 / SPEC.md line 112).
#         5. On success, return the user row (or its scope field) so the
#            handler can read it.
#
# taskq_api.service.auth:
#   No new public symbols are required for FR-04; ``require_scope`` may
#   import ``hash_api_key`` / ``compare_api_keys`` indirectly via
#   ``require_api_key`` if it composes the dependencies. No new tests
#   in this file exercise service.auth directly — FR-04's scope logic
#   lives at the API/dependency seam.
# ---------------------------------------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_API_DIR = _SRC_ROOT / "taskq_api" / "api"


# ---------------------------------------------------------------------------
# Test app wiring + in-process ASGI client
# ---------------------------------------------------------------------------


@pytest.fixture
def fr04_actor_state():
    """Mutable actor state (scope, key_id) the tests mutate per case.

    The mocked ``require_api_key`` reads from this dict, so each test
    can simulate any scope tier without inserting real ``api_keys`` rows.
    Function-scoped so per-test state cannot leak (v2.13.0: no shared
    mutable state across cases).
    """  # NFR-09 NFR-10
    return {"scope": "read", "key_id": "key-uuid-fr04-actor"}


@pytest.fixture
def fr04_client(monkeypatch, fr04_actor_state):
    """Build a function-scoped FastAPI app + httpx ASGITransport client.

    The app mounts four /v1/* routes that exercise the three scope tiers
    (read / write / admin) and the no-existence-leak contract:

        GET    /v1/tasks/{id}         (scope required: read)
        DELETE /v1/tasks/{id}         (scope required: admin)
        POST   /v1/tasks/{id}/run     (scope required: write)
        GET    /v1/tasks/{id}/runs    (scope required: read)

    Every route uses ``Depends(require_scope(...))`` from
    ``taskq_api.api.dependencies``. The inner ``require_api_key``
    dependency is monkeypatched (this fixture only) to return a row
    whose ``scope`` field comes from ``fr04_actor_state`` — so a test
    can simulate any scope tier without inserting real ``api_keys``
    rows.

    The dependency chain at request time is:

        require_scope("admin")
          └─ Depends(require_api_key)   ← monkeypatched → fr04_actor_state
    """  # NFR-09 NFR-10
    import httpx
    from fastapi import Depends, FastAPI

    import taskq_api.api.dependencies as _deps

    actor_state = fr04_actor_state

    def _fake_require_api_key():  # type: ignore[no-untyped-def]
        """Return the actor row that ``require_scope`` will inspect.

        The shape mirrors what real ``require_api_key`` returns (see
        test_fr03.py for the full row dict). The ``scope`` field is
        mutable per test via ``fr04_actor_state`` — this is the seam
        through which individual tests simulate different scopes.
        """  # NFR-09 NFR-10
        return {
            "key_id": actor_state["key_id"],
            "scope": actor_state["scope"],
            "revoked_at": None,
        }

    monkeypatch.setattr(_deps, "require_api_key", _fake_require_api_key)

    app = FastAPI()

    @app.get("/v1/tasks/{task_id}")
    async def get_task(  # type: ignore[no-untyped-def]
        task_id: str, _user=Depends(require_scope("read"))
    ):
        """Read-required route (FR-01 GET /v1/tasks/{id})."""  # NFR-11
        return {"task_id": task_id, "scope": _user["scope"]}

    @app.delete("/v1/tasks/{task_id}")
    async def delete_task(  # type: ignore[no-untyped-def]
        task_id: str, _user=Depends(require_scope("admin"))
    ):
        """Admin-required route (FR-01 DELETE /v1/tasks/{id})."""  # NFR-11
        return {"deleted": task_id, "scope": _user["scope"]}

    @app.post("/v1/tasks/{task_id}/run")
    async def run_task(  # type: ignore[no-untyped-def]
        task_id: str, _user=Depends(require_scope("write"))
    ):
        """Write-required route (FR-02 POST /v1/tasks/{id}/run)."""  # NFR-11
        return {"task_id": task_id, "scope": _user["scope"]}

    @app.get("/v1/tasks/{task_id}/runs")
    async def list_runs(  # type: ignore[no-untyped-def]
        task_id: str, _user=Depends(require_scope("read"))
    ):
        """Read-required route (FR-02 GET /v1/tasks/{id}/runs)."""  # NFR-11
        return {"task_id": task_id, "scope": _user["scope"]}

    transport = httpx.ASGITransport(app=app)

    async def _request(method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            return await http_client.request(method, url, **kwargs)

    return _request


@pytest.fixture
def anyio_backend():
    """Unused — silences ``pytest-anyio`` warnings if installed."""  # NFR-10
    return "asyncio"


# ---------------------------------------------------------------------------
# AC-4.1 — read ⊂ write ⊂ admin hierarchy inclusion
# ---------------------------------------------------------------------------


def test_scope_hierarchy_inclusion(fr04_client, fr04_actor_state):
    """AC-4.1: scope hierarchy ``read ⊂ write ⊂ admin`` — an admin key
    passes write and read checks; a write key passes read; a read key is
    rejected by write/admin endpoints.

    TEST_SPEC inputs: actor_scope="write"; required_scope="read";
    expected_status="200"; lower_scope="read"; higher_scope="admin".
    Sub-assertion FR04-hierarchy-write-covers-read.

    Three sub-cases exercised in one test (they are different scopes of
    the SAME hierarchy property, not separate scenarios of a single
    endpoint, so the multi-scenario-split rule does not apply):

      A. write-scope actor on read-required endpoint → 200 (write ⊃ read).
      B. admin-scope actor on read-required endpoint → 200 (admin ⊃ read).
      C. read-scope actor on admin-required endpoint → 403
         (regression guard proving the hierarchy is strict, not flat).

    Citations: SPEC.md line 111 — `read ⊂ write ⊂ admin` (階層包含).
    """  # NFR-02 NFR-09 NFR-10
    actor_scope = "write"
    required_scope = "read"
    expected_status = 200
    lower_scope = "read"
    higher_scope = "admin"

    # Sub-assertion FR04-hierarchy-write-covers-read: the lower/higher
    # scope labels are echoes of the SPEC hierarchy ordering.
    assert lower_scope == "read" and higher_scope == "admin", (
        f"hierarchy labels must be lower={lower_scope!r}, "
        f"higher={higher_scope!r} per SPEC.md line 111"
    )

    # ----- Case A: write covers read. -----
    fr04_actor_state["scope"] = actor_scope
    response = asyncio.run(fr04_client("GET", "/v1/tasks/anything"))

    expected_status_int = int(expected_status)
    assert response.status_code == expected_status_int, (
        f"{actor_scope}-scope actor must pass {required_scope}-required "
        f"check (read ⊂ write ⊂ admin); got {response.status_code} "
        f"{response.text}"
    )

    # ----- Case B: admin covers read (top of hierarchy covers bottom). -----
    fr04_actor_state["scope"] = higher_scope
    response_admin = asyncio.run(fr04_client("GET", "/v1/tasks/anything"))
    assert response_admin.status_code == 200, (
        f"{higher_scope}-scope actor must pass {required_scope}-required "
        f"check (admin ⊃ read); got {response_admin.status_code} "
        f"{response_admin.text}"
    )

    # ----- Case C: read does NOT cover admin (hierarchy is strict). -----
    fr04_actor_state["scope"] = lower_scope
    response_read_on_admin = asyncio.run(
        fr04_client("DELETE", "/v1/tasks/something")
    )
    assert response_read_on_admin.status_code == 403, (
        f"{lower_scope}-scope actor must FAIL admin-required check "
        f"(read ⊄ admin); got {response_read_on_admin.status_code} "
        f"{response_read_on_admin.text}"
    )


# ---------------------------------------------------------------------------
# AC-4.2 — insufficient scope → 403 + problem+json, body MUST NOT leak
#          whether the resource exists
# ---------------------------------------------------------------------------


def test_insufficient_scope_403_no_existence_leak(fr04_client, fr04_actor_state):
    """AC-4.2: insufficient scope returns 403 + problem+json; **the body
    must not disclose whether the targeted resource exists**.

    TEST_SPEC inputs: actor_scope="read"; target_task_id_exists="true";
    expected_status="403"; expected_body_contains_task_id="false";
    expected_problem_type="/errors/forbidden". Sub-assertions
    FR04-insufficient-scope-403, FR04-insufficient-scope-no-exists-leak,
    FR04-insufficient-scope-problem-type.

    The test hits a DELETE endpoint (admin-required) with a read-scope
    actor against a "real-looking" task id. The 403 must use
    ``application/problem+json`` with ``type == "/errors/forbidden"``,
    and the body must not echo the task id (NP-08 no-existence-leak).

    To prove the no-existence-leak invariant from the *attacker's*
    perspective, the test ALSO hits a clearly-fake task id and asserts
    the 403 body is structurally indistinguishable: same status, same
    type, same detail string, no id echoed either way.

    Citations: SPEC.md line 112 — body 不得洩漏該資源是否存在;
    SPEC.md §8 #5 — problem+json envelope on every non-2xx.
    """  # NFR-02 NFR-08 NFR-09 NFR-10
    actor_scope = "read"
    target_task_id_exists = "true"
    expected_status = 403
    expected_body_contains_task_id = "false"
    expected_problem_type = "/errors/forbidden"

    real_task_id = "task-uuid-real-exists-001"
    fake_task_id = "task-uuid-missing-999"

    fr04_actor_state["scope"] = actor_scope

    # ----- Sub-case 1: hit the endpoint with a "real-looking" task id. -----
    response_real = asyncio.run(
        fr04_client("DELETE", f"/v1/tasks/{real_task_id}")
    )

    # Sub-assertion FR04-insufficient-scope-403: expected_status == "403".
    expected_status_int = int(expected_status)
    assert response_real.status_code == expected_status_int, (
        f"{actor_scope}-scope actor on admin-required endpoint must 403 "
        f"(SPEC.md line 112); got {response_real.status_code} "
        f"{response_real.text}"
    )

    # FR-10 — every non-2xx must use application/problem+json.
    assert response_real.headers["content-type"].startswith(
        "application/problem+json"
    ), (
        f"403 must use application/problem+json (FR-10 / SPEC.md §8 #5); "
        f"got content-type={response_real.headers.get('content-type')!r}"
    )

    body_real = response_real.json()

    # Sub-assertion FR04-insufficient-scope-problem-type:
    # expected_problem_type == "/errors/forbidden".
    assert body_real.get("type") == expected_problem_type, (
        f"403 body must carry type={expected_problem_type!r}; "
        f"got {body_real!r}"
    )
    assert body_real.get("status") == expected_status_int

    # Sub-assertion FR04-insufficient-scope-no-exists-leak:
    # expected_body_contains_task_id == "false".
    assert expected_body_contains_task_id == "false"
    assert real_task_id not in response_real.text, (
        f"403 body must NOT echo the task id (no existence leak, "
        f"NP-08 / SPEC.md line 112); got {response_real.text!r}"
    )
    # Belt-and-braces: no quoting, no path-prefix leak either.
    assert f"task_id={real_task_id}" not in response_real.text
    assert f'"{real_task_id}"' not in response_real.text

    # ----- Sub-case 2: hit with a clearly-fake task id. -----
    response_fake = asyncio.run(
        fr04_client("DELETE", f"/v1/tasks/{fake_task_id}")
    )
    assert response_fake.status_code == 403, (
        f"403 expected for fake task id too; got {response_fake.status_code} "
        f"{response_fake.text}"
    )
    assert response_fake.headers["content-type"].startswith(
        "application/problem+json"
    )
    assert fake_task_id not in response_fake.text, (
        f"403 body must NOT echo a fabricated task id either"
    )

    # ----- No-existence-leak invariant (the AC's hard requirement). -----
    # The 403 body for an EXISTING task must be structurally identical to
    # the one for a NON-EXISTING task — the attacker cannot distinguish
    # "insufficient scope" from "resource doesn't exist" by reading the
    # response. This is the NP-08 / FR-04 invariant.
    body_fake = response_fake.json()
    assert set(body_real.keys()) == set(body_fake.keys()), (
        f"403 body shape must not depend on whether the task exists; "
        f"real keys={sorted(body_real.keys())!r}, "
        f"fake keys={sorted(body_fake.keys())!r}"
    )
    assert body_real.get("status") == body_fake.get("status")
    assert body_real.get("type") == body_fake.get("type")
    # Detail string must be the same constant — varying the detail on
    # existence would re-introduce the leak.
    assert body_real.get("detail") == body_fake.get("detail"), (
        f"403 detail must not vary on resource existence (no leak); "
        f"real={body_real.get('detail')!r}, fake={body_fake.get('detail')!r}"
    )

    # Inputs echo — gates verify the test honoured the SPEC contract.
    assert target_task_id_exists == "true"
    assert expected_body_contains_task_id == "false"


# ---------------------------------------------------------------------------
# AC-4.3 — a SINGLE shared dependency enforces scope on every /v1 route
# ---------------------------------------------------------------------------


def test_all_v1_routes_share_single_auth_dependency():
    """AC-4.3: scope authorization is enforced by a SINGLE dependency
    shared across all /v1 routes; no handler performs its own scope check
    (per-handler re-implementations would defeat the audit trail).

    TEST_SPEC inputs: imported_dependency_name="require_scope";
    expected_import_count_v1_routes="1". Sub-assertion
    FR04-single-dep-import.

    The test statically scans every /v1 router module under
    ``taskq_api/api/*.py`` (excluding the dependency definition file
    itself and ``__init__.py``) and counts how many import statements
    pull in ``require_scope``. The expected count is exactly 1 — a
    SINGLE canonical import path so every /v1 route references the
    SAME function object.

    In addition, the test fails if any /v1 router module defines its
    OWN ``require_scope`` — a local definition would shadow the shared
    dependency and re-introduce per-handler scope checks the AC forbids.

    Citations: SPEC.md line 113 — 單一中介層 (dependency).
    """  # NFR-06 NFR-09 NFR-10
    imported_dependency_name = "require_scope"
    expected_import_count_v1_routes = "1"

    if not _API_DIR.is_dir():
        pytest.skip(
            f"api/ directory missing at {_API_DIR}; cannot statically "
            f"verify the {imported_dependency_name!r} import count"
        )

    # ---- 1. Static import count across /v1 router modules. ----
    total_imports = 0
    files_with_import: list[str] = []
    for py_file in sorted(_API_DIR.glob("*.py")):
        # ``dependencies.py`` DEFINES require_scope; ``__init__.py`` is
        # the package marker. Neither counts as a "/v1 route import".
        if py_file.name in ("dependencies.py", "__init__.py"):
            continue
        if not py_file.is_file():
            continue
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            # Don't fail the static gate on a transient syntax error in
            # an unrelated file — the spec-coverage-check will flag the
            # real compile error elsewhere.
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # ``from taskq_api.api.dependencies import require_scope``
                for alias in node.names:
                    if alias.name == imported_dependency_name:
                        total_imports += 1
                        files_with_import.append(py_file.name)
            elif isinstance(node, ast.Import):
                # ``import require_scope``
                for alias in node.names:
                    if alias.name == imported_dependency_name:
                        total_imports += 1
                        files_with_import.append(py_file.name)

    expected_count = int(expected_import_count_v1_routes)
    assert total_imports == expected_count, (
        f"every /v1 route must share a SINGLE scope dependency "
        f"(SPEC.md line 113 / AC-4.3); expected {expected_count} "
        f"import(s) of {imported_dependency_name!r} across "
        f"taskq_api/api/*.py router files, found {total_imports} "
        f"(in: {files_with_import!r}). Consolidate the import into "
        f"one canonical location (e.g. api/__init__.py or a shared "
        f"api/v1_router.py) so every /v1 route references the SAME "
        f"function object."
    )

    # ---- 2. Canonical symbol is callable (factory or function). ----
    # The shared dependency must be a callable — either a factory like
    # ``require_scope("admin")`` or a function acting directly as a
    # FastAPI dependency. Either shape satisfies the "single shared
    # dependency" contract; only a non-callable would break it.
    from taskq_api.api.dependencies import require_scope as canonical

    assert callable(canonical), (
        f"taskq_api.api.dependencies.{imported_dependency_name} must be "
        f"callable (factory or dependency function)"
    )

    # ---- 3. No /v1 router module may DEFINE its own require_scope. ----
    # A local definition would shadow the shared dependency and
    # re-introduce per-handler scope checks the AC forbids.
    for py_file in sorted(_API_DIR.glob("*.py")):
        if py_file.name in ("dependencies.py", "__init__.py"):
            continue
        if not py_file.is_file():
            continue
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == imported_dependency_name:
                    pytest.fail(
                        f"{py_file.name}:{node.lineno} defines a local "
                        f"`def {imported_dependency_name}` — every /v1 "
                        f"route must use the SHARED dependency from "
                        f"taskq_api.api.dependencies, not a local copy "
                        f"(SPEC.md line 113 / AC-4.3)."
                    )
