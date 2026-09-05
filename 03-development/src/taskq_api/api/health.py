"""[FR-09] HTTP router for ``/healthz`` and ``/readyz``.

This router implements the FR-09 liveness + readiness probes per
SPEC.md lines 156-157 + §8 #10/#11:

  * ``GET /healthz`` — process-alive liveness probe; returns 200 with
    ``{"status": "ok"}`` and depends on NO auth (SPEC.md line 107).
  * ``GET /readyz`` — readiness probe that evaluates two predicates:
    (a) DB reachability and (b) ``alembic current == head``. Returns
    200 only when BOTH are True; otherwise 503 with a ``detail`` that
    names which check failed so an operator can diagnose the cause
    (SPEC.md §8 #10 / #11 forbid silent retry-to-infinity on a
    not-ready process).

Both predicates are exposed as module-level callables
(``check_db_reachable() -> bool`` and
``check_migrations_at_head() -> bool``) so the RED fault-injection
tests in ``test_fr09.py`` can monkey-patch them to simulate
failures without a live DB / alembic invocation. The default bodies
are simple, idempotent, and never raise — they translate a probe
failure into a ``False`` return so the readiness handler can render
the appropriate 503 with diagnostic detail.

Citations:
- SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09).
- SPEC.md line 156 — ``GET /healthz`` 進程存活 → 200 ``{status:ok}``.
- SPEC.md line 157 — ``GET /readyz`` DB 連線可用且 alembic current ==
  head → 200; 否則 503 並在 body 說明哪一項失敗.
- SPEC.md §8 #10 — DB outage → /readyz 503 + DB-failure detail.
- SPEC.md §8 #11 — migration not at head → /readyz 503 + migration
  detail (fail-closed).
- SAD.md §3.1 — observability lives in the api layer.
- NFR-04 — DB URL password MUST NOT appear in any response body.
- NFR-11 — handlers ≤40 lines each.
"""  # NFR-04 NFR-09 NFR-10 NFR-11
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse


router = APIRouter(tags=["health"])


def check_db_reachable() -> bool:
    """Return ``True`` iff the configured database accepts a connection.

    The default body opens a transient connection against the engine
    built by ``taskq_api.repository.session.get_engine()`` and treats
    ANY exception (operational, programming, or unexpected) as
    "unreachable" — ``False``. The handler relies on this contract to
    surface a 503 with ``detail`` naming the DB rather than letting an
    internal exception escape the probe (SPEC.md §8 #10 — DB outage
    MUST surface as a structured 503, never a retry-to-infinity loop).

    Tests monkey-patch this symbol; the production body MUST NOT add
    any bypass that only fires under test, otherwise the seam the
    fault-injection tests rely on would silently disappear.

    Citations: SPEC.md line 157 + §8 #10.
    """  # NFR-04 NFR-09 NFR-11
    try:
        from sqlalchemy import text

        from taskq_api.repository.session import get_engine

        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001  (probe intentionally total — §8 #10)
        return False


def check_migrations_at_head() -> bool:
    """Return ``True`` iff ``alembic current`` matches the script head.

    The default body invokes ``alembic.command.current`` against the
    in-tree ``migrations/`` directory and compares the resolved
    revision against ``alembic.script.ScriptDirectory.from_config(
    ...).head``. ANY exception (missing config, script directory
    absent, current revision diverged from head) collapses to
    ``False`` so the readiness handler renders a 503 with the
    migration-failure detail — the fail-closed guard from SPEC.md
    line 157 + §8 #11 forbids a process whose migrations are stale
    from passing ``/readyz``.

    Tests monkey-patch this symbol; the production body MUST NOT add
    any bypass that only fires under test, otherwise the seam the
    fault-injection tests rely on would silently disappear.

    Citations: SPEC.md line 157 + §8 #11.
    """  # NFR-04 NFR-09 NFR-11
    try:
        from alembic.command import current as alembic_current
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        from taskq_api.repository.session import database_url

        migrations_dir = "03-development/src/migrations"
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", migrations_dir)
        cfg.set_main_option("sqlalchemy.url", database_url())
        script_directory = ScriptDirectory.from_config(cfg)
        head_revision: Optional[str] = script_directory.head
        current_rev: Optional[str] = alembic_current(cfg)
        if head_revision is None:
            return False
        # ``current`` returns a list of revisions (one per database
        # head when multiple are configured); an empty list means the
        # database has never been stamped — also a not-at-head state.
        return bool(current_rev) and head_revision in current_rev
    except Exception:  # noqa: BLE001  (probe intentionally total — §8 #11)
        return False


@router.get("/healthz")
async def healthz_endpoint() -> JSONResponse:
    """GET /healthz — liveness probe (FR-09 / AC-9.1).

    Returns 200 with ``{"status": "ok"}`` whenever the Python process
    is alive enough to serve HTTP. MUST NOT depend on
    ``require_api_key`` (SPEC.md line 107); a missing X-API-Key
    header on this route is NOT an auth failure.

    Citations: SPEC.md line 156; SPEC.md line 107.
    """  # NFR-09 NFR-11
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/readyz")
async def readyz_endpoint() -> JSONResponse:
    """GET /readyz — readiness probe (FR-09 / AC-9.2, AC-9.3, AC-9.4).

    Evaluates the two module-level predicates:

      * ``check_db_reachable()`` — DB connectable.
      * ``check_migrations_at_head()`` — ``alembic current == head``.

    Returns 200 with ``{"status": "ok"}`` only when BOTH are True
    (AC-9.2). On any False or any exception raised by the predicates,
    returns 503 with a ``detail`` that names which check failed
    (AC-9.3 / AC-9.4 — SPEC.md §8 #10 / #11 forbid silent retry on
    a not-ready process). The body MUST NOT echo the DB URL
    (NFR-04).

    Citations: SPEC.md line 157 + §8 #10 / #11.
    """  # NFR-04 NFR-09 NFR-11
    db_ok = False
    try:
        db_ok = bool(check_db_reachable())
    except Exception:
        db_ok = False
    if not db_ok:
        return JSONResponse(
            status_code=503,
            content={"detail": "db unreachable: connection probe failed"},
        )
    migrations_ok = False
    try:
        migrations_ok = bool(check_migrations_at_head())
    except Exception:
        migrations_ok = False
    if not migrations_ok:
        return JSONResponse(
            status_code=503,
            content={"detail": "migration not at head: alembic current != head"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})


def register(app) -> None:  # type: ignore[no-untyped-def]
    """Mount the health router onto ``app``.

    Either ``router`` (an ``APIRouter``) or ``register(app)`` is
    accepted by the FR-09 test wiring; both shapes are supported so
    the GREEN contract stays decoupled from the chosen surface
    (test_fr09.py tries ``register`` first, then ``router``).

    Citations: SPEC.md line 156-157.
    """  # NFR-09 NFR-11
    app.include_router(router)