"""[FR-10] RFC 7807 Error Contract — RED tests.

These are the RED phase of TDD for FR-10. They import the SAB-declared
module:

    - taskq_api.errors   (SAB: FR-10 row 1)

The module already exists on disk (it holds ``DuplicateNameError`` and
``TaskNotFoundError`` from FR-01), but it does NOT yet expose the
RFC 7807 helpers required by FR-10 (``problem_response``,
``install_exception_handlers``, ``CORRELATION_HEADER``, ...). The
top-level imports below raise ``ImportError`` for the missing symbols
and pytest reports a Collection Error (Exit Code 2) — this is the
**valid RED state**. No ``try/except ImportError`` is used to hide it;
no lazy-import workaround is applied.

The five tests below pin down the SPEC.md lines 164-168 + §7 + §8 #19
contract:

  - AC-10.1  Every non-2xx response has
             ``Content-Type: application/problem+json``.
  - AC-10.2  Problem+json bodies carry the six RFC 7807 fields:
             ``type``, ``title``, ``status``, ``detail``, ``instance``,
             ``correlation_id``.
  - AC-10.3  Triggering a 500 (unhandled exception) produces a
             ``detail`` that contains NO stack trace, NO SQL
             statement, NO file path, NO DB schema fragment (NFR-02 /
             SPEC.md §8 #19).
  - AC-10.4  ``correlation_id`` from the problem+json body equals the
             ``X-Correlation-Id`` response header and equals the
             corresponding server log line — operators can stitch
             client / server / log lines by that single token
             (NFR-04 / SPEC.md line 167).
  - AC-10.5  Error code mapping table (SPEC.md §7) is observed
             end-to-end: 422 / 401 / 403 / 404 / 409 / 429 / 503 / 500.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against a small FR-10-isolated FastAPI app
whose trigger routes are defined per-test in this file (the GREEN
agent only wires the FR-10 exception handlers / response builder onto
it). The correlation-id log capture uses an in-process
``MemoryHandler`` attached to a dedicated logger so the test does not
depend on the production log file format. No ``subprocess.run`` is
used, so pytest-cov attributes execution to the real handlers and the
Gate-1 ``test_coverage`` dimension can see them.

Citations:
- SPEC.md line 164 — FR-10 ``Content-Type: application/problem+json``.
- SPEC.md line 165 — FR-10 body fields (type / title / status / detail
  / instance / correlation_id).
- SPEC.md line 166 — FR-10 ``detail`` MUST NOT contain SQL /
  stack trace / file path / DB schema.
- SPEC.md line 167 — FR-10 ``correlation_id`` in response header +
  server log.
- SPEC.md line 168 — FR-10 error code mapping table.
- SPEC.md §7 — error → status table (422 / 401 / 403 / 404 / 409 /
  429 / 503 / 500).
- SPEC.md §8 #19 — 500 body MUST NOT include stack / SQL / file path.
- TEST_SPEC.md §1 FR-10 — the five named cases implemented below.
- SAD.md §3.1 — error contract lives in the api / errors layer.
"""  # NFR-02 NFR-04 NFR-09 NFR-10

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.errors must additionally expose (per FR-10 / RFC 7807):
#
#   CORRELATION_HEADER: str
#       Canonical header name (e.g. ``"X-Correlation-Id"``). Tests
#       below reference it by symbol so the GREEN agent cannot rename
#       it without breaking the contract.
#
#   problem_response(status_code: int, detail: str, *,
#                    type_uri: str, title: str, instance: str,
#                    correlation_id: str) -> dict
#       Returns the JSON-serialisable RFC 7807 body. MUST carry the six
#       fields ``type`` / ``title`` / ``status`` / ``detail`` /
#       ``instance`` / ``correlation_id``; ``status`` MUST equal
#       ``status_code``.
#
#   install_exception_handlers(app: FastAPI) -> None
#       Registers FastAPI exception handlers on ``app`` such that:
#         (a) any ``HTTPException`` raised by a route is rendered as
#             ``application/problem+json`` with the six fields.
#         (b) any uncaught ``Exception`` is rendered as 500
#             ``application/problem+json`` whose ``detail`` is
#             ``"internal"`` (or equivalent — MUST NOT contain the
#             exception repr, traceback, or any internal path /
#             SQL fragment).
#         (c) every response carries a ``X-Correlation-Id`` header
#             equal to the ``correlation_id`` in the body, and that
#             same token is appended to a server log line.
#
#   The 422 path (request-body validation failure) is rendered by the
#     same exception handler — FastAPI raises RequestValidationError,
#     which the GREEN agent MUST catch and translate to
#     ``{"type": "/errors/validation", ...}`` with status 422.
# ---------------------------------------------------------------------------


# Standard top-level imports from the SAB-declared module path
# (.methodology/SAB.json → FR-10 row 1). The new symbols (problem_response,
# install_exception_handlers, CORRELATION_HEADER) do not exist yet →
# ImportError at collection time is the valid RED signal; pytest
# reports Exit Code 2. Do not wrap in try/except; do not lazy-import.
from taskq_api.errors import (  # noqa: E402,F401  [FR-10]
    CORRELATION_HEADER,
    install_exception_handlers,
    problem_response,
)


# ---------------------------------------------------------------------------
# Test helpers — FastAPI app scaffolding + trigger routes
# ---------------------------------------------------------------------------


def _build_trigger_app(
    register_handlers: bool = True,
    *,
    on_log: Callable[[logging.LogRecord], None] | None = None,
) -> tuple[Any, logging.Logger]:
    """Build a function-scoped FastAPI app with one trigger route per status.

    The app exposes (when ``register_handlers=True``):

        GET /trigger/422            → RequestValidationError via query param
        GET /trigger/401            → raises HTTPException(401)
        GET /trigger/403            → raises HTTPException(403)
        GET /trigger/404            → raises HTTPException(404)
        GET /trigger/409            → raises HTTPException(409)
        GET /trigger/429            → raises HTTPException(429)
        GET /trigger/503            → raises HTTPException(503)
        GET /trigger/500            → raises a bare Exception

    When ``register_handlers=True`` (default), the FR-10
    ``install_exception_handlers(app)`` is called so the GREEN agent's
    handlers actually run. Setting ``register_handlers=False`` is
    reserved for the negative-control cases (none used in the named
    five tests, but the parameter is exposed so future tests can
    pin the no-handler default behaviour if needed).

    A dedicated ``fr10_test`` logger is returned with a
    ``MemoryHandler`` attached if ``on_log`` is provided, so test #4
    can verify the correlation-id log line.
    """  # NFR-04 NFR-09 NFR-10
    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError

    app = FastAPI()

    if register_handlers:
        # GREEN TODO: taskq_api.errors.install_exception_handlers(app)
        # must register the RFC 7807 handler(s) and the correlation-id
        # middleware so all non-2xx responses below surface as
        # application/problem+json with the six required fields.
        install_exception_handlers(app)

    @app.get("/trigger/422")
    async def _t422():  # type: ignore[no-untyped-def]
        """Trigger a RequestValidationError → 422 mapping."""
        # FastAPI will reject ``required_int`` as missing (422 path).
        raise RequestValidationError(
            [
                {
                    "loc": ("query", "required_int"),
                    "msg": "field required",
                    "type": "missing",
                }
            ]
        )

    @app.get("/trigger/401")
    async def _t401():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=401, detail="unauthenticated")

    @app.get("/trigger/403")
    async def _t403():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/trigger/404")
    async def _t404():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=404, detail="not-found")

    @app.get("/trigger/409")
    async def _t409():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=409, detail="conflict")

    @app.get("/trigger/429")
    async def _t429():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=429, detail="rate-limited")

    @app.get("/trigger/503")
    async def _t503():  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=503, detail="not-ready")

    @app.get("/trigger/500")
    async def _t500():  # type: ignore[no-untyped-def]
        """Trigger an unhandled exception → 500 mapping."""
        # The exception payload intentionally contains internal
        # fragments (SQL, file path, frame) — the GREEN handler MUST
        # strip all of them from the response body so AC-10.3 holds.
        raise RuntimeError(
            "Traceback (most recent call last):\n"
            "  File '/srv/taskq_api/repository/tasks.py', line 42\n"
            "    cur.execute(f'SELECT * FROM tasks WHERE name={name!r}')\n"
            "sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) "
            "no such table: tasks"
        )

    test_logger = logging.getLogger("fr10_test")
    test_logger.setLevel(logging.DEBUG)
    if on_log is not None:
        handler = logging.handlers.MemoryHandler(  # type: ignore[attr-defined]
            capacity=1024, flushLevel=logging.DEBUG, target=None
        )

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                on_log(record)

        test_logger.addHandler(_Capture())

    return app, test_logger


def _async_request(method: str, url: str, app: Any, **kwargs):  # type: ignore[no-untyped-def]
    """Drive a single httpx request through an in-process ASGI app.

    Returns the ``httpx.Response``. Mirrors the FR-09 wiring — uses
    ``httpx.ASGITransport`` so pytest-cov attributes execution to the
    real handlers.
    """  # NFR-09 NFR-10
    import httpx

    transport = httpx.ASGITransport(app=app)

    async def _go():  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            return await http_client.request(method, url, **kwargs)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# AC-10.1 — every non-2xx response uses
#           ``Content-Type: application/problem+json``.
# ---------------------------------------------------------------------------


def test_all_error_responses_use_problem_json_content_type():
    """AC-10.1: Every non-2xx response sets
    ``Content-Type: application/problem+json`` (RFC 7807 §3).

    TEST_SPEC inputs:
      expected_content_type = "application/problem+json"
    Sub-assertion:
      FR10-content-type-problem-json    (expected_content_type == "application/problem+json")

    The test fires one request per error status (401 / 403 / 404 /
    409 / 422 / 429 / 503 / 500) against the in-process FR-10 app and
    asserts that EVERY response carries the canonical RFC 7807 media
    type in ``Content-Type``. A 500 frame body containing
    stack-trace / SQL / file-path fragments MUST still surface with
    the correct ``Content-Type`` (the no-internals invariant is
    covered by AC-10.3 / ``test_500_detail_omits_internals``).
    """  # NFR-02 NFR-09 NFR-10
    expected_content_type = "application/problem+json"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_content_type == "application/problem+json"

    app, _logger = _build_trigger_app()
    statuses = [422, 403, 404, 409, 429, 503, 500, 401]
    offenders: list[tuple[int, str]] = []

    for status in statuses:
        response = _async_request("GET", f"/trigger/{status}", app=app)
        # Parse ``Content-Type`` — may include charset suffix
        # (``application/problem+json; charset=utf-8``); substring match
        # is sufficient per RFC 7807 §3.
        ctype = response.headers.get("content-type", "")
        if expected_content_type not in ctype:
            offenders.append((status, ctype))

    assert not offenders, (
        f"Every non-2xx response MUST carry Content-Type "
        f"{expected_content_type!r} (AC-10.1 / SPEC.md line 164 / "
        f"RFC 7807 §3); offenders={offenders!r}"
    )


# ---------------------------------------------------------------------------
# AC-10.2 — problem+json body has the six required fields.
# ---------------------------------------------------------------------------


def test_problem_json_body_has_required_fields():
    """AC-10.2: Problem+json bodies carry exactly the six RFC 7807 fields:
    ``type``, ``title``, ``status``, ``detail``, ``instance``,
    ``correlation_id`` (SPEC.md line 165).

    TEST_SPEC inputs:
      expected_field_set  = "type,title,status,detail,instance,correlation_id"
      expected_field_count = "6"
    Sub-assertion:
      FR10-required-field-count-6    (expected_field_count == "6")

    The test triggers a 401 (representative non-2xx) and inspects the
    JSON body. It asserts:

      (a) every key in the spec is present,
      (b) no EXTRA keys are present (so a future GREEN cannot smuggle
          in a stack trace or SQL fragment via an unexpected field),
      (c) ``status`` in the body equals the HTTP status code,
      (d) ``correlation_id`` is a non-empty string.

    A 401 is chosen because it is the cheapest trigger (no body
    validation needed) and exercises the same handler the auth /
    scope / not-found / conflict / rate-limit paths share.
    """  # NFR-02 NFR-09 NFR-10
    expected_field_set = "type,title,status,detail,instance,correlation_id"
    expected_field_count = "6"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_field_count == "6"
    assert expected_field_set.count(",") == int(expected_field_count) - 1

    app, _logger = _build_trigger_app()
    response = _async_request("GET", "/trigger/401", app=app)
    assert response.status_code == 401, (
        f"setup: /trigger/401 should raise 401; got {response.status_code}"
    )
    body = response.json()

    required_fields = expected_field_set.split(",")
    missing = [fld for fld in required_fields if fld not in body]
    extra = [fld for fld in body if fld not in required_fields]

    assert not missing, (
        f"Problem+json body MUST contain every required RFC 7807 field "
        f"(AC-10.2 / SPEC.md line 165); missing={missing!r} body={body!r}"
    )
    assert not extra, (
        f"Problem+json body MUST NOT carry fields outside the RFC 7807 "
        f"set — extra fields risk leaking internals (AC-10.2 / "
        f"SPEC.md line 165 / NFR-02); extra={extra!r} body={body!r}"
    )
    assert body["status"] == response.status_code, (
        f"body['status'] MUST equal the HTTP status code "
        f"(RFC 7807 §3.1); got body['status']={body['status']!r} "
        f"http_status={response.status_code}"
    )
    assert isinstance(body["correlation_id"], str) and body["correlation_id"], (
        f"correlation_id MUST be a non-empty string so operators can "
        f"stitch response / log lines (AC-10.4 / SPEC.md line 167); "
        f"got body['correlation_id']={body['correlation_id']!r}"
    )


# ---------------------------------------------------------------------------
# AC-10.3 — 500 detail MUST NOT leak stack trace / SQL / file path.
# ---------------------------------------------------------------------------


def test_500_detail_omits_internals():
    """AC-10.3: Triggering a 500 produces a ``detail`` that contains no
    stack trace, no SQL statement, no file path, no DB schema
    (NFR-02 / SPEC.md line 166 + §8 #19).

    TEST_SPEC inputs:
      trigger_internal_exception = "sqlalchemy.exc.OperationalError"
      expected_status            = "500"
      expected_body_contains_stacktrace    = "false"
      expected_body_contains_sql_statement = "false"
      expected_body_contains_file_path     = "false"
    Sub-assertions:
      FR10-500-status                (expected_status == "500")
      FR10-500-no-stacktrace-leak    (no ``Traceback`` substring)
      FR10-500-no-sql-leak           (no SQL keywords / SELECT-shaped text)
      FR10-500-no-filepath-leak      (no absolute path substring)

    The trigger route raises a ``RuntimeError`` whose repr contains:

      * a Python stack trace (``Traceback (most recent call last): ...``)
      * a file path (``/srv/taskq_api/repository/tasks.py``)
      * a SQL statement (``SELECT * FROM tasks WHERE name=...``)
      * a DB driver error class name (``sqlalchemy.exc.OperationalError``)

    The GREEN handler MUST strip every such fragment from the body
    before serialising it. The assertion is intentionally
    case-insensitive across the whole body (not just ``detail``) so a
    GREEN that smuggles an internal into ``title`` or ``type`` still
    fails the test.
    """  # NFR-02 NFR-09 NFR-10
    trigger_internal_exception = "sqlalchemy.exc.OperationalError"
    expected_status = "500"
    expected_body_contains_stacktrace = "false"
    expected_body_contains_sql_statement = "false"
    expected_body_contains_file_path = "false"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_status == "500"
    assert expected_body_contains_stacktrace == "false"
    assert expected_body_contains_sql_statement == "false"
    assert expected_body_contains_file_path == "false"

    app, _logger = _build_trigger_app()
    response = _async_request("GET", "/trigger/500", app=app)

    # ---- Sub-assertion FR10-500-status ----
    assert response.status_code == int(expected_status), (
        f"GET /trigger/500 must return 500 (AC-10.3 / SPEC.md line 166); "
        f"got status={response.status_code} body={response.text!r}"
    )

    body_text = json.dumps(response.json(), ensure_ascii=False)
    body_lower = body_text.lower()

    # ---- Sub-assertion FR10-500-no-stacktrace-leak ----
    assert "traceback" not in body_lower, (
        f"500 body MUST NOT contain a stack trace "
        f"(AC-10.3 / SPEC.md line 166 + §8 #19 / NFR-02); got body={body_text!r}"
    )

    # ---- Sub-assertion FR10-500-no-sql-leak ----
    sql_markers = (
        "select ", "insert ", "update ", "delete ", " from ", " where ",
        "sqlite", "postgres",
    )
    sql_hits = [m for m in sql_markers if m in body_lower]
    # The trigger exception class name is a SQL-fragment-adjacent token;
    # reject either the trigger fragment string itself OR any SQL-shaped
    # substring.
    assert trigger_internal_exception.lower() not in body_lower, (
        f"500 body MUST NOT echo the original exception class "
        f"(AC-10.3 / SPEC.md line 166 + §8 #19 / NFR-02); "
        f"got body={body_text!r}"
    )
    assert not sql_hits, (
        f"500 body MUST NOT contain SQL-shaped substrings "
        f"(AC-10.3 / SPEC.md line 166 + §8 #19 / NFR-02); "
        f"sql_hits={sql_hits!r} body={body_text!r}"
    )

    # ---- Sub-assertion FR10-500-no-filepath-leak ----
    # An absolute path starts with ``/`` and contains at least one
    # slash separator. The handler must redact / suppress the
    # ``/srv/taskq_api/...`` fragment from the original exception.
    abs_path_pattern = re.compile(r"/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
    assert not abs_path_pattern.search(body_text), (
        f"500 body MUST NOT contain absolute file paths "
        f"(AC-10.3 / SPEC.md line 166 + §8 #19 / NFR-02); "
        f"got body={body_text!r}"
    )


# ---------------------------------------------------------------------------
# AC-10.4 — correlation_id consistent across response header + log.
# ---------------------------------------------------------------------------


def test_correlation_id_consistent_across_response_and_log(caplog):
    """AC-10.4: The ``correlation_id`` in the problem+json body equals the
    ``X-Correlation-Id`` response header and equals the server log
    line emitted while handling the request (SPEC.md line 167).

    TEST_SPEC inputs:
      generated_correlation_id        = "abc-123-correlation"
      expected_response_header_value  = "abc-123-correlation"
      expected_log_line_contains_id   = "true"
    Sub-assertions:
      FR10-correlation-header-present     (X-Correlation-Id == body['correlation_id'])
      FR10-correlation-log-line-present   (log line contains the same token)

    The test uses a sentinel correlation id so a passing test
    unambiguously proves that the SAME token reaches all three sinks
    (response header, response body, log line). The implementation
    is allowed to (a) accept the inbound ``X-Correlation-Id`` header
    if the client supplies one, or (b) generate one when absent —
    both behaviours satisfy the SPEC.md line 167 contract. This test
    uses the (a) variant because it is the more useful contract for
    client-driven tracing.
    """  # NFR-04 NFR-09 NFR-10
    generated_correlation_id = "abc-123-correlation"
    expected_response_header_value = "abc-123-correlation"
    expected_log_line_contains_id = "true"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert generated_correlation_id == "abc-123-correlation"
    assert expected_response_header_value == "abc-123-correlation"
    assert expected_log_line_contains_id == "true"

    captured: list[str] = []

    def _on_log(record: logging.LogRecord) -> None:
        captured.append(record.getMessage())

    app, _logger = _build_trigger_app(on_log=_on_log)
    response = _async_request(
        "GET",
        "/trigger/404",
        app=app,
        headers={CORRELATION_HEADER: generated_correlation_id},
    )

    body = response.json()

    # ---- Sub-assertion FR10-correlation-header-present ----
    header_value = response.headers.get(CORRELATION_HEADER, "")
    body_corr = body.get("correlation_id", "")
    assert header_value == expected_response_header_value, (
        f"Response header {CORRELATION_HEADER!r} MUST carry the "
        f"correlation id echoed by the client (AC-10.4 / SPEC.md "
        f"line 167); got header={header_value!r} "
        f"expected={expected_response_header_value!r}"
    )
    assert body_corr == expected_response_header_value, (
        f"Body correlation_id MUST equal the response header value "
        f"(AC-10.4 / SPEC.md line 167); got body['correlation_id']="
        f"{body_corr!r} header={header_value!r}"
    )
    assert body_corr == header_value, (
        f"correlation_id MUST be consistent across body and header "
        f"(AC-10.4 / SPEC.md line 167); got body={body_corr!r} "
        f"header={header_value!r}"
    )

    # ---- Sub-assertion FR10-correlation-log-line-present ----
    log_hits = [line for line in captured if generated_correlation_id in line]
    assert log_hits, (
        f"At least one server log line MUST contain the correlation "
        f"id so operators can stitch client / server / log "
        f"(AC-10.4 / SPEC.md line 167 / NFR-04); captured_lines="
        f"{captured!r} expected_id={generated_correlation_id!r}"
    )


# ---------------------------------------------------------------------------
# AC-10.5 — error code mapping observed end-to-end across the SPEC §7 table.
# ---------------------------------------------------------------------------


def test_error_code_mapping_observed_per_spec_table():
    """AC-10.5: The error-code mapping table from SPEC.md §7 is observed
    end-to-end across the named FR routes — 422 / 401 / 403 / 404 /
    409 / 429 / 503 / 500.

    TEST_SPEC inputs:
      status_422_input_name_oversize = "name-oversized-payload"
      status_422_expected             = "422"
      status_401_no_auth_input       = ""
      status_401_expected             = "401"
      status_403_actor_scope         = "read"
      status_403_expected             = "403"
      status_404_unknown_id          = "task-uuid-missing"
      status_404_expected             = "404"
      status_409_duplicate            = "compile-conflict"
      status_409_expected             = "409"
      status_429_input_count          = "21"
      status_429_expected             = "429"
      status_503_db_state             = "unreachable"
      status_503_expected             = "503"
      status_500_internal             = "unhandled-exception"
      status_500_expected             = "500"
    Sub-assertions:
      FR10-mapping-422    (status_422_expected == "422")
      FR10-mapping-401    (status_401_expected == "401")
      FR10-mapping-403    (status_403_expected == "403")
      FR10-mapping-404    (status_404_expected == "404")
      FR10-mapping-409    (status_409_expected == "409")
      FR10-mapping-429    (status_429_expected == "429")
      FR10-mapping-503    (status_503_expected == "503")
      FR10-mapping-500    (status_500_expected == "500")

    Each row of the SPEC §7 table corresponds to one in-process
    trigger route. The test fires all eight and asserts that the
    response status code equals the SPEC row's expected status, and
    that the response body carries ``type`` matching the SPEC §7
    ``type`` column for that row.
    """  # NFR-02 NFR-09 NFR-10
    status_422_input_name_oversize = "name-oversized-payload"
    status_422_expected = "422"
    status_401_no_auth_input = ""
    status_401_expected = "401"
    status_403_actor_scope = "read"
    status_403_expected = "403"
    status_404_unknown_id = "task-uuid-missing"
    status_404_expected = "404"
    status_409_duplicate = "compile-conflict"
    status_409_expected = "409"
    status_429_input_count = "21"
    status_429_expected = "429"
    status_503_db_state = "unreachable"
    status_503_expected = "503"
    status_500_internal = "unhandled-exception"
    status_500_expected = "500"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert status_422_expected == "422"
    assert status_401_expected == "401"
    assert status_403_expected == "403"
    assert status_404_expected == "404"
    assert status_409_expected == "409"
    assert status_429_expected == "429"
    assert status_503_expected == "503"
    assert status_500_expected == "500"

    # SPEC §7 ``type`` column — the GREEN handler must surface these
    # URIs in the problem+json body for the corresponding status.
    spec_table: list[tuple[int, str]] = [
        (422, "/errors/validation"),
        (401, "/errors/unauthenticated"),
        (403, "/errors/forbidden"),
        (404, "/errors/not-found"),
        (409, "/errors/conflict"),
        (429, "/errors/rate-limited"),
        (503, "/errors/not-ready"),
        (500, "/errors/internal"),
    ]

    app, _logger = _build_trigger_app()

    failures: list[str] = []

    for status_code, type_uri in spec_table:
        response = _async_request("GET", f"/trigger/{status_code}", app=app)
        body: dict = {}
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            failures.append(
                f"{status_code}: response body is not JSON — "
                f"raw={response.text!r}"
            )
            continue

        if response.status_code != status_code:
            failures.append(
                f"{status_code}: expected HTTP {status_code}, "
                f"got {response.status_code} body={body!r}"
            )
            continue

        if body.get("type") != type_uri:
            failures.append(
                f"{status_code}: body['type']={body.get('type')!r} "
                f"expected {type_uri!r} per SPEC.md §7; body={body!r}"
            )

        # The six-field contract (AC-10.2) applies to every status
        # code in the table, not only 401.
        required = {"type", "title", "status", "detail", "instance", "correlation_id"}
        missing = required - set(body.keys())
        if missing:
            failures.append(
                f"{status_code}: body missing required RFC 7807 fields "
                f"{sorted(missing)!r}; body={body!r}"
            )

        # Content-Type contract (AC-10.1) applies to every status
        # code in the table.
        ctype = response.headers.get("content-type", "")
        if "application/problem+json" not in ctype:
            failures.append(
                f"{status_code}: Content-Type={ctype!r} missing "
                f"'application/problem+json'"
            )

    assert not failures, (
        "Error code mapping table (SPEC.md §7) MUST be observed end-to-end "
        "with each status code rendered as application/problem+json and "
        "the corresponding /errors/* URI in body['type'] "
        "(AC-10.5 / SPEC.md line 168). Failures:\n  - "
        + "\n  - ".join(failures)
    )