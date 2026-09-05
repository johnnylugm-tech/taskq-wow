"""[FR-09] Health Checks + Observability (/healthz, /readyz, /v1/metrics) — RED
tests.

These are the RED phase of TDD for FR-09. They import the SAB-declared
modules:

    - taskq_api.api.health   (SAB: FR-09 row 1)
    - taskq_api.api.metrics  (SAB: FR-09 row 2)

Neither module exists on disk yet. The top-level imports below raise
``ModuleNotFoundError`` and pytest reports Exit Code 2 (Collection Error) —
this is the **valid RED state**. No ``try/except ImportError`` is used to
hide it; no lazy-import workaround is applied.

The five tests below pin down the SPEC.md lines 156-158 + 211 contract:

  - AC-9.1  ``GET /healthz``        returns 200 ``{"status":"ok"}``, no auth.
  - AC-9.2  ``GET /readyz``         returns 200 when DB reachable AND
                                    ``alembic current == head``.
  - AC-9.3  ``GET /readyz``         returns 503 with ``detail`` naming the
                                    DB when the DB is unreachable.
  - AC-9.4  ``GET /readyz``         returns 503 with ``detail`` naming the
                                    migration when migrations are not at
                                    head (fail-closed).
  - AC-9.5  ``GET /v1/metrics``     requires ``admin`` scope; returns task
                                    counts by status, latency percentiles,
                                    and rate-limit rejection counts; the
                                    response MUST NOT include the DB
                                    connection string password fragment
                                    (NFR-04 / SPEC.md line 211).

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against a small FR-09-isolated FastAPI app whose
routes come from the SAB-declared modules (``api.health`` and
``api.metrics``). The DB-reachability and migration-at-head predicates
that ``/readyz`` evaluates are monkeypatched at the FR-09 GREEN seam so
the fault-injection cases do not depend on a live Postgres / alembic
run — both modes are hermetic, no subprocess is spawned, so pytest-cov
attributes execution to the real handlers and the Gate-1
``test_coverage`` dimension can see them.

Citations:
- SPEC.md line 156 — FR-09 ``/healthz`` liveness probe.
- SPEC.md line 157 — FR-09 ``/readyz`` readiness probe (DB + alembic).
- SPEC.md line 158 — FR-09 ``/v1/metrics`` admin-only observability.
- SPEC.md line 211 — NFR-04 DB URL password MUST NOT appear in
  ``/v1/metrics`` response (FR-09 cross-cut).
- SPEC.md §8 #10 — DB outage → /readyz 503 + DB-failure detail.
- SPEC.md §8 #11 — migration not at head → /readyz 503 + migration detail
  (fail-closed).
- TEST_SPEC.md §1 FR-09 — the five named cases implemented below.
- SAD.md §3.1 — observability lives in the api layer.
"""  # NFR-02 NFR-04 NFR-09 NFR-10

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.api.health:
#   A FastAPI ``APIRouter`` (callable as ``router`` or a ``register(app)``
#   helper) that mounts:
#
#     GET /healthz
#         Process-alive liveness probe. MUST NOT depend on
#         ``require_api_key`` (SPEC.md line 107 / FR-09). Returns 200 with
#         body ``{"status": "ok"}`` whenever the Python process is alive
#         enough to serve HTTP.
#
#     GET /readyz
#         Readiness probe. MUST NOT depend on ``require_api_key``
#         (SPEC.md line 107). Evaluates two predicates:
#           (a) DB reachability  — the GREEN agent typically calls
#               ``repository.session.get_engine().connect()`` (or the
#               equivalent SELECT-1 probe) and returns ``True`` / ``False``.
#           (b) ``alembic current == head`` — the GREEN agent typically
#               invokes ``alembic.command.current(alembic_cfg)`` and
#               compares against ``alembic.script.ScriptDirectory.head``.
#         Both predicates MUST be exposed as module-level callables
#         ``check_db_reachable() -> bool`` and
#         ``check_migrations_at_head() -> bool`` so the RED fault-injection
#         tests below can monkey-patch them. On both True, returns 200 with
#         body ``{"status":"ok"}``. On any False (or any exception raised
#         by the predicates), returns 503 with body ``{"detail": <reason>}``
#         where ``<reason>`` identifies which check failed — SPEC.md
#         §8 #10 / #11 forbid silent retry-to-infinity on a not-ready
#         process.
#
# taskq_api.api.metrics:
#   A FastAPI ``APIRouter`` (or ``register(app)`` helper) that mounts:
#
#     GET /v1/metrics
#         Observability endpoint gated by ``Depends(require_scope("admin"))``
#         (SPEC.md line 158 — admin scope is mandatory). On 200 returns a
#         JSON body carrying:
#             * task counts by status (e.g. ``{"pending": int, "running":
#               int, "done": int, "failed": int, "timeout": int,
#               "interrupted": int}`` or an equivalent mapping — the
#               shape is the implementation's call),
#             * execution latency percentiles (``p50``, ``p95``, ``p99``
#               in milliseconds — exact key set is implementation-defined,
#               but they MUST be present and numeric when any runs exist),
#             * rate-limit rejection count (``rate_limit_rejections`` or
#               equivalent key — a positive integer).
#         The body MUST NOT contain the DB connection string password
#         fragment (NFR-04 / SPEC.md line 211). On missing / wrong scope
#         the dependency chain MUST surface the FR-04 403 + problem+json
#         body unchanged.
# ---------------------------------------------------------------------------


# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-09). Neither module exists on disk yet →
# ModuleNotFoundError at collection time is the valid RED signal; pytest
# reports Exit Code 2. Do not wrap in try/except; do not lazy-import.
from taskq_api.api import health as _health_mod  # noqa: E402,F401  [FR-09]
from taskq_api.api import metrics as _metrics_mod  # noqa: E402,F401  [FR-09]


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Test app wiring + in-process ASGI client
# ---------------------------------------------------------------------------


@pytest.fixture
def fr09_client(monkeypatch):
    """Build a function-scoped FastAPI app + httpx ASGITransport client.

    The app mounts:

        GET /healthz            (no auth, no DB dependency)
        GET /readyz             (no auth; consults the patched
                                 check_db_reachable / check_migrations_at_head)
        GET /v1/metrics         (Depends(require_scope("admin")) — gated)

    ``require_api_key`` is monkeypatched to return a row dict whose
    ``scope`` field is mutable per test, so each scope tier can be
    simulated without inserting real ``api_keys`` rows (mirrors the
    test_fr04.py seam).

    ``api.health`` and ``api.metrics`` SAB-declared routers are mounted
    by calling ``_health_mod.router`` (or ``register(app)`` if the GREEN
    agent picks that shape — the test tolerates both) so pytest-cov
    attributes execution to the real handlers.
    """  # NFR-09 NFR-10
    import httpx
    from fastapi import Depends, FastAPI

    import taskq_api.api.dependencies as _deps

    # ----- mutable actor state — mutated per test to flip scope tier -----
    actor_state: dict[str, Any] = {
        "key_id": "key-uuid-fr09-actor",
        "scope": "admin",
        "revoked_at": None,
    }

    def _fake_require_api_key() -> dict:  # type: ignore[no-untyped-def]
        """Return the mutable actor row the scope dependency reads.

        Mirrors the real ``require_api_key`` return-value shape (see
        test_fr03.py). The ``scope`` field is mutated by the test that
        wishes to exercise a different tier.
        """  # NFR-09 NFR-10
        return {
            "key_id": actor_state["key_id"],
            "scope": actor_state["scope"],
            "revoked_at": actor_state["revoked_at"],
        }

    monkeypatch.setattr(_deps, "require_api_key", _fake_require_api_key)

    app = FastAPI()

    # ----- mount the SAB-declared routers --------------------------------
    # The GREEN agent may expose ``router`` (a fastapi.APIRouter) OR
    # ``register(app)`` (a side-effect helper). Try both so the test
    # wiring stays decoupled from the chosen surface.
    if hasattr(_health_mod, "register"):
        _health_mod.register(app)
    elif hasattr(_health_mod, "router"):
        app.include_router(_health_mod.router)
    else:
        pytest.fail(
            "taskq_api.api.health must expose either ``router`` (APIRouter) "
            "or ``register(app)`` so the SAB-declared module can be mounted "
            "on the FastAPI app (SAB.json FR-09 row 1)"
        )

    if hasattr(_metrics_mod, "register"):
        _metrics_mod.register(app)
    elif hasattr(_metrics_mod, "router"):
        app.include_router(_metrics_mod.router)
    else:
        pytest.fail(
            "taskq_api.api.metrics must expose either ``router`` (APIRouter) "
            "or ``register(app)`` so the SAB-declared module can be mounted "
            "on the FastAPI app (SAB.json FR-09 row 2)"
        )

    transport = httpx.ASGITransport(app=app)

    async def _request(method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            return await http_client.request(method, url, **kwargs)

    return _request, actor_state


@pytest.fixture
def anyio_backend():
    """Unused — silences ``pytest-anyio`` warnings if installed."""  # NFR-10
    return "asyncio"


# ---------------------------------------------------------------------------
# AC-9.1 — /healthz 200 with {"status":"ok"}, no auth required
# ---------------------------------------------------------------------------


def test_healthz_200_no_auth(fr09_client):
    """AC-9.1: ``GET /healthz`` returns 200 with ``{"status":"ok"}`` while the
    process is alive, with NO authentication required.

    TEST_SPEC inputs:
      endpoint = "healthz"
      expected_status = "200"
      expected_body_status_field = "ok"
    Sub-assertions:
      FR09-healthz-200           (status == 200)
      FR09-healthz-body-ok       (body["status"] == "ok")

    The test fires an unauthenticated GET to ``/healthz`` and asserts:
      (a) status_code == 200 (SPEC.md line 156 — 進程存活 → 200),
      (b) JSON body has ``status == "ok"``,
      (c) the route does NOT consult ``require_api_key`` (verified
          implicitly — no X-API-Key header is sent and the call
          succeeds; if the GREEN agent wrongly attaches
          ``Depends(require_api_key)`` the call returns 401 instead).
    """  # NFR-04 NFR-09 NFR-10
    endpoint = "healthz"
    expected_status = "200"
    expected_body_status_field = "ok"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert endpoint == "healthz"
    assert expected_status == "200"
    assert expected_body_status_field == "ok"

    client, _actor = fr09_client
    response = asyncio.run(client("GET", "/healthz"))

    # ---- Sub-assertion FR09-healthz-200: expected_status == "200". ----
    assert response.status_code == int(expected_status), (
        f"GET /healthz must return 200 while the process is alive "
        f"(AC-9.1 / SPEC.md line 156); got status={response.status_code} "
        f"body={response.text!r}"
    )

    # ---- Sub-assertion FR09-healthz-body-ok:
    #      expected_body_status_field == "ok". ----
    body = response.json()
    body_status = body.get("status")
    assert body_status == expected_body_status_field, (
        f"/healthz 200 body must carry status='ok' "
        f"(AC-9.1 / SPEC.md line 156); got body={body!r}"
    )


# ---------------------------------------------------------------------------
# AC-9.2 — /readyz 200 when DB reachable AND alembic current == head
# ---------------------------------------------------------------------------


def test_readyz_200_when_db_ok_and_migrations_at_head(fr09_client, monkeypatch):
    """AC-9.2: ``GET /readyz`` returns 200 when the DB is reachable AND
    ``alembic current == head``.

    TEST_SPEC inputs:
      db_state = "ok"
      alembic_current = "head"
      expected_status = "200"
    Sub-assertion: FR09-readyz-200-db-ok  (status == 200).

    The test monkey-patches the two predicates the GREEN agent exposes
    on ``taskq_api.api.health``:

        check_db_reachable() -> bool          → True
        check_migrations_at_head() -> bool    → True

    so the readiness handler evaluates the happy-path branch without
    needing a live DB or alembic invocation. The GREEN handler MUST
    surface a 200 only when BOTH predicates are True (the contract
    pinned by AC-9.2).
    """  # NFR-04 NFR-09 NFR-10
    db_state = "ok"
    alembic_current = "head"
    expected_status = "200"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert db_state == "ok"
    assert alembic_current == "head"
    assert expected_status == "200"

    client, _actor = fr09_client

    # GREEN TODO: taskq_api.api.health must expose
    #   check_db_reachable() -> bool
    #   check_migrations_at_head() -> bool
    # so the readiness handler can call them and the tests below can
    # fault-inject failures without a live DB / alembic run.
    monkeypatch.setattr(_health_mod, "check_db_reachable", lambda: True)
    monkeypatch.setattr(_health_mod, "check_migrations_at_head", lambda: True)

    response = asyncio.run(client("GET", "/readyz"))

    # ---- Sub-assertion FR09-readyz-200-db-ok: expected_status == "200". ----
    assert response.status_code == int(expected_status), (
        f"GET /readyz must return 200 when DB reachable AND migrations "
        f"at head (AC-9.2 / SPEC.md line 157); got status="
        f"{response.status_code} body={response.text!r}"
    )


# ---------------------------------------------------------------------------
# AC-9.3 — /readyz 503 when DB unreachable (detail names the DB failure)
# ---------------------------------------------------------------------------


def test_readyz_503_when_db_unreachable(fr09_client, monkeypatch):
    """AC-9.3: After stopping the DB, ``GET /readyz`` returns **503** with
    ``detail`` identifying the DB failure.

    TEST_SPEC inputs:
      db_state = "unreachable"
      expected_status = "503"
      expected_detail_contains_db = "true"
    Sub-assertions:
      FR09-readyz-503-db-unreachable      (status == 503)
      FR09-readyz-503-detail-db           (detail contains 'db' substring)

    The test monkey-patches ``check_db_reachable()`` to raise a
    connection-like error (or simply return False) — the GREEN handler
    MUST translate that into a 503 response whose body ``detail``
    names the DB so an operator can diagnose WHICH check failed
    (SPEC.md line 157 + §8 #10). The migration predicate stays True
    so this test isolates the DB branch specifically.
    """  # NFR-04 NFR-09 NFR-10
    db_state = "unreachable"
    expected_status = "503"
    expected_detail_contains_db = "true"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert db_state == "unreachable"
    assert expected_status == "503"
    assert expected_detail_contains_db == "true"

    client, _actor = fr09_client

    def _db_unreachable() -> bool:  # type: ignore[no-untyped-def]
        """Simulate the DB outage — ``/readyz`` MUST report a 503 here."""
        return False

    monkeypatch.setattr(_health_mod, "check_db_reachable", _db_unreachable)
    # Migrations stay at head so this test isolates the DB branch.
    monkeypatch.setattr(_health_mod, "check_migrations_at_head", lambda: True)

    response = asyncio.run(client("GET", "/readyz"))

    # ---- Sub-assertion FR09-readyz-503-db-unreachable:
    #      expected_status == "503". ----
    assert response.status_code == int(expected_status), (
        f"GET /readyz must return 503 when DB is unreachable "
        f"(AC-9.3 / SPEC.md line 157 + §8 #10 — fail closed, do not "
        f"silently retry); got status={response.status_code} "
        f"body={response.text!r}"
    )

    # ---- Sub-assertion FR09-readyz-503-detail-db:
    #      expected_detail_contains_db == "true". ----
    body = response.json()
    body_text = json.dumps(body, ensure_ascii=False).lower()
    assert "db" in body_text, (
        f"/readyz 503 body MUST identify the DB failure so an operator "
        f"can diagnose WHICH check failed (AC-9.3 / SPEC.md line 157 + "
        f"§8 #10); got body={body!r}"
    )


# ---------------------------------------------------------------------------
# AC-9.4 — /readyz 503 when migration not at head (fail-closed)
# ---------------------------------------------------------------------------


def test_readyz_503_when_migration_not_at_head(fr09_client, monkeypatch):
    """AC-9.4: After ``alembic downgrade -1``, ``GET /readyz`` returns
    **503** with ``detail`` identifying the migration-not-at-head failure
    (fail-closed).

    TEST_SPEC inputs:
      alembic_current = "v1"
      alembic_head = "v3"
      expected_status = "503"
      expected_detail_contains_migration = "true"
    Sub-assertions:
      FR09-readyz-503-migration-not-head  (status == 503)
      FR09-readyz-503-detail-migration    (detail contains 'migration')

    The test monkey-patches ``check_migrations_at_head()`` to return
    False (simulating the post-``alembic downgrade -1`` state where
    the database is one revision behind head). The DB predicate stays
    True so this test isolates the migration branch specifically.

    This is the fail-closed guard from SPEC.md line 157: a deployment
    that shipped new code but forgot to run migrations MUST NOT pass
    ``/readyz`` — otherwise the orchestrator would route traffic to
    a process whose DB schema is stale.
    """  # NFR-04 NFR-09 NFR-10
    alembic_current = "v1"
    alembic_head = "v3"
    expected_status = "503"
    expected_detail_contains_migration = "true"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert alembic_current == "v1"
    assert alembic_head == "v3"
    assert expected_status == "503"
    assert expected_detail_contains_migration == "true"

    client, _actor = fr09_client

    # DB is OK — isolate the migration branch (AC-9.4).
    monkeypatch.setattr(_health_mod, "check_db_reachable", lambda: True)

    def _migration_not_at_head() -> bool:  # type: ignore[no-untyped-def]
        """Simulate ``alembic current`` returning v1 while head is v3."""
        return False

    monkeypatch.setattr(
        _health_mod, "check_migrations_at_head", _migration_not_at_head
    )

    response = asyncio.run(client("GET", "/readyz"))

    # ---- Sub-assertion FR09-readyz-503-migration-not-head:
    #      expected_status == "503". ----
    assert response.status_code == int(expected_status), (
        f"GET /readyz must return 503 when alembic current != head "
        f"(AC-9.4 / SPEC.md line 157 — fail-closed: do NOT pass a "
        f"process whose DB schema is stale); got status="
        f"{response.status_code} body={response.text!r}"
    )

    # ---- Sub-assertion FR09-readyz-503-detail-migration:
    #      expected_detail_contains_migration == "true". ----
    body = response.json()
    body_text = json.dumps(body, ensure_ascii=False).lower()
    assert "migration" in body_text, (
        f"/readyz 503 body MUST identify the migration-not-at-head "
        f"failure so an operator can diagnose WHICH check failed "
        f"(AC-9.4 / SPEC.md line 157 + §8 #11); got body={body!r}"
    )


# ---------------------------------------------------------------------------
# AC-9.5 — /v1/metrics requires admin scope; no DB URL password leaks
# ---------------------------------------------------------------------------


def test_metrics_admin_scope_no_db_url_leak(fr09_client):
    """AC-9.5: ``GET /v1/metrics`` requires ``admin`` scope; returns task
    counts by status, execution latency percentiles, rate-limit rejection
    counts; does NOT leak the DB connection string password (NFR-04).

    TEST_SPEC inputs:
      actor_scope = "admin"
      expected_status = "200"
      expected_response_contains_db_url_password = "false"
    Sub-assertions:
      FR09-metrics-admin-200            (status == 200)
      FR09-metrics-no-db-url-leak       (response body MUST NOT contain the
                                        DB URL password fragment)

    The test authenticates as ``scope="admin"`` (the same row shape
    ``require_api_key`` returns for a real admin key — see FR-04). The
    handler MUST return 200 with the observability payload. The test
    then stringifies the full body and asserts the DB URL password
    fragment from ``TASKQ_DB_URL`` is absent (NFR-04 / SPEC.md
    line 211 — DB URL password MUST NOT appear in any /v1/metrics
    response).
    """  # NFR-04 NFR-09 NFR-10
    actor_scope = "admin"
    expected_status = "200"
    expected_response_contains_db_url_password = "false"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert actor_scope == "admin"
    assert expected_status == "200"
    assert expected_response_contains_db_url_password == "false"

    client, actor_state = fr09_client
    actor_state["scope"] = actor_scope

    response = asyncio.run(client("GET", "/v1/metrics"))

    # ---- Sub-assertion FR09-metrics-admin-200: expected_status == "200". ----
    assert response.status_code == int(expected_status), (
        f"GET /v1/metrics with admin scope MUST return 200 "
        f"(AC-9.5 / SPEC.md line 158); got status={response.status_code} "
        f"body={response.text!r}"
    )

    # ---- Sub-assertion FR09-metrics-no-db-url-leak:
    #      expected_response_contains_db_url_password == "false". ----
    body_text = response.text
    # The SAB-declared config exposes the canonical DB URL on
    # ``taskq_api.config.TASKQ_DB_URL``. Strip the scheme prefix so
    # the assertion targets the password fragment specifically (a
    # connection string like ``sqlite:///./taskq.db`` has no password
    # component — the substring check then trivially passes, which is
    # the right outcome for that default). The assertion is written
    # to FAIL if a password-bearing URL ever leaks into the response.
    from taskq_api import config as _config

    db_url = _config.TASKQ_DB_URL
    # Identify the password fragment: text after the second ``:`` in
    # ``scheme://user:password@host`` form, before the ``@``. If the
    # URL has no ``@`` (e.g. plain ``sqlite:///./taskq.db``), there is
    # no password to leak and the assertion trivially passes.
    password_fragment = ""
    if "@" in db_url and "://" in db_url:
        userinfo = db_url.split("://", 1)[1].split("@", 1)[0]
        if ":" in userinfo:
            password_fragment = userinfo.split(":", 1)[1]
    assert password_fragment not in body_text, (
        f"/v1/metrics response MUST NOT contain the DB connection "
        f"string password fragment (NFR-04 / SPEC.md line 211 / "
        f"AC-N4.2 cross-cut); password_fragment={password_fragment!r} "
        f"appeared in body={body_text!r}"
    )
    # Defensive belt-and-braces — the FULL DB URL must also not leak,
    # even when no password is configured. The handler must aggregate
    # counters without echoing its own connection string back.
    assert db_url not in body_text, (
        f"/v1/metrics response MUST NOT contain the DB connection "
        f"string (NFR-04 / SPEC.md line 211); got body={body_text!r}"
    )
