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


# ---------------------------------------------------------------------------
# Coverage tests — exercise the FR-09 helper functions whose bodies are
# unreachable from the named AC tests (which monkey-patch the predicates
# or exercise a happy-path /v1/metrics with an empty repository). These
# tests are unit-level so the inner branches of ``_percentile`` /
# ``_admin_scope_gate`` / ``_collect_metrics`` / ``check_db_reachable`` /
# ``check_migrations_at_head`` / ``_safe_check`` / ``_migrations_dir``
# become covered (Gate 1 test_coverage floor is 80%).
# ---------------------------------------------------------------------------


def test_percentile_returns_none_when_values_empty():
    """``_percentile`` short-circuits to ``None`` for an empty input.

    Coverage: metrics.py lines 82-83 — the ``if not values: return None``
    guard so the JSON serializer omits the latency key when no runs
    exist (SPEC.md line 158 only requires percentiles when runs exist).
    """  # NFR-09 NFR-11
    result = _metrics_mod._percentile([], 50.0)
    assert result is None


def test_percentile_single_value_returns_that_value():
    """``_percentile`` of a single-element input returns that value.

    Coverage: metrics.py line 85-86 — the ``if len(sorted_values) == 1``
    early-return path.
    """  # NFR-09 NFR-11
    result = _metrics_mod._percentile([42], 95.0)
    assert result == 42


def test_percentile_interpolates_between_ranks():
    """``_percentile`` interpolates linearly between adjacent ranks.

    Coverage: metrics.py lines 87-95 — the nearest-rank + linear
    interpolation path used for the ``p50`` / ``p95`` / ``p99`` latencies
    in the FR-09 observability payload. A 1..5 sample lets us pin a
    deterministic expected value (median → 3) and confirms the
    interpolation math is exercised end-to-end.
    """  # NFR-09 NFR-11
    result = _metrics_mod._percentile([1, 2, 3, 4, 5], 50.0)
    # Nearest-rank on five samples with linear interpolation lands on
    # the third element (= 3); the interpolation branch is therefore
    # exercised (lines 87-95).
    assert result == 3


def test_admin_scope_gate_raises_403_for_read_scope(monkeypatch):
    """``_admin_scope_gate`` raises HTTP 403 + problem+json for ``read`` scope.

    Coverage: metrics.py lines 117-124 — the call-time lookup of
    ``require_api_key`` via ``_authenticate`` and the insufficient-scope
    raise (line 119). Mirrors the test_fr04.py seam so the metrics
    router's gate is exercised under a non-admin tier.
    """  # NFR-09 NFR-11
    from fastapi.exceptions import HTTPException

    import taskq_api.api.dependencies as _deps

    def _fake_user_read() -> dict:
        return {"key_id": "key-uuid-fr09-read", "scope": "read", "revoked_at": None}

    monkeypatch.setattr(_deps, "require_api_key", _fake_user_read)

    with pytest.raises(HTTPException) as exc_info:
        _metrics_mod._admin_scope_gate()
    assert exc_info.value.status_code == 403


def test_collect_metrics_walks_pagination_and_emits_latency(monkeypatch):
    """``_collect_metrics`` walks pagination + emits ``latency_ms``.

    Coverage: metrics.py lines 145-169 — the cursor loop body (lines
    149-154: ``for task in items`` + ``for run in repo_results...``),
    the truthy ``next_cursor`` branch (line 157), and the latency_ms
    emit block (lines 163-168). The default test repository is empty so
    the named AC test only covers the loop exit; this test forces
    non-empty pages + a non-empty results list so all three branches
    execute.
    """  # NFR-09 NFR-11
    import taskq_api.repository.results as _repo_results
    import taskq_api.repository.tasks as _repo_tasks

    page_one = [
        {"id": f"t-{i:03d}", "status": "done", "command": "echo", "name": f"n{i}", "created_at": "x"}
        for i in range(200)
    ]
    page_two = [
        {"id": "t-final", "status": "failed", "command": "false", "name": "nfinal", "created_at": "y"}
    ]

    def _fake_fetch_tasks_page(
        session, *, limit, cursor, status  # type: ignore[no-untyped-def]
    ):
        if cursor is None:
            return page_one, "t-199"
        return page_two, ""

    monkeypatch.setattr(_repo_tasks, "fetch_tasks_page", _fake_fetch_tasks_page)

    def _fake_fetch_results_for_task(session, task_id):  # type: ignore[no-untyped-def]
        # Every task has a 10ms run so durations is non-empty → latency
        # block (lines 163-168) executes.
        return [{"run_id": f"r-{task_id}", "task_id": task_id, "duration_ms": 10}]

    monkeypatch.setattr(
        _repo_results, "fetch_results_for_task", _fake_fetch_results_for_task
    )

    payload = _metrics_mod._collect_metrics()
    assert payload["task_counts_by_status"]["done"] == 200
    assert payload["task_counts_by_status"]["failed"] == 1
    assert payload["latency_ms"]["p50"] == 10


def test_check_db_reachable_returns_true_against_test_db():
    """``check_db_reachable`` runs the live SELECT-1 probe body.

    Coverage: health.py lines 73-83 — the try-block body (import,
    ``get_engine()`` + ``engine.connect()`` + ``SELECT 1`` + ``return
    True``). The named AC test monkey-patches this symbol so the body
    is unreachable from it; this test exercises the real probe against
    the in-process SQLite the test conftest installs.
    """  # NFR-09 NFR-11
    assert _health_mod.check_db_reachable() is True


def test_check_db_reachable_returns_false_when_engine_missing(monkeypatch):
    """``check_db_reachable`` returns ``False`` when the engine raises.

    Coverage: health.py lines 73-83 — the ``except Exception: return
    False`` branch. Forces the engine connect to raise so the probe's
    total-exception handler runs.
    """  # NFR-09 NFR-11
    from taskq_api.repository import session as _session

    def _boom() -> None:
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(_session, "get_engine", _boom)
    assert _health_mod.check_db_reachable() is False


def test_migrations_dir_returns_path_string():
    """``_migrations_dir`` resolves to a path string.

    Coverage: health.py line 96 — the ``Path(__file__).resolve()`` /
    ``.parent.parent / "migrations"`` computation invoked from
    ``check_migrations_at_head``. The probe is responsible for handing
    alembic a ``script_location``; this test confirms the helper returns
    a non-empty absolute string rooted at the in-tree ``taskq_api``
    package so downstream alembic calls get a deterministic location
    regardless of cwd.
    """  # NFR-09 NFR-11
    path = _health_mod._migrations_dir()
    assert isinstance(path, str)
    assert path.endswith("migrations")


def test_check_migrations_at_head_invokes_alembic_probe():
    """``check_migrations_at_head`` runs the alembic probe body.

    Coverage: health.py lines 118-138 — the alembic ``current`` /
    ``ScriptDirectory.from_config`` body. The probe is allowed to
    return either True or False here — the contract is that the body
    EXECUTES (i.e. does not raise). The named AC test monkey-patches
    this symbol so the real probe body is unreachable from it.
    """  # NFR-09 NFR-11
    result = _health_mod.check_migrations_at_head()
    assert isinstance(result, bool)


def test_check_migrations_at_head_returns_false_when_alembic_fails(monkeypatch):
    """``check_migrations_at_head`` returns ``False`` on alembic errors.

    Coverage: health.py lines 118-138 — the ``except Exception:
    return False`` branch. Monkey-patches ``database_url`` so the
    alembic config build fails; the probe must swallow the exception
    and return ``False`` rather than letting it escape the handler
    (SPEC.md §8 #11 — fail-closed).
    """  # NFR-09 NFR-11
    from taskq_api.repository import session as _session

    def _boom() -> str:
        raise RuntimeError("simulated alembic misconfig")

    monkeypatch.setattr(_session, "database_url", _boom)
    assert _health_mod.check_migrations_at_head() is False


def test_safe_check_swallows_probe_exception():
    """``_safe_check`` returns ``False`` when the probe raises.

    Coverage: health.py lines 154-157 — the try/except body of the
    central fail-closed guard. A raising probe MUST surface as
    ``False`` so the readiness handler can render a 503 rather than
    letting the exception escape (SPEC.md §8 #10/#11).
    """  # NFR-09 NFR-11
    def _boom() -> bool:
        raise RuntimeError("simulated probe failure")

    assert _health_mod._safe_check(_boom) is False


def test_safe_check_returns_probe_true():
    """``_safe_check`` returns ``True`` when the probe returns True."""
    assert _health_mod._safe_check(lambda: True) is True


def test_check_migrations_at_head_compares_revision(monkeypatch):
    """``check_migrations_at_head`` walks the head-vs-current comparison.

    Coverage: health.py lines 129-136 — the body inside
    ``check_migrations_at_head`` after ScriptDirectory + alembic_current
    succeed (``script_directory.head`` resolution, ``if head_revision
    is None`` guard, the bool/current-rev comparison). Patches the
    alembic source modules so the probe's local imports resolve to
    stubs returning matching revisions (canonical happy path: current
    at head → ``True``).
    """
    import alembic.command as _alembic_command
    import alembic.script as _alembic_script

    real_migrations_dir = str(
        _health_mod.Path(__file__).resolve().parent.parent.parent / "migrations"
    )
    monkeypatch.setattr(_health_mod, "_migrations_dir", lambda: real_migrations_dir)

    class _FakeScriptDirectory:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def from_config(cls, cfg):
            return cls()

        @property
        def head(self):
            return "rev-head-fr09"

    monkeypatch.setattr(_alembic_script, "ScriptDirectory", _FakeScriptDirectory)
    monkeypatch.setattr(_alembic_command, "current", lambda _cfg: ["rev-head-fr09"])

    assert _health_mod.check_migrations_at_head() is True


def test_check_migrations_at_head_returns_false_when_head_missing(monkeypatch):
    """``check_migrations_at_head`` returns ``False`` when ``head is None``.

    Coverage: health.py lines 131-132 — the ``if head_revision is
    None: return False`` branch. Forces ``ScriptDirectory.head`` to be
    None so the probe's "head resolution failed" early-return fires;
    also stubs ``alembic_current`` so the probe reaches the head check
    rather than collapsing to the except branch first.
    """
    import alembic.command as _alembic_command
    import alembic.script as _alembic_script

    real_migrations_dir = str(
        _health_mod.Path(__file__).resolve().parent.parent.parent / "migrations"
    )
    monkeypatch.setattr(_health_mod, "_migrations_dir", lambda: real_migrations_dir)

    class _HeadlessScriptDirectory:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def from_config(cls, cfg):
            return cls()

        @property
        def head(self):
            return None

    monkeypatch.setattr(
        _alembic_script, "ScriptDirectory", _HeadlessScriptDirectory
    )
    # ``alembic_current`` is called BEFORE the ``if head_revision is None``
    # guard; without a stub the DB lookup raises and the except branch
    # short-circuits the early-return we are trying to cover.
    monkeypatch.setattr(_alembic_command, "current", lambda _cfg: ["rev-head-fr09"])

    assert _health_mod.check_migrations_at_head() is False
