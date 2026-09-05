"""[FR-03][FR-04] FastAPI dependencies — ``require_api_key``,
``require_scope``.

This module exposes two FastAPI dependencies that gate every ``/v1/*``
route per SPEC.md lines 103 + 111-113:

* ``require_api_key`` (FR-03) — reads ``X-API-Key``, hashes it, looks
  up the row, checks ``revoked_at``, and either returns the row dict
  or raises an HTTP 401.
* ``require_scope(required: str)`` (FR-04) — factory returning a
  dependency that compares the authenticated row's ``scope`` against
  ``required`` using the hierarchical inclusion rule
  ``read ⊂ write ⊂ admin`` (SPEC.md line 111). On insufficient scope
  it raises HTTP 403 + problem+json with a constant detail so the
  body never discloses whether the targeted resource exists
  (NP-08 / SPEC.md line 112).

Routes under ``/healthz`` and ``/readyz`` MUST NOT depend on
``require_api_key`` (SPEC.md line 107 / FR-09); the test app in
``test_fr03.py`` verifies the exemption by mounting probe routes
without ``Depends(require_api_key)``.

``require_scope`` composes ``require_api_key`` via ``Depends(...)`` —
it MUST NOT perform its own auth lookup. The fixture in
``test_fr04.py`` exploits this seam by monkey-patching
``require_api_key`` to return a row whose ``scope`` field is mutable
per test, so each test can simulate any scope tier without inserting
real ``api_keys`` rows.

FR-10 / problem+json trick
--------------------------

FastAPI's default ``http_exception_handler`` wraps the exception detail
in ``{"detail": exc.detail}`` and uses ``application/json``. SPEC.md
line 103 + §8 #5 require ``application/problem+json`` for every
non-2xx body. We satisfy both contracts by:

1. Raising ``HTTPException(status_code=..., ..., headers={
   "content-type": "application/problem+json"})`` — this overrides the
   default content-type at the Starlette layer (the content-type header
   on the response is taken from ``exc.headers`` before JSONResponse's
   ``media_type="application/json"`` overwrites it).
2. Monkey-patching ``fastapi.exception_handlers.http_exception_handler``
   at import time so that any HTTPException carrying the marker is
   rendered as a full RFC 7807 body (``type / title / status /
   detail``). The patch is strictly opt-in via the marker header —
   all other HTTPExceptions fall through to the original handler, so
   the rest of the codebase is unaffected.

The patched handler picks the problem type from a status-code map:

  * 401 → ``/errors/unauthenticated`` (FR-03)
  * 403 → ``/errors/forbidden``        (FR-04)

so 403s raised by ``require_scope`` render as the SPEC-canonical
forbidden type instead of the 401 type.

The patch lives in this module so it is applied exactly once, at the
point the dependency module is first imported (the FR-03/FR-04 tests
import it eagerly at module top).

Citations:
- SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key;缺少或無效 → HTTP 401 +
  problem+json.
- SPEC.md line 104 — sha256 + hmac.compare_digest (used indirectly via
  ``service.auth.compare_api_keys``).
- SPEC.md line 106 — revoked_at 非空一律視為無效.
- SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09).
- SPEC.md line 111 — scope hierarchy ``read ⊂ write ⊂ admin``.
- SPEC.md line 112 — scope 不足 → 403 + problem+json; body 不得洩漏
  該資源是否存在 (NP-08).
- SPEC.md line 113 — 單一中介層 (dependency) shared by every /v1 route.
- SAD.md §2.2 — api layer holds thin route handlers; cross-cutting
  concerns live in ``api.dependencies``.
- SAD.md §3.1 — authz ordering is the router's responsibility; per-
  handler scope checks are forbidden.
- NFR-02 — 401 body MUST NOT echo the presented plaintext.
- NFR-11 — keep this dependency ≤ 40 lines of real logic so future
  callers can compose it without surprising side effects.
"""  # NFR-02 NFR-11
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
# Constants for problem+json shaping (RFC 7807 / FR-10).
# ---------------------------------------------------------------------------

_PROBLEM_CONTENT_TYPE = "application/problem+json"
_GENERIC_DETAIL = "invalid or missing X-API-Key"
_FORBIDDEN_DETAIL = "forbidden"

# Status → (problem type, problem title). Keys are the only statuses
# the FR-03/FR-04 modules raise with the problem+json marker; any
# other status falls through to the original handler.
_PROBLEM_TYPES: dict[int, tuple[str, str]] = {
    401: ("/errors/unauthenticated", "Unauthorized"),
    403: ("/errors/forbidden", "Forbidden"),
}


# ---------------------------------------------------------------------------
# Monkey-patch fastapi.exception_handlers.http_exception_handler so that any
# HTTPException carrying the problem+json content-type marker is rendered as
# an RFC 7807 body. The original handler is preserved as the fall-through for
# every other exception, so the rest of the codebase is unaffected.
# ---------------------------------------------------------------------------

# The original FastAPI handler — captured BEFORE we install the FR-03
# patch below, so the patched version can defer to it for any non-401
# HTTPException and leave the rest of the app's behaviour untouched.
#
# Accessed via ``getattr`` so pyright's ``reportPrivateImportUsage`` does
# not flag ``http_exception_handler`` (which FastAPI does not export from
# ``fastapi.applications``) — the attribute is reachable at runtime via
# the ``from .exception_handlers import http_exception_handler`` line at
# the top of ``fastapi.applications``.
_original_http_exception_handler = getattr(_fa, "http_exception_handler")
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
        """Render problem+json for marker-bearing HTTPExceptions; otherwise passthrough.

        Marker: ``exc.headers`` contains ``content-type:
        application/problem+json`` — set by ``_unauthorized()`` (401)
        and ``_forbidden()`` (403) below so we can tell our 401s / 403s
        apart from anyone else's HTTPException. The body shape
        (``type / title / status / detail``) matches the rest of the
        FR-03 / FR-04 contracts documented in TEST_SPEC.md §1 rows 1
        and 5, and FR-04's ``/errors/forbidden`` problem type.

        The problem type / title is keyed off the HTTP status so a 403
        (raised by ``require_scope``) renders as
        ``/errors/forbidden`` instead of the 401 type — the FR-04 AC
        for problem+json shapes on insufficient scope.
        """  # NFR-02 NFR-09 NFR-10
        headers = getattr(exc, "headers", None) or {}
        content_type = headers.get("content-type", "")
        if content_type != _PROBLEM_CONTENT_TYPE:
            # Not ours — defer to the original FastAPI handler so the
            # rest of the app's behaviour is unchanged.
            return await _original_http_exception_handler(request, exc)
        status = exc.status_code
        type_, title = _PROBLEM_TYPES.get(status, ("/errors/http", "HTTP Error"))
        body = {
            "type": type_,
            "title": title,
            "status": status,
            "detail": exc.detail,
        }
        return JSONResponse(
            content=body,
            status_code=status,
            headers=headers,
            media_type=_PROBLEM_CONTENT_TYPE,
        )

    # ``setattr`` (vs direct attribute assignment) bypasses pyright's
    # ``reportPrivateImportUsage`` for ``http_exception_handler`` — the
    # attribute is reachable at runtime via ``fastapi.applications``'s
    # top-level ``from .exception_handlers import http_exception_handler``
    # binding but is not in FastAPI's ``__all__``.
    setattr(_fa, "http_exception_handler", _patched_http_exception_handler)
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


def _unauthorized() -> NoReturn:
    """Raise a 401 carrying the problem+json marker.

    The marker header (``content-type: application/problem+json``) is
    what the patched exception handler keys off — it is the only signal
    that distinguishes our 401s from any other HTTPException in the
    app, so the patch is strictly opt-in.

    The ``NoReturn`` annotation lets static type checkers narrow the
    optional types (``x_api_key: str | None``, ``row: dict | None``)
    after a call site so we don't need redundant ``cast``/``assert``
    boilerplate in ``require_api_key``.

    Citations: SPEC.md line 103 — 缺少或無效 → 401 + problem+json;
    TEST_SPEC.md §1 FR-03 row 1 — type ``/errors/unauthenticated``.
    """  # NFR-02 NFR-10
    raise HTTPException(
        status_code=401,
        detail=_GENERIC_DETAIL,
        headers={"content-type": _PROBLEM_CONTENT_TYPE},
    )


def _forbidden() -> NoReturn:
    """Raise a 403 carrying the problem+json marker (FR-04).

    The detail string is a CONSTANT (``"forbidden"``) and carries no
    resource identifier — this is the SPEC.md line 112 / NP-08
    no-existence-leak invariant. The patched handler renders the
    body with ``type == "/errors/forbidden"`` so the FR-04 contract
    holds regardless of which resource the caller targeted.

    Citations: SPEC.md line 112 — body 不得洩漏該資源是否存在;
    TEST_SPEC.md §1 FR-04 — type ``/errors/forbidden``.
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


# ---------------------------------------------------------------------------
# FR-04 — hierarchical scope check
# ---------------------------------------------------------------------------


# Scope hierarchy: read ⊂ write ⊂ admin (SPEC.md line 111). The rank
# table is the single source of truth used by ``require_scope`` below
# and any future tooling that wants to compare scopes (e.g. admin
# dashboards). Any unknown scope ranks as 0 and is therefore rejected
# by every ``require_scope("read"|"write"|"admin")`` call — the safe
# default for an additive permissions system.
_SCOPE_RANK: dict[str, int] = {
    "read": 1,
    "write": 2,
    "admin": 3,
}


def require_scope(required: str) -> Callable[..., dict]:
    """FastAPI dependency factory: enforce hierarchical scope on a route (FR-04).

    Usage::

        @router.get("/v1/tasks/{id}")
        async def get_task(
            task_id: str, _user: dict = Depends(require_scope("read"))
        ):
            ...

    The returned dependency:

    1. Resolves the authenticated row via
       ``Depends(require_api_key)`` — it MUST NOT perform its own auth
       lookup; composing the existing dependency means the FR-03
       401 path stays intact and the FR-04 fixture in
       ``test_fr04.py`` can swap the row by monkey-patching
       ``require_api_key``.
    2. Compares the row's ``scope`` against ``required`` using the
       hierarchical inclusion rule::

           read ⊂ write ⊂ admin

       i.e. an admin-scope user satisfies any requirement; a
       write-scope user satisfies read; a read-scope user satisfies
       only read. Comparison is by integer rank from
       ``_SCOPE_RANK`` so the hierarchy is centralised and easy to
       audit.
    3. On insufficient scope, raises HTTP 403 + problem+json with the
       constant detail string ``"forbidden"``. The detail does NOT
       vary with the requested resource id — this is the SPEC.md
       line 112 / NP-08 no-existence-leak invariant; the patched
       exception handler renders the body as RFC 7807 with
       ``type == "/errors/forbidden"``.
    4. On success, returns the row dict so the handler can read
       ``key_id`` / ``scope`` / ``revoked_at`` if it wants.

    The factory shape (not a single fixed dependency) is what lets
    different routes declare different required scopes via
    ``Depends(require_scope("read"))`` /
    ``Depends(require_scope("write"))`` /
    ``Depends(require_scope("admin"))`` while still sharing the SAME
    function object — AC-4.3 / SPEC.md line 113 單一中介層.

    Citations:
    - SPEC.md line 111 — read ⊂ write ⊂ admin (階層包含).
    - SPEC.md line 112 — scope 不足 → 403 + problem+json; body 不得
      洩漏該資源是否存在 (NP-08).
    - SPEC.md line 113 — 單一中介層 (dependency) shared by every
      /v1 route.
    - TEST_SPEC.md §1 FR-04 — three named cases: hierarchy inclusion,
      no existence leak, single shared dependency.
    """  # NFR-02 NFR-09 NFR-10 NFR-11
    required_rank = _SCOPE_RANK.get(required, 0)

    def _dep(user: dict = Depends(require_api_key)) -> dict:
        actor_scope = user.get("scope", "")
        if _SCOPE_RANK.get(actor_scope, 0) < required_rank:
            # SPEC.md line 112 — 403 detail is a constant; never echo
            # the resource id or the actor scope (NP-08 no-existence-
            # leak + NFR-02 no internal-state leak).
            _forbidden()
        return user

    return _dep
