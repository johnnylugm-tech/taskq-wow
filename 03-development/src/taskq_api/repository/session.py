"""[FR-06] Persistence seam — SQLAlchemy engine, session scope, ORM rows.

This module is the ONLY place in the package that imports ``sqlalchemy``
(NFR-06 ``forbidden-sqlalchemy`` contract): ``api/``, ``service/`` and
``models/`` reach persistence exclusively through the ``repository/``
layer, which owns both the connection pool and the transaction boundary.

It exports three things:

* ``get_engine()`` — the process-singleton :class:`~sqlalchemy.engine.Engine`
  built from ``TASKQ_DB_URL`` with ``pool_size=TASKQ_DB_POOL_SIZE`` and
  ``pool_pre_ping=True``.
* ``session_scope()`` — the request-scoped transaction boundary: one
  ``Session``, ``commit()`` on a clean exit, ``rollback()`` + re-raise on
  any exception, ``close()`` in ``finally``.
* the declarative ORM rows (``TaskRow`` / ``TaskResultRow`` /
  ``TaskTagRow``). They live here rather than under ``models/`` because
  ``models/`` may not import sqlalchemy (AC-6.1); ``.methodology/SAB.json``
  keeps the repository layer's module list closed, so the mapped classes
  share the seam that owns the engine.

All statements are ORM / parameterized — string-concatenated SQL is
forbidden (SPEC.md line 126 / NP-04).

Citations:
- SPEC.md line 124 — 業務層不得直接持有 Session;資料存取全部走 repository/ 層
  (AC-6.1 / NFR-06 forbidden-sqlalchemy contract).
- SPEC.md line 125 — 每個 API 請求一個 Session;交易邊界由 context manager
  顯式管理;成功 commit,例外 rollback (AC-6.2).
- SPEC.md line 126 — 禁止字串拼接 SQL;一律 ORM / parameterized (AC-6.3).
- SPEC.md line 127 — selectinload / joinedload 顯式預載 (AC-6.4); the
  ``results`` / ``tags`` associations declared here are what
  ``repository.tasks.fetch_tasks_page`` eager-loads.
- SPEC.md line 128 — pool_size=TASKQ_DB_POOL_SIZE, pool_pre_ping=True (AC-6.5).
- SPEC.md §5.1 — TASKQ_DB_POOL_SIZE=5, TASKQ_DB_URL default.
- SAD.md §2.7 — repository is the persistence boundary.
- NFR-04 — the connection string never leaves this module.
"""  # NFR-02 NFR-06 NFR-11
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from taskq_api import config

# Process singletons — built once by ``get_engine()`` (SPEC line 128).
_engine: sa.Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """[FR-06] Declarative base for the persistence-layer ORM rows.

    Citations: SPEC.md line 126 — ORM only, no string-concatenated SQL;
    SAD.md §2.7 — repository owns the mapped rows.
    """  # NFR-11


class TaskRow(Base):
    """[FR-06] ``tasks`` table row.

    Citations: SPEC.md §5.3 — ``tasks`` columns (id, name, command,
    status, created_at); SPEC.md line 127 — ``results`` / ``tags`` are the
    associations that list endpoints must eager-load (AC-6.4).
    """  # NFR-11

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(1000), unique=True, nullable=False)
    command: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)

    results: Mapped[list["TaskResultRow"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    tags: Mapped[list["TaskTagRow"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskResultRow(Base):
    """[FR-06] ``task_results`` table row (v3 schema).

    Citations: SPEC.md line 98 — ``task_results`` columns (``exit_code`` /
    ``stdout_tail`` / ``stderr_tail`` / ``duration_ms`` / ``finished_at``);
    SPEC.md line 99 — the run history read newest-first by
    ``repository.results.fetch_results_for_task``; SPEC.md line 127 —
    eager-loaded association (AC-6.4).
    """  # NFR-11

    __tablename__ = "task_results"

    run_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        sa.ForeignKey("tasks.id"), nullable=False, index=True
    )
    exit_code: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    stdout_tail: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    stderr_tail: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    finished_at: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)

    task: Mapped["TaskRow"] = relationship(back_populates="results")


class TaskTagRow(Base):
    """[FR-06] ``task_tags`` table row (v2 schema).

    Citations: SPEC.md §5.3 — ``task_tags`` join rows; SPEC.md line 127 —
    eager-loaded association (AC-6.4).
    """  # NFR-11

    __tablename__ = "task_tags"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.ForeignKey("tasks.id"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    task: Mapped["TaskRow"] = relationship(back_populates="tags")


def database_url() -> str:
    """[FR-06] Resolve the database URL: environment first, config default.

    The value is read here and nowhere else so the connection string never
    reaches the api/service layers (NFR-04).

    Citations: SPEC.md §5.1 — ``TASKQ_DB_URL``; NFR-04 — connection string
    must not leak.
    """  # NFR-04 NFR-11
    return os.environ.get("TASKQ_DB_URL", config.TASKQ_DB_URL)


def get_engine() -> sa.Engine:
    """[FR-06] Return the process-singleton Engine (AC-6.5).

    The engine is created once with ``pool_size=TASKQ_DB_POOL_SIZE`` (5 per
    SPEC §5.1) and ``pool_pre_ping=True`` so a stale pooled connection is
    recycled instead of surfacing as a request error. The schema is created
    on first use; migrations (Alembic) replace this in FR-07.

    Citations: SPEC.md line 128 — pool_size=TASKQ_DB_POOL_SIZE,
    pool_pre_ping=True; SPEC.md §5.1 — TASKQ_DB_POOL_SIZE=5.
    """  # NFR-01 NFR-11
    global _engine, _session_factory
    if _engine is None:
        _engine = sa.create_engine(
            database_url(),
            pool_size=config.TASKQ_DB_POOL_SIZE,
            pool_pre_ping=True,
        )
        Base.metadata.create_all(_engine)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """[FR-06] One ``Session`` per request, with an explicit transaction boundary.

    Commits when the block exits cleanly, rolls back and re-raises when the
    block raises (the rollback is unconditional — any exception type), and
    always closes the session in ``finally``.

    Citations: SPEC.md line 125 — 每個 API 請求一個 Session;成功 commit,
    例外 rollback; SPEC.md line 124 — the business layer never holds the
    Session itself (AC-6.2 / AC-6.1).
    """  # NFR-03 NFR-11
    get_engine()
    session = _session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        # Unconditional rollback — including CancelledError, which must
        # keep propagating untouched (NFR-03).
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def active_session(session: Session | None) -> Iterator[Session]:
    """[FR-06] Use the caller's ``Session``, or open a scoped one.

    Every repository function takes an optional ``Session`` as its first
    argument. When the caller passes one it owns the transaction boundary
    (SPEC line 125) and the repository must neither commit nor roll back;
    when it passes ``None`` the repository borrows its own
    ``session_scope()``, which commits on success and rolls back on
    exception.

    This helper lives beside ``session_scope`` so every repository module
    resolves the boundary the same way instead of re-deriving the rule.

    Citations: SPEC.md line 125 — one Session per request, boundary owned
    by the context manager; SPEC.md line 124 — callers in service/ never
    hold a Session themselves.
    """  # NFR-11
    if session is not None:
        yield session
    else:
        with session_scope() as own_session:
            yield own_session
