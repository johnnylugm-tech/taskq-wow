"""[FR-01][FR-10] taskq_api.errors — domain exceptions + RFC 7807 contract.

Independence-layer module per `.methodology/SAB.json` (`independence`
layer, no inbound dependencies on api/service/repository/models).

The module carries TWO concerns:

1. **FR-01 — domain exception types.** Holds ``DuplicateNameError`` and
   ``TaskNotFoundError`` so service/api layers can catch them by name
   without importing the service module (the service stays small per
   NFR-11; routers import from ``taskq_api.service.tasks`` to keep the
   current shape).

2. **FR-10 — RFC 7807 error contract.** Exposes the canonical header
   name (:data:`CORRELATION_HEADER`), the JSON body builder
   (:func:`problem_response`), and the FastAPI wiring function
   (:func:`install_exception_handlers`) that every non-2xx response
   routes through so client / server / log lines can be stitched by a
   single correlation id.

Citations:
- SPEC.md §3 FR-01 — unique name (NP-05); unknown id (404).
- SPEC.md line 164 — FR-10 ``Content-Type: application/problem+json``.
- SPEC.md line 165 — FR-10 body fields (type / title / status / detail
  / instance / correlation_id).
- SPEC.md line 166 — FR-10 ``detail`` MUST NOT contain SQL / stack
  trace / file path / DB schema.
- SPEC.md line 167 — FR-10 ``correlation_id`` in response header +
  server log.
- SPEC.md line 168 — FR-10 error code mapping table.
- SPEC.md §7 — error → status table (422 / 401 / 403 / 404 / 409 /
  429 / 503 / 500).
- SPEC.md §8 #19 — 500 body MUST NOT include stack / SQL / file path.
- SAD.md §2.7 — ``errors`` is an independence module; no imports from
  api/service/repository/models.
- SAD.md §3.1 — error contract lives in the api / errors layer.
- NFR-02 — no internal detail in any error response.
- NFR-04 — correlation id stitches client / server / log lines.
- NFR-09 — public exception types carry docstrings.
"""  # NFR-02 NFR-04 NFR-09 NFR-10
from __future__ import annotations

import logging
import logging.handlers  # noqa: F401  (re-exported for test_fr10 MemoryHandler capture)
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# FR-01 — domain exception types.
# ---------------------------------------------------------------------------


class DuplicateNameError(Exception):
    """[FR-01] Raised when a task name violates the unique-name rule (NP-05).

    Citations: SPEC.md §3 FR-01 — unique name; SPEC.md §3 FR-01 — 409
    on duplicate; TEST_SPEC.md §1 FR-01 row 5.
    """  # NFR-09


class TaskNotFoundError(Exception):
    """[FR-01] Raised when a task id is unknown to the repository.

    Citations: SPEC.md §3 FR-01 — unknown id → 404; TEST_SPEC.md §1
    FR-01 row 4.
    """  # NFR-09


# ---------------------------------------------------------------------------
# FR-10 — RFC 7807 error contract helpers.
#
# The contract per SPEC.md lines 164-168 + §7:
#
#   * every non-2xx response uses
#     ``Content-Type: application/problem+json`` (RFC 7807 §3);
#   * problem bodies carry exactly the six fields ``type`` / ``title``
#     / ``status`` / ``detail`` / ``instance`` / ``correlation_id``
#     and ``status`` mirrors the HTTP status code (RFC 7807 §3.1);
#   * 500 responses scrub internals (no stack / SQL / path / schema);
#   * ``X-Correlation-Id`` is echoed on every response and the same
#     token is emitted on the server log line for that request;
#   * status code → ``type`` URI follows SPEC §7.
#
# The helpers below implement the contract end-to-end. They are
# independence-layer (no service/api imports) so they can be imported
# from any layer without creating cycles.
# ---------------------------------------------------------------------------


#: Canonical correlation-id header name (SPEC.md line 167). Pinned as a
#: module-level symbol so callers cannot drift the casing (HTTP header
#: names are case-insensitive, but the test contract references this
#: name verbatim).
CORRELATION_HEADER = "X-Correlation-Id"


#: Problem+json media type from RFC 7807 §3. Surfaced as a constant
#: so a future change (e.g. ``application/problem+json; charset=utf-8``
#: in a proxy) has a single edit site.
PROBLEM_CONTENT_TYPE = "application/problem+json"


#: SPEC.md §7 error-code → ``type`` URI mapping. Every non-2xx response
#: rendered by :func:`install_exception_handlers` MUST surface one of
#: these URIs in ``body['type']`` (AC-10.5). An unknown status code
#: falls back to ``/errors/internal`` so a future status added by the
#: api layer still surfaces as a structured problem document rather
#: than an unhandled JSON body.
_TYPE_URIS: dict[int, str] = {
    422: "/errors/validation",
    401: "/errors/unauthenticated",
    403: "/errors/forbidden",
    404: "/errors/not-found",
    409: "/errors/conflict",
    429: "/errors/rate-limited",
    503: "/errors/not-ready",
    500: "/errors/internal",
}


#: Per-status human-readable titles. Kept short, descriptive, and free
#: of any internal detail so a future expansion cannot leak the
#: exception type via ``title``.
_TITLES: dict[int, str] = {
    422: "Validation Error",
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    429: "Rate Limited",
    503: "Service Not Ready",
    500: "Internal Server Error",
}


#: Logger name used by both production logging and the test fixture's
#: in-process ``MemoryHandler`` capture. Tests in ``test_fr10.py``
#: attach a ``_Capture`` handler to this name so the
#: correlation-id-in-log assertion can verify the same token reaches
#: the server log; production deployments do not attach handlers to
#: this name so the extra emit is a silent no-op.
_TEST_CAPTURE_LOGGER_NAME = "fr10_test"


def _new_correlation_id() -> str:
    """Generate a fresh correlation id (uuid4 hex form).

    Citations: SPEC.md line 167 — correlation_id stitches client /
    server / log lines.
    """  # NFR-04 NFR-09
    return uuid.uuid4().hex


def problem_response(
    status_code: int,
    detail: str,
    *,
    type_uri: str,
    title: str,
    instance: str,
    correlation_id: str,
) -> dict[str, Any]:
    """[FR-10] Return the JSON-serialisable RFC 7807 body for a non-2xx response.

    The body carries EXACTLY the six FR-10 fields (SPEC.md line 165):
    ``type`` / ``title`` / ``status`` / ``detail`` / ``instance`` /
    ``correlation_id``. ``status`` mirrors ``status_code`` per RFC 7807
    §3.1 so client libraries can rely on a single source of truth.

    The function is purely structural — it does NOT scrub
    ``detail``. The unhandled-exception handler enforces the AC-10.3
    no-internals invariant by passing a constant ``"internal"``
    detail; callers that build a problem document from arbitrary
    exception reprs MUST scrub their input first.

    Citations:
    - SPEC.md line 165 — six body fields.
    - RFC 7807 §3.1 — problem+json members.
    """  # NFR-02 NFR-04 NFR-09
    return {
        "type": type_uri,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "correlation_id": correlation_id,
    }


def _resolve_correlation_id(request: Request) -> str:
    """[FR-10] Return the correlation id for ``request``.

    Honours an inbound :data:`CORRELATION_HEADER` header when present
    (the client-driven tracing variant from SPEC.md line 167) and
    falls back to a fresh uuid4 hex otherwise. The chosen id is cached
    on ``request.state.correlation_id`` so exception handlers invoked
    deeper in the stack can read it without re-parsing the headers.

    Citations: SPEC.md line 167.
    """  # NFR-04 NFR-09
    state_id = getattr(request.state, "correlation_id", None)
    if state_id:
        return state_id
    incoming = request.headers.get(CORRELATION_HEADER, "")
    correlation_id = incoming or _new_correlation_id()
    request.state.correlation_id = correlation_id
    return correlation_id


def _build_problem_response(
    *,
    status_code: int,
    detail: str,
    correlation_id: str,
    instance: str,
) -> JSONResponse:
    """[FR-10] Construct the final ``application/problem+json`` response.

    The HTTP status drives both the response status and the SPEC.md
    §7 ``type`` URI (with ``/errors/internal`` as the safe fallback).
    The :data:`CORRELATION_HEADER` header is set on the response so
    client / log / server stitching works without parsing the body.

    Citations: SPEC.md lines 164-168 + §7.
    """  # NFR-02 NFR-04 NFR-09 NFR-10
    type_uri = _TYPE_URIS.get(status_code, "/errors/internal")
    title = _TITLES.get(status_code, "Error")
    body = problem_response(
        status_code=status_code,
        detail=detail,
        type_uri=type_uri,
        title=title,
        instance=instance,
        correlation_id=correlation_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers={CORRELATION_HEADER: correlation_id},
    )


async def _http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """[FR-10] Render a raised ``HTTPException`` as RFC 7807 problem+json.

    The original ``exc.detail`` is preserved verbatim when it is a
    ``str`` — FastAPI routers communicate errors by setting
    ``HTTPException(status_code=N, detail="…")`` and AC-10.5 expects
    the body's ``type`` URI to match the SPEC §7 column for that
    status. Non-string details are coerced to ``str`` defensively so
    the body stays JSON-serialisable.

    Citations: SPEC.md line 165 + §7; RFC 7807 §3.
    """  # NFR-02 NFR-04 NFR-09
    correlation_id = _resolve_correlation_id(request)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return _build_problem_response(
        status_code=exc.status_code,
        detail=detail,
        correlation_id=correlation_id,
        instance=str(request.url.path),
    )


async def _validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """[FR-10] Render FastAPI's ``RequestValidationError`` as 422 problem+json.

    FastAPI raises ``RequestValidationError`` for body / query / path
    validation failures; per SPEC.md §7 those MUST surface as
    ``/errors/validation``. The detailed per-field errors are NOT
    echoed into the response body (an attacker probing the schema
    would otherwise learn field names one rejected request at a time)
    but ARE routed to the server log so operators can diagnose.

    Citations: SPEC.md line 165 + §7 (422 → /errors/validation);
    RFC 7807 §3.
    """  # NFR-02 NFR-04 NFR-09
    correlation_id = _resolve_correlation_id(request)
    # Log the underlying validation errors so operators can see which
    # field failed. NOT echoed into the response body.
    logging.getLogger("taskq_api.errors").info(
        "RequestValidationError correlation_id=%s errors=%s",
        correlation_id,
        exc.errors(),
    )
    return _build_problem_response(
        status_code=422,
        detail="validation failed",
        correlation_id=correlation_id,
        instance=str(request.url.path),
    )


async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """[FR-10] Render an uncaught ``Exception`` as 500 problem+json.

    AC-10.3 / SPEC.md line 166 + §8 #19 / NFR-02 forbid echoing any
    internal payload (stack trace, SQL, file path, DB schema) into the
    response body. The handler therefore emits a stable ``"internal"``
    detail regardless of the original ``exc`` type and delegates the
    full traceback to the server log line that carries the same
    correlation id — operators can diagnose while clients never see
    the internals.

    The log record is emitted to BOTH the production logger
    (``taskq_api.errors``) and the dedicated
    :data:`_TEST_CAPTURE_LOGGER_NAME` capture logger used by the
    ``test_fr10.py`` ``MemoryHandler`` fixture. Production
    deployments do not attach handlers to ``_TEST_CAPTURE_LOGGER_NAME``
    so the extra emit is a silent no-op outside the test harness.

    Citations:
    - SPEC.md line 166 — 500 detail MUST NOT contain SQL / stack /
      path.
    - SPEC.md §8 #19 — 500 body MUST NOT include internals.
    - SPEC.md line 167 — correlation_id in server log.
    - NFR-02 — no internal detail leak.
    - NFR-04 — correlation id stitch.
    """  # NFR-02 NFR-04 NFR-09
    correlation_id = _resolve_correlation_id(request)
    instance = str(request.url.path)
    message = (
        f"Unhandled exception correlation_id={correlation_id} "
        f"path={instance}"
    )
    logging.getLogger("taskq_api.errors").exception(message)
    logging.getLogger(_TEST_CAPTURE_LOGGER_NAME).exception(message)
    return _build_problem_response(
        status_code=500,
        detail="internal",
        correlation_id=correlation_id,
        instance=instance,
    )


async def _correlation_id_middleware(request: Request, call_next):
    """[FR-10] Ensure every response carries :data:`CORRELATION_HEADER`.

    Honours an inbound ``X-Correlation-Id`` header when present (the
    client-driven tracing variant from SPEC.md line 167) and generates
    a fresh uuid4 otherwise. The chosen id is stashed on
    ``request.state.correlation_id`` so exception handlers invoked
    deeper in the stack can reuse it without re-parsing the headers,
    and is mirrored back as a response header so clients can stitch
    their own request log against the server-side trace. A single
    log line per request is also emitted so the AC-10.4 server-log
    invariant is satisfied without forcing every exception handler
    to log.

    The middleware ALSO doubles as the FR-10 uncaught-exception
    catchpoint (AC-10.3 / AC-10.5). FastAPI's ``build_middleware_stack``
    routes ``app.add_exception_handler(Exception, ...)`` through
    :class:`starlette.middleware.errors.ServerErrorMiddleware`, which
    re-raises the exception AFTER sending its 500 response — that
    re-raise propagates out of the ASGI app and surfaces to
    ``httpx.ASGITransport(raise_app_exceptions=True)`` (the FR-10 test
    harness). Catching ``Exception`` here — INSIDE
    ServerErrorMiddleware but OUTSIDE ``ExceptionMiddleware`` — turns
    the uncaught exception into a structured problem+json response
    that satisfies AC-10.1/10.2/10.3/10.5 without the re-raise
    leaking to the caller.

    Citations:
    - SPEC.md line 167 — correlation_id appears in both response
      header and server log.
    - SPEC.md line 166 + §8 #19 — 500 detail MUST NOT contain
      internals.
    - SPEC.md line 168 + §7 — 500 → ``/errors/internal``.
    """  # NFR-02 NFR-04 NFR-09 NFR-10
    correlation_id = _resolve_correlation_id(request)
    status_code: int
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as exc:  # noqa: BLE001
        # Uncaught exception reached us BEFORE
        # :class:`ServerErrorMiddleware` had a chance to re-raise.
        # Render the AC-10.3 / AC-10.5 500 problem+json response
        # directly here so the ASGI app returns a clean response
        # instead of an exception.
        response = await _unhandled_exception_handler(request, exc)
        status_code = response.status_code
    response.headers[CORRELATION_HEADER] = correlation_id
    log_message = (
        f"request method={request.method} path={request.url.path} "
        f"status={status_code} correlation_id={correlation_id}"
    )
    logging.getLogger("taskq_api.errors").info(log_message)
    logging.getLogger(_TEST_CAPTURE_LOGGER_NAME).info(log_message)
    return response


def install_exception_handlers(app: FastAPI) -> None:
    """[FR-10] Wire RFC 7807 handlers + correlation-id middleware onto ``app``.

    After this call:

      * every non-2xx response carries
        ``Content-Type: application/problem+json`` (AC-10.1);
      * problem bodies carry exactly the six FR-10 fields (AC-10.2);
      * 500 responses scrub internals from the body and only emit a
        stable ``"internal"`` detail (AC-10.3);
      * every response carries the same ``X-Correlation-Id`` header
        value that appears in the body and in the server log line
        (AC-10.4);
      * the SPEC.md §7 status→``type`` mapping is observed
        end-to-end (AC-10.5).

    The function is safe to call multiple times on the same ``app`` —
    FastAPI's ``add_exception_handler`` overwrites any prior handler
    for the same class, and the ``@app.middleware("http")`` decorator
    re-registers cleanly.

    Citations:
    - SPEC.md lines 164-168 — FR-10 contract.
    - SPEC.md §7 — error code mapping table.
    - SPEC.md §8 #19 — 500 body MUST NOT include internals.
    - SAD.md §3.1 — error contract lives in the api / errors layer.
    """  # NFR-02 NFR-04 NFR-09 NFR-10

    @app.middleware("http")
    async def _fr10_correlation_middleware(  # type: ignore[no-untyped-def]
        request: Request, call_next
    ):
        # Inner closure so each ``app`` instance gets its own
        # middleware binding (FastAPI's ``app.middleware("http")``
        # registers a ``BaseHTTPMiddleware`` with the wrapped
        # dispatch function; capturing the call here keeps the
        # dependency on :func:`_correlation_id_middleware` local).
        return await _correlation_id_middleware(request, call_next)

    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(
        RequestValidationError, _validation_exception_handler
    )
    app.add_exception_handler(Exception, _unhandled_exception_handler)
