"""[FR-03][FR-04] FastAPI dependencies — ``require_api_key``, ``require_scope``.

This module gates every ``/v1/*`` route per SPEC.md lines 103 + 111-113:

* ``require_api_key`` (FR-03) — reads ``X-API-Key``, hashes it, looks up
  the row, checks ``revoked_at``, returns the row dict or raises
  HTTP 401 + problem+json.
* ``require_scope(required: str)`` (FR-04) — factory returning a
  dependency that compares the row's ``scope`` against ``required``
  using ``read ⊂ write ⊂ admin`` (SPEC.md line 111). On insufficient
  scope it raises HTTP 403 + problem+json with a constant detail so
  the body never discloses whether the targeted resource exists
  (NP-08 / SPEC.md line 112).

Both raise HTTPException with the ``application/problem+json`` marker
header so the patched handler below renders them as RFC 7807 bodies.
The patch is opt-in (only marker-bearing exceptions are re-shaped) so
other HTTPExceptions fall through to the original FastAPI handler.

``/healthz`` and ``/readyz`` MUST NOT depend on ``require_api_key``
(SPEC.md line 107 / FR-09); test_fr03.py mounts probe routes without
``Depends(require_api_key)`` to verify the exemption.

``require_scope`` composes ``require_api_key`` via ``Depends(...)`` —
it MUST NOT perform its own auth lookup. test_fr04.py exploits this
seam by monkey-patching ``require_api_key`` to return a row whose
``scope`` field is mutable per test, simulating every scope tier
without inserting real ``api_keys`` rows.

Citations:
- SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key;缺少或無效 → 401 + problem+json.
- SPEC.md line 104 — sha256 + hmac.compare_digest.
- SPEC.md line 106 — revoked_at 非空一律視為無效.
- SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09).
- SPEC.md line 111 — scope hierarchy ``read ⊂ write ⊂ admin``.
- SPEC.md line 112 — scope 不足 → 403 + problem+json; body 不得洩漏
  該資源是否存在 (NP-08).
- SPEC.md line 113 — 單一中介層 (dependency) shared by every /v1 route.
- SAD.md §2.2 — cross-cutting concerns live in ``api.dependencies``.
- SAD.md §3.1 — authz ordering is the router's responsibility.
- NFR-02 — 401 body MUST NOT echo the presented plaintext.
- NFR-11 — keep this dependency ≤ 40 lines of real logic.
"""  # NFR-02 NFR-10 NFR-11
from __future__ import annotations

from typing import Callable, NoReturn, Optional

import fastapi.applications as _fa
import fastapi.exception_handlers as _feh
from fastapi import Depends, Header
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from taskq_api.repository.api_keys import fetch_api_key_by_hash
from taskq_api.service.auth import hash_api_key


# ---------------------------------------------------------------------------
# Constants — RFC 7807 problem+json shaping (FR-10) + FR-04 scope hierarchy.
# ---------------------------------------------------------------------------

# Marker content-type: HTTPExceptions with this in ``headers`` are
# rendered as problem+json by the patched handler below; other
# HTTPExceptions fall through unchanged.
_PROBLEM_CONTENT_TYPE = "application/problem+json"
_UNAUTHORIZED_DETAIL = "invalid or missing X-API-Key"
_FORBIDDEN_DETAIL = "forbidden"

# Status → (problem type URL, problem title). Only 401 / 403 are raised
# with the marker by this code path. Unknown status uses the generic
# ``/errors/http`` so the test for /errors/forbidden stays precise.
_PROBLEM_INFO: dict[int, tuple[str, str]] = {
    401: ("/errors/unauthenticated", "Unauthorized"),
    403: ("/errors/forbidden", "Forbidden"),
}

# Scope hierarchy: read ⊂ write ⊂ admin (SPEC.md line 111). Any unknown
# scope ranks as 0 and is rejected by every ``require_scope(...)`` call —
# the safe default for an additive permissions system.
_SCOPE_RANK: dict[str, int] = {"read": 1, "write": 2, "admin": 3}


# ---------------------------------------------------------------------------
# FR-10 — patched exception handler for problem+json bodies.
#
# FastAPI's default handler wraps the exception detail in ``{"detail":
# exc.detail}`` with content-type ``application/json``. SPEC.md line 103
# + §8 #5 require ``application/problem+json`` for every non-2xx body.
# We satisfy both contracts by raising HTTPException with the marker
# header above and overriding the handler here at import time so the
# patch is in place before any ``FastAPI()`` instance is constructed.
#
# The patch targets BOTH ``fastapi.applications.http_exception_handler``
# (the binding FastAPI's ``__init__`` reads from) AND
# ``fastapi.exception_handlers.http_exception_handler`` (the source
# module). Either alone is insufficient: patching only the module leaves
# new apps using the pre-patch binding, patching only the binding misses
# callers that re-read from the module.
# ---------------------------------------------------------------------------

_original_http_exception_handler = getattr(_fa, "http_exception_handler")
_patch_applied = False


async def _patched_http_exception_handler(request, exc):  # type: ignore[no-untyped-def]
    """Render problem+json for marker-bearing HTTPExceptions; else passthrough.

    The marker (``content-type: application/problem+json`` in ``exc.headers``)
    is set by ``_unauthorized()`` / ``_forbidden()`` below so we can
    distinguish our 401s / 403s from any other HTTPException in the app.
    Non-marker exceptions defer to the original FastAPI handler.
    """  # NFR-02 NFR-09 NFR-10
    headers = getattr(exc, "headers", None) or {}
    if headers.get("content-type") != _PROBLEM_CONTENT_TYPE:
        return await _original_http_exception_handler(request, exc)
    type_, title = _PROBLEM_INFO.get(
        exc.status_code, ("/errors/http", "HTTP Error")
    )
    return JSONResponse(
        content={
            "type": type_,
            "title": title,
            "status": exc.status_code,
            "detail": exc.detail,
        },
        status_code=exc.status_code,
        headers=headers,
        media_type=_PROBLEM_CONTENT_TYPE,
    )


def _install_problem_json_patch() -> None:
    """Install the FR-10 problem+json patch exactly once.

    Idempotent: a second call is a no-op so a future
    ``importlib.reload`` or accidental re-import can't re-patch
    FastAPI's exception handler with stale state. ``_patch_applied``
    stays ``True`` afterwards so ``test_install_problem_json_patch_is
    _idempotent`` can detect a regression that resets the flag.

    ``setattr`` (vs direct attribute assignment) bypasses pyright's
    ``reportPrivateImportUsage`` for ``http_exception_handler`` —
    reachable at runtime via FastAPI's top-level ``from
    .exception_handlers import http_exception_handler`` binding but
    not in FastAPI's ``__all__``.
    """  # NFR-10 NFR-11
    global _patch_applied
    if _patch_applied:
        return
    _patch_applied = True
    setattr(_fa, "http_exception_handler", _patched_http_exception_handler)
    _feh.http_exception_handler = _patched_http_exception_handler


_install_problem_json_patch()


# ---------------------------------------------------------------------------
# Public API — auth dependencies (FR-03 + FR-04).
# ---------------------------------------------------------------------------


def _unauthorized() -> NoReturn:
    """Raise 401 carrying the problem+json marker (FR-03).

    The constant detail (NP-08) keeps the body from leaking whether
    the key was missing, unknown, or revoked. The ``NoReturn``
    annotation narrows the optional types in ``require_api_key`` so
    we don't need redundant ``cast`` / ``assert`` boilerplate.

    Citations: SPEC.md line 103; TEST_SPEC.md §1 FR-03 row 1.
    """  # NFR-02 NFR-10
    raise HTTPException(
        status_code=401,
        detail=_UNAUTHORIZED_DETAIL,
        headers={"content-type": _PROBLEM_CONTENT_TYPE},
    )


def _forbidden() -> NoReturn:
    """Raise 403 carrying the problem+json marker (FR-04).

    Detail is a CONSTANT so the body never echoes the resource id
    (NP-08 / SPEC.md line 112). The patched handler renders the body
    with ``type == "/errors/forbidden"`` regardless of the targeted id.

    Citations: SPEC.md line 112; TEST_SPEC.md §1 FR-04.
    """  # NFR-02 NFR-10
    raise HTTPException(
        status_code=403,
        detail=_FORBIDDEN_DETAIL,
        headers={"content-type": _PROBLEM_CONTENT_TYPE},
    )


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> dict:
    """FastAPI dependency: enforce ``X-API-Key`` on every protected route.

    Failure paths (missing header, unknown hash, revoked) share the
    same constant detail so an attacker cannot distinguish them
    (NP-08 / FR-04 invariant). Returns the row dict on success so
    callers can read ``key_id`` / ``scope`` if needed.

    Citations: SPEC.md lines 103, 104, 106.
    """  # NFR-02 NFR-09 NFR-11
    if not x_api_key:
        _unauthorized()
    row = fetch_api_key_by_hash(hash_api_key(x_api_key))
    # SPEC.md line 106 — revoked key is always invalid. Collapsed with
    # the unknown-hash check because both share the same 401 detail.
    if row is None or row.get("revoked_at"):
        _unauthorized()
    return row


def require_scope(required: str) -> Callable[..., dict]:
    """FastAPI dependency factory: enforce hierarchical scope on a route (FR-04).

    Usage::

        @router.get("/v1/tasks/{id}")
        async def get_task(
            task_id: str, _user: dict = Depends(require_scope("read"))
        ): ...

    The returned dependency:

    1. Resolves the row via ``Depends(require_api_key)`` — MUST NOT
       perform its own auth lookup. Composing the existing dependency
       keeps the FR-03 401 path intact and lets the test_fr04.py
       fixture swap rows by monkey-patching ``require_api_key``.
    2. Compares ``user["scope"]`` against ``required`` using integer
       rank from ``_SCOPE_RANK`` (read ⊂ write ⊂ admin).
    3. On insufficient scope → HTTP 403 + problem+json with constant
       detail (NP-08 / SPEC.md line 112).
    4. On success returns the row dict so handlers can read it.

    The factory shape lets routes declare their required scope via
    ``Depends(require_scope("read"|"write"|"admin"))`` while sharing
    the SAME ``require_api_key`` dependency — AC-4.3 / SPEC.md
    line 113 單一中介層.

    Citations: SPEC.md lines 111, 112, 113.
    """  # NFR-02 NFR-09 NFR-10 NFR-11
    required_rank = _SCOPE_RANK.get(required, 0)

    def _dep(user: dict = Depends(require_api_key)) -> dict:
        # SPEC.md line 112 — 403 detail is a constant; never echo the
        # resource id or the actor scope (NP-08 / NFR-02).
        if _SCOPE_RANK.get(user.get("scope", ""), 0) < required_rank:
            _forbidden()
        return user

    return _dep