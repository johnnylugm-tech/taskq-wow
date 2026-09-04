"""[FR-03] FastAPI dependencies — ``require_api_key``.

This module exposes the single dependency ``require_api_key`` that gates
every ``/v1/*`` route per SPEC.md line 103. It is intentionally small
(reads the ``X-API-Key`` header, hashes it, looks up the row, checks
``revoked_at``, and either returns the row dict or raises an HTTP 401).

Routes under ``/healthz`` and ``/readyz`` MUST NOT depend on this
function (SPEC.md line 107 / FR-09); the test app in
``test_fr03.py`` verifies the exemption by mounting probe routes
without ``Depends(require_api_key)``.

FR-10 / problem+json trick
--------------------------

FastAPI's default ``http_exception_handler`` wraps the exception detail
in ``{"detail": exc.detail}`` and uses ``application/json``. SPEC.md
line 103 + §8 #5 require ``application/problem+json`` with ``type ==
"/errors/unauthenticated"`` for the 401 body. We satisfy both
contracts by:

1. Raising ``HTTPException(status_code=401, ..., headers={
   "content-type": "application/problem+json"})`` — this overrides the
   default content-type at the Starlette layer (the content-type header
   on the response is taken from ``exc.headers`` before JSONResponse's
   ``media_type="application/json"`` overwrites it).
2. Monkey-patching ``fastapi.exception_handlers.http_exception_handler``
   at import time so that any 401 carrying our marker is rendered as a
   full RFC 7807 body (``type / title / status / detail``). The patch is
   strictly opt-in via the marker header — all other HTTPExceptions fall
   through to the original handler, so the rest of the codebase is
   unaffected.

The patch lives in this module so it is applied exactly once, at the
point the dependency module is first imported (the FR-03 tests import
it eagerly at module top).

Citations:
- SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key;缺少或無效 → HTTP 401 +
  problem+json.
- SPEC.md line 104 — sha256 + hmac.compare_digest (used indirectly via
  ``service.auth.compare_api_keys``).
- SPEC.md line 106 — revoked_at 非空一律視為無效.
- SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09).
- SAD.md §2.2 — api layer holds thin route handlers; cross-cutting
  concerns live in ``api.dependencies``.
- NFR-02 — 401 body MUST NOT echo the presented plaintext.
- NFR-11 — keep this dependency ≤ 40 lines of real logic so future
  callers can compose it without surprising side effects.
"""  # NFR-02 NFR-11
from __future__ import annotations

from typing import Optional

import fastapi.applications as _fa
import fastapi.exception_handlers as _feh
from fastapi import Header
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from taskq_api.repository.api_keys import fetch_api_key_by_hash
from taskq_api.service.auth import hash_api_key


# ---------------------------------------------------------------------------
# Constants for problem+json shaping (RFC 7807 / FR-10).
# ---------------------------------------------------------------------------

_PROBLEM_TYPE = "/errors/unauthenticated"
_PROBLEM_TITLE = "Unauthorized"
_PROBLEM_CONTENT_TYPE = "application/problem+json"
_GENERIC_DETAIL = "invalid or missing X-API-Key"


# ---------------------------------------------------------------------------
# Monkey-patch fastapi.exception_handlers.http_exception_handler so that any
# HTTPException carrying the problem+json content-type marker is rendered as
# an RFC 7807 body. The original handler is preserved as the fall-through for
# every other exception, so the rest of the codebase is unaffected.
# ---------------------------------------------------------------------------

# The original FastAPI handler — captured BEFORE we install the FR-03
# patch below, so the patched version can defer to it for any non-401
# HTTPException and leave the rest of the app's behaviour untouched.
_original_http_exception_handler = _fa.http_exception_handler
_patch_applied = False


def _install_problem_json_patch() -> None:
    """Install the FR-03 problem+json patch exactly once.

    ``FastAPI.__init__`` does
    ``self.exception_handlers.setdefault(HTTPException, http_exception_handler)``
    where ``http_exception_handler`` is bound at the top of
    ``fastapi.applications`` via ``from .exception_handlers import
    http_exception_handler``. Patching only
    ``fastapi.exception_handlers.http_exception_handler`` would NOT
    affect new ``FastAPI()`` instances because the binding in
    ``fastapi.applications`` is already resolved. We therefore patch
    both namespaces so that future ``FastAPI()`` instances pick up the
    override, and so that any already-constructed app that re-reads
    from ``fastapi.exception_handlers`` also sees it.

    Citations: SPEC.md line 103 — 401 + problem+json; FR-10 — RFC 7807
    media type on every non-2xx response.
    """  # NFR-10 NFR-11
    global _patch_applied
    if _patch_applied:
        return
    _patch_applied = True

    async def _patched_http_exception_handler(request, exc):  # type: ignore[no-untyped-def]
        """Render problem+json for marker-bearing 401s; otherwise passthrough.

        Marker: ``exc.headers`` contains ``content-type:
        application/problem+json`` — set by ``_unauthorized()`` below so
        we can tell our 401s apart from anyone else's HTTPException.
        The body shape (``type / title / status / detail``) matches the
        rest of the FR-03 contract documented in TEST_SPEC.md §1 rows
        1 and 5.
        """  # NFR-02 NFR-09
        headers = getattr(exc, "headers", None) or {}
        content_type = headers.get("content-type", "")
        if content_type != _PROBLEM_CONTENT_TYPE:
            # Not ours — defer to the original FastAPI handler so the
            # rest of the app's behaviour is unchanged.
            return await _original_http_exception_handler(request, exc)
        body = {
            "type": _PROBLEM_TYPE,
            "title": _PROBLEM_TITLE,
            "status": exc.status_code,
            "detail": exc.detail,
        }
        return JSONResponse(
            content=body,
            status_code=exc.status_code,
            headers=headers,
            media_type=_PROBLEM_CONTENT_TYPE,
        )

    _fa.http_exception_handler = _patched_http_exception_handler
    _feh.http_exception_handler = _patched_http_exception_handler
    # NOTE: FastAPI copies the bound function into ``self.exception_handlers``
    # at ``FastAPI.__init__`` time, so test apps constructed AFTER our
    # import will see the patched version. This is the order we want:
    # test files do ``from taskq_api.api.dependencies import …`` at
    # module top, which imports this module BEFORE they build the
    # ``FastAPI()`` instance.


_install_problem_json_patch()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _unauthorized() -> None:
    """Raise a 401 carrying the problem+json marker.

    The marker header (``content-type: application/problem+json``) is
    what the patched exception handler keys off — it is the only signal
    that distinguishes our 401s from any other HTTPException in the
    app, so the patch is strictly opt-in.

    Citations: SPEC.md line 103 — 缺少或無效 → 401 + problem+json;
    TEST_SPEC.md §1 FR-03 row 1 — type ``/errors/unauthenticated``.
    """  # NFR-02 NFR-10
    raise HTTPException(
        status_code=401,
        detail=_GENERIC_DETAIL,
        headers={"content-type": _PROBLEM_CONTENT_TYPE},
    )


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> dict:
    """FastAPI dependency: enforce ``X-API-Key`` on every protected route.

    Behaviour:

    1. If ``X-API-Key`` is missing or empty → 401 problem+json.
    2. Hash the presented key with SHA-256; if no row in ``api_keys``
       matches → 401 problem+json.
    3. If the row's ``revoked_at`` is non-null → 401 problem+json.
    4. Otherwise return the row dict (so the route can read
       ``key_id`` / ``scope`` if it wants).

    The 401 body NEVER echoes the presented plaintext (NFR-02). All
    failure paths share the same generic detail string so an attacker
    cannot distinguish "missing header" from "unknown hash" from
    "revoked" — NP-08 / FR-04 invariant.

    Citations: SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key;缺少或
    無效 → 401 + problem+json; SPEC.md line 104 — sha256 hash lookup;
    SPEC.md line 106 — revoked_at 非空一律視為無效; TEST_SPEC.md §1
    FR-03 rows 1/5.
    """  # NFR-02 NFR-09 NFR-11
    if not x_api_key:
        _unauthorized()
    row = fetch_api_key_by_hash(hash_api_key(x_api_key))
    if row is None:
        _unauthorized()
    if row.get("revoked_at"):
        # SPEC.md line 106 — revoked key is always invalid.
        _unauthorized()
    return row
