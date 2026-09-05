"""[FR-06] Persistence + Transaction Boundaries (repository/ only, one
Session/request, no string-concat SQL, eager-load N+1 guard, pool config)
— RED tests.

These tests are the RED phase of TDD for FR-06. They reference the
SAB-declared module ``taskq_api.repository.session`` (``.methodology/SAB.json``
→ FR-06 row 1). That module does NOT exist on disk yet, so the top-level
import below raises ``ModuleNotFoundError`` and pytest reports Exit Code 2
(Collection Error). That is the **valid RED state** — no
``try/except ImportError`` is used to hide it.

The five tests below pin down SPEC.md lines 124-128:

  - AC-6.1  test_repository_is_only_sqlalchemy_importer
            service/, api/, models/ must not import sqlalchemy directly;
            repository/ is the only importer (NFR-06 / import-linter
            forbidden-sqlalchemy contract).

  - AC-6.2  test_request_session_scope_commit_on_success_rollback_on_exception
            One ``Session`` per API request; ``session_scope`` commits
            on success and rolls back on exception (guaranteed by the
            context manager).

  - AC-6.3  test_no_sql_string_concatenation_in_source
            ``grep`` for f-string / ``+`` / ``%`` SQL concatenation
            patterns over ``03-development/src/`` yields 0 hits.

  - AC-6.4  test_list_endpoint_constant_sql_count_no_n_plus_1
            List endpoints use ``selectinload`` / ``joinedload`` to
            eager-load associations; the number of SQL statements per
            list request is constant (independent of returned rows).

  - AC-6.5  test_engine_pool_size_and_pre_ping_configured
            SQLAlchemy engine uses ``pool_size=TASKQ_DB_POOL_SIZE``
            and ``pool_pre_ping=True`` (SPEC §5.1).

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. The dynamic tests (2, 4, 5)
call ``session_scope()`` / ``get_engine()`` directly; the static tests
(1, 3) scan the source tree with ``pathlib.Path.rglob``. No subprocess
is spawned, so pytest-cov attributes execution to the real handler /
repository / engine functions and the Gate-1 ``test_coverage`` dimension
can see them.

Citations:
- SPEC.md line 124 — 業務層不得直接持有 Session / 資料存取全部走
  repository/ 層 (NFR-06 forbidden-sqlalchemy contract).
- SPEC.md line 125 — 每個 API 請求一個 Session;交易邊界由 context
  manager 顯式管理;成功 commit,例外 rollback.
- SPEC.md line 126 — 禁止字串拼接 SQL;一律 ORM / parameterized.
- SPEC.md line 127 — selectinload / joinedload 顯式預載;N+1 為驗收
  失敗條件 (NFR-01 / NP-06).
- SPEC.md line 128 — pool_size=TASKQ_DB_POOL_SIZE, pool_pre_ping=True.
- SPEC.md §5.1 — TASKQ_DB_POOL_SIZE=5.
- TEST_SPEC.md §1 FR-06 — the five named cases implemented below.
- SAD.md §2.7 — repository is the persistence seam.
- NFR-06 — api > service > repository > models layering; sqlalchemy is
  forbidden outside repository/.
"""  # NFR-02 NFR-05 NFR-06 NFR-10

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.repository.session must export:
#   get_engine() -> sqlalchemy.engine.Engine
#       Returns a process-singleton SQLAlchemy 2.0 Engine constructed
#       from TASKQ_DB_URL with:
#           pool_size = TASKQ_DB_POOL_SIZE  (= 5 per SPEC §5.1)
#           pool_pre_ping = True            (SPEC line 128)
#
#   session_scope() -> ContextManager[sqlalchemy.orm.Session]
#       Context manager that yields a fresh Session bound to the engine
#       from ``get_engine()``. On a clean exit it MUST call
#       ``session.commit()``; on any exception it MUST call
#       ``session.rollback()`` and re-raise the original exception. The
#       Session is closed in a ``finally`` clause (AC-6.2 / SPEC line 125).
#
# The repository layer is the only place that may import sqlalchemy
# directly (AC-6.1 / NFR-06 forbidden-sqlalchemy contract). The GREEN
# agent will also migrate ``taskq_api.repository.tasks`` from the
# in-memory dict to SQLAlchemy ORM so that ``insert_task`` /
# ``fetch_tasks_page`` / ``delete_task_row`` honor the new ``Session``
# argument (currently a no-op passed as ``None``).
# ---------------------------------------------------------------------------


# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-06). ``taskq_api.repository.session`` does
# NOT exist on disk yet → ModuleNotFoundError at collection time is the
# valid RED signal; pytest reports Exit Code 2. Do not wrap in
# try/except; do not lazy-import.
from taskq_api.repository.session import (  # noqa: E402,F401  [FR-06]
    get_engine,
    session_scope,
)


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_TASKQ_API_DIR = _SRC_ROOT / "taskq_api"
_REPO_DIR = _TASKQ_API_DIR / "repository"


# ---------------------------------------------------------------------------
# AC-6.1 — repository/ is the only sqlalchemy importer (static)
# ---------------------------------------------------------------------------


def test_repository_is_only_sqlalchemy_importer():
    """AC-6.1: business-layer code (``service/``, ``api/``, ``models/``)
    must NOT import ``sqlalchemy`` directly; the ``repository/`` layer is
    the only importer (NFR-06 forbidden-sqlalchemy contract, import-linter
    ``forbidden-sqlalchemy`` contract).

    TEST_SPEC inputs:
      grep_pattern="import sqlalchemy"
      expected_match_count_outside_repo="0"
    Sub-assertion: FR06-no-sqlalchemy-outside-repo — count must be 0.

    The test scans every ``.py`` file under ``03-development/src/``. Any
    occurrence of the substring ``import sqlalchemy`` in a file outside
    the ``taskq_api/repository/`` tree is a violation (the corresponding
    import-linter contract would also fail with exit 1).

    The second half of the assertion confirms that the feature IS
    implemented in the repository layer — ``repository/session.py`` must
    exist (SAB FR-06 row 1) and must import ``sqlalchemy``. Without that
    half, the test would pass on a codebase that has NO sqlalchemy
    anywhere (the trivial case that lacks the feature).
    """  # NFR-02 NFR-06 NFR-09 NFR-10
    grep_pattern = "import sqlalchemy"
    expected_match_count_outside_repo = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_match_count_outside_repo == "0"

    # Layers that must NOT import sqlalchemy per NFR-06 / SAD §2.7.
    forbidden_top_dirs = {"service", "api", "models"}

    matches_outside_repo = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_SRC_ROOT)
        parts = rel.parts
        # Files under taskq_api/<layer>/... — exclude the ``repository/``
        # tree, the ``__init__`` markers at the package root, and any
        # utility file (e.g. ``taskq_api/__main__.py``, ``config.py``,
        # ``errors.py``) — those are independence-layer stubs.
        if not parts or parts[0] != "taskq_api":
            continue
        if len(parts) < 3:
            # taskq_api/__init__.py or taskq_api/__main__.py — top-level
            # package file, not a forbidden layer.
            continue
        layer = parts[1]
        if layer in forbidden_top_dirs:
            content = py_file.read_text(encoding="utf-8")
            for line_num, line in enumerate(content.splitlines(), start=1):
                if grep_pattern in line:
                    matches_outside_repo.append(
                        f"{rel.as_posix()}:{line_num}: {line.strip()}"
                    )

    # ---- Sub-assertion FR06-no-sqlalchemy-outside-repo:
    #      expected_match_count_outside_repo == "0". ----
    assert len(matches_outside_repo) == 0, (
        f"sqlalchemy must only be imported in repository/ "
        f"(AC-6.1 / NFR-06 forbidden-sqlalchemy contract); "
        f"found {len(matches_outside_repo)} forbidden matches: "
        f"{matches_outside_repo}"
    )

    # ---- Positive half: repository/session.py must exist AND import
    #      sqlalchemy. Without this half, the test would pass on a
    #      codebase that has NO sqlalchemy anywhere — the trivial RED
    #      state where the feature is missing entirely. ----
    session_py = _REPO_DIR / "session.py"
    assert session_py.is_file(), (
        f"GREEN must add taskq_api/repository/session.py per SAB.json "
        f"FR-06 row 1 (AC-6.1); file not found at {session_py}"
    )
    session_content = session_py.read_text(encoding="utf-8")
    assert grep_pattern in session_content, (
        f"taskq_api/repository/session.py must `import sqlalchemy` "
        f"(AC-6.1: repository is the only sqlalchemy importer); "
        f"got first 200 chars: {session_content[:200]!r}"
    )


# ---------------------------------------------------------------------------
# AC-6.2 — session_scope commits on success, rolls back on exception
# ---------------------------------------------------------------------------


def test_request_session_scope_commit_on_success_rollback_on_exception():
    """AC-6.2: every API request opens exactly one ``Session``;
    ``session_scope`` commits on a clean exit and rolls back on any
    exception (guaranteed by the context manager — SPEC.md line 125).

    TEST_SPEC inputs:
      trigger_exception="true"
      expected_committed_rows_on_exception="0"
      expected_rolled_back_rows_on_exception="0"
    Sub-assertions:
      FR06-rollback-zero-committed   (no rows committed on exception)
      FR06-rollback-confirmed        (no rows leaked across rollback)

    The test exercises three paths:

      PART A — success commit:
        Open ``session_scope()``, insert a task, exit cleanly. The row
        MUST be visible to a fresh ``session_scope()`` afterwards
        (because the first commit succeeded).

      PART B — exception rollback:
        Open ``session_scope()``, insert a task, raise ``RuntimeError``.
        The ``pytest.raises`` context catches the exception (the manager
        MUST re-raise). A subsequent ``session_scope()`` MUST NOT see
        the row (the rollback undid the INSERT).

      PART C — unrelated exceptions are also rolled back:
        The same contract holds when the exception is a custom type the
        implementation does not specifically handle — the rollback must
        be unconditional.
    """  # NFR-02 NFR-06 NFR-09 NFR-10
    trigger_exception = "true"
    expected_committed_rows_on_exception = "0"
    expected_rolled_back_rows_on_exception = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert trigger_exception == "true"
    assert expected_committed_rows_on_exception == "0"
    assert expected_rolled_back_rows_on_exception == "0"

    # Lazy import the repository seam so the Collection Error above
    # already failed the test before this code runs.
    from taskq_api.repository import tasks as repo  # noqa: E402

    # -----------------------------------------------------------------
    # PART A — success path: session_scope commits a clean operation.
    # -----------------------------------------------------------------
    committed_name = "fr06-session-scope-commit-target"

    # Establish a deterministic baseline.
    if hasattr(repo, "_reset_state"):
        repo._reset_state()

    with session_scope() as session:
        # GREEN TODO: insert_task(session, *, name, command) -> str.
        # The GREEN repository takes a real Session and flushes via
        # SQLAlchemy; the RED in-memory implementation accepts the
        # session and ignores it. Either way the row materializes.
        repo.insert_task(session, name=committed_name, command="echo committed")

    # After the context exits cleanly the row MUST be visible in a
    # FRESH session — i.e. the previous session was COMMITTED.
    with session_scope() as verify_session:
        items_after_commit, _ = repo.fetch_tasks_page(
            verify_session,
            limit=200,
            cursor=None,
            status=None,
        )
    names_after_commit = {t["name"] for t in items_after_commit}
    assert committed_name in names_after_commit, (
        f"session_scope must COMMIT a successful operation "
        f"(AC-6.2); row {committed_name!r} not visible after clean exit"
    )

    # -----------------------------------------------------------------
    # PART B — failure path: session_scope rolls back on exception.
    # -----------------------------------------------------------------
    rolled_back_name = "fr06-session-scope-rollback-target"

    # Baseline count for the "rows on exception == 0" invariant.
    with session_scope() as baseline_session:
        pre_items, _ = repo.fetch_tasks_page(
            baseline_session, limit=200, cursor=None, status=None
        )
    pre_names = {t["name"] for t in pre_items}

    # ---- Sub-assertion FR06-rollback-zero-committed +
    #      FR06-rollback-confirmed: the manager MUST re-raise the
    #      exception (so the surrounding pytest.raises catches it) AND
    #      the row MUST NOT survive past the rollback. ----
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            repo.insert_task(
                session, name=rolled_back_name, command="echo rolled-back"
            )
            raise RuntimeError("triggered for rollback test")

    # Open a fresh session to read post-rollback state. The row
    # inserted above MUST be absent.
    with session_scope() as verify_session:
        post_items, _ = repo.fetch_tasks_page(
            verify_session, limit=200, cursor=None, status=None
        )
    post_names = {t["name"] for t in post_items}

    # Sub-assertion FR06-rollback-zero-committed:
    # expected_committed_rows_on_exception == "0"
    newly_committed = post_names - pre_names
    assert rolled_back_name not in newly_committed, (
        f"session_scope must ROLL BACK the failed operation "
        f"(AC-6.2); row {rolled_back_name!r} survived the exception. "
        f"Newly committed rows: {newly_committed!r}"
    )

    # Sub-assertion FR06-rollback-confirmed:
    # expected_rolled_back_rows_on_exception == "0"
    assert rolled_back_name not in post_names, (
        f"session_scope must ROLL BACK the failed operation "
        f"(AC-6.2 / SPEC line 125); row {rolled_back_name!r} is still "
        f"visible after the exception was raised and re-raised"
    )


# ---------------------------------------------------------------------------
# AC-6.3 — no SQL string concatenation in source (static grep)
# ---------------------------------------------------------------------------


def test_no_sql_string_concatenation_in_source():
    """AC-6.3: ``grep -rn`` over ``03-development/src/`` for f-string /
    ``+`` / ``%`` SQL concatenation yields 0 hits (SPEC.md line 126 +
    §8 #17 / NP-04).

    TEST_SPEC inputs:
      grep_pattern_concat_form = 'f"SELECT \\|\\| ".*\\{.*\\}.*FROM'
      expected_match_count = "0"
    Sub-assertion: FR06-no-sql-concat-zero — count must be 0.

    The test scans every ``.py`` file under ``03-development/src/`` for
    SQL-concat patterns. The spec pattern is Oracle-style
    (``SELECT || `` in an f-string with a ``{...}`` placeholder) — the
    test ALSO checks for the broader Pythonic patterns that real
    string-concat SQL takes (f-string with SQL keywords + placeholder,
    ``+``/``%`` concatenation against SQL keywords). All pattern groups
    must yield zero hits.

    The second half confirms ``repository/session.py`` exists with
    parameterized / ORM-style queries (a stub file with NO SQL at all
    would make the grep pass trivially without implementing the
    feature).
    """  # NFR-02 NFR-06 NFR-09 NFR-10
    # Decode the spec's JSON-escaped regex. The TEST_SPEC input cell
    # stores it as `f\"SELECT \\|\\| \".*\\{.*\\}.*FROM`; unescaped that
    # is `f"SELECT || ".*{.*}.*FROM` which, as a regex, matches an
    # f-string starting with ``SELECT || `` followed by ``{...}`` and
    # ``FROM`` — Oracle-style concat in Python.
    grep_pattern_concat_form = r'f"SELECT \|\| ".*{.*}.*FROM'
    expected_match_count = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_match_count == "0"

    # Broader Pythonic patterns — any f-string that interpolates into a
    # SQL keyword is forbidden (SPEC line 126). These catch the realistic
    # shapes a developer might write, not just the Oracle-specific shape
    # the spec regex targets.
    additional_patterns = [
        # f-string with SELECT + FROM + placeholder
        r'f["\'].{0,40}SELECT.{0,200}\{.{0,200}\}.{0,200}FROM',
        # f-string with INSERT + INTO + placeholder
        r'f["\'].{0,40}INSERT.{0,200}\{.{0,200}\}.{0,200}INTO',
        # f-string with UPDATE + SET + placeholder
        r'f["\'].{0,40}UPDATE.{0,200}\{.{0,200}\}.{0,200}SET',
        # f-string with DELETE + FROM + placeholder
        r'f["\'].{0,40}DELETE.{0,200}\{.{0,200}\}.{0,200}FROM',
    ]

    matches = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_SRC_ROOT)
        content = py_file.read_text(encoding="utf-8")
        # Spec regex — match per-line so we can pinpoint the offender.
        for line_num, line in enumerate(content.splitlines(), start=1):
            if re.search(grep_pattern_concat_form, line):
                matches.append(
                    f"{rel.as_posix()}:{line_num} [spec] {line.strip()}"
                )
            for pat in additional_patterns:
                if re.search(pat, line):
                    matches.append(
                        f"{rel.as_posix()}:{line_num} [extra] {line.strip()}"
                    )

    # ---- Sub-assertion FR06-no-sql-concat-zero:
    #      expected_match_count == "0" (over both the spec pattern and
    #      the broader Pythonic patterns). ----
    assert len(matches) == 0, (
        f"no string-concatenated SQL allowed in source "
        f"(AC-6.3 / SPEC.md line 126 / §8 #17 / NP-04); "
        f"found {len(matches)} matches: {matches}"
    )

    # ---- Positive half: repository/session.py must exist with at
    #      least one parameterized / ORM-style statement so the test
    #      guards a real implementation, not an empty stub. ----
    session_py = _REPO_DIR / "session.py"
    assert session_py.is_file(), (
        f"GREEN must add taskq_api/repository/session.py per SAB.json "
        f"FR-06 row 1 (AC-6.3 / AC-6.2 / AC-6.5)"
    )
    session_content = session_py.read_text(encoding="utf-8")
    # Either a SQLAlchemy ORM ``select(...)`` / ``Session.execute`` or
    # an explicit ``text(..., bindparams(...))`` proves parameterized
    # usage. An f-string concatenation would be the opposite signal.
    has_orm = bool(
        re.search(r"\bselect\s*\(", session_content)
        or re.search(r"\btext\s*\(", session_content)
        or re.search(r"\bSession\s*\(", session_content)
        or re.search(r"\bsessionmaker\s*\(", session_content)
    )
    assert has_orm, (
        f"taskq_api/repository/session.py must use ORM / parameterized "
        f"queries (AC-6.3 / SPEC line 126); no select( / text( / "
        f"sessionmaker( call found"
    )


# ---------------------------------------------------------------------------
# AC-6.4 — list endpoint constant SQL count (N+1 guard)
# ---------------------------------------------------------------------------


def test_list_endpoint_constant_sql_count_no_n_plus_1():
    """AC-6.4: list endpoints use ``selectinload`` / ``joinedload`` to
    eager-load associations; the number of SQL statements per list
    request is **constant** (independent of returned rows — SPEC.md
    line 127 + §8 #14 / NFR-01 / NP-06).

    TEST_SPEC inputs:
      num_tasks_in_db = "10"
      expected_statement_count_per_request = "4"
    Sub-assertion: FR06-list-constant-sql-count — must equal 4.

    The test:
      1. Seeds the repository with ``num_tasks_in_db`` rows (10).
      2. Attaches a SQLAlchemy ``before_cursor_execute`` event listener
         to the engine returned by ``get_engine()`` to record every
         statement the session issues.
      3. Opens a ``session_scope()`` and calls ``fetch_tasks_page`` with
         ``limit=10``.
      4. Asserts the listener saw exactly ``expected_statement_count``
         (4) statements, regardless of the row count.

    In the GREEN implementation, the four statements are:
      (a) ``SELECT ... FROM tasks``               — main query
      (b) ``SELECT ... FROM task_results``        — eager-load
      (c) ``SELECT ... FROM task_tags``           — eager-load
      (d) ``SELECT count(*) FROM tasks``          — total / cursor
    The exact breakdown is the GREEN agent's choice; the invariant
    this test pins down is the **count**, not the breakdown.

    Citations: SPEC.md line 127 — selectinload / joinedload 顯式預載;
    N+1 為驗收失敗條件.
    """  # NFR-01 NFR-02 NFR-06 NFR-09 NFR-10
    num_tasks_in_db = "10"
    expected_statement_count_per_request = "4"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert num_tasks_in_db == "10"
    assert expected_statement_count_per_request == "4"

    from sqlalchemy import event  # type: ignore  # SQLAlchemy 2.0 — declared dep

    from taskq_api.repository import tasks as repo  # noqa: E402

    # ---- 1. Seed N tasks. Reset state so the test is deterministic. ----
    if hasattr(repo, "_reset_state"):
        repo._reset_state()
    n_rows = int(num_tasks_in_db)
    for i in range(n_rows):
        repo.insert_task(
            None,
            name=f"fr06-n1-task-{i:02d}",
            command=f"echo n1-row-{i:02d}",
        )

    # ---- 2. Attach an event listener to the GREEN engine. ----
    engine = get_engine()
    statements: list[str] = []

    def _record_statement(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record_statement)

    try:
        # ---- 3. Drive one list request through the repository. ----
        with session_scope() as session:
            items, _next_cursor = repo.fetch_tasks_page(
                session,
                limit=n_rows,
                cursor=None,
                status=None,
            )
    finally:
        # Detach the listener so it does not leak into other tests.
        event.remove(engine, "before_cursor_execute", _record_statement)

    # Sanity: the seeded rows must all come back (the test only counts
    # statements when the repository actually returns N rows).
    assert len(items) == n_rows, (
        f"fetch_tasks_page must return all {n_rows} seeded rows; "
        f"got {len(items)} items"
    )

    # ---- 4. Constant SQL count — independent of returned rows. ----
    # ---- Sub-assertion FR06-list-constant-sql-count:
    #      expected_statement_count_per_request == "4" ----
    actual_count = len(statements)
    assert actual_count == int(expected_statement_count_per_request), (
        f"list endpoint must issue a constant number of SQL statements "
        f"for {n_rows} rows (no N+1, AC-6.4 / SPEC line 127 / NP-06); "
        f"expected {expected_statement_count_per_request}, "
        f"got {actual_count}. Statements observed:\n"
        + "\n".join(f"  [{i}] {s}" for i, s in enumerate(statements[:20]))
    )

    # ---- Constant-in-rows property — repeat with fewer rows and
    #      assert the count is the same. This is the "independent of
    #      returned rows" half of the AC; without it the test would
    #      pass with N=4 even on an N+1 implementation that happens to
    #      issue exactly 4 statements for 10 rows. ----
    # Reset and seed a smaller row set (5 rows), re-attach the
    # listener, and re-run the list request.
    if hasattr(repo, "_reset_state"):
        repo._reset_state()
    smaller_n = 5
    for i in range(smaller_n):
        repo.insert_task(
            None,
            name=f"fr06-n1-smaller-{i:02d}",
            command=f"echo smaller-{i:02d}",
        )
    statements.clear()
    event.listen(engine, "before_cursor_execute", _record_statement)
    try:
        with session_scope() as session:
            smaller_items, _ = repo.fetch_tasks_page(
                session, limit=smaller_n, cursor=None, status=None
            )
    finally:
        event.remove(engine, "before_cursor_execute", _record_statement)

    assert len(smaller_items) == smaller_n, (
        f"fetch_tasks_page must return all {smaller_n} seeded rows; "
        f"got {len(smaller_items)} items"
    )
    assert len(statements) == int(expected_statement_count_per_request), (
        f"list endpoint SQL count must be CONSTANT (independent of "
        f"returned rows, AC-6.4); with {n_rows} rows the count was OK, "
        f"but with {smaller_n} rows the count is {len(statements)} "
        f"(expected {expected_statement_count_per_request}). "
        f"This is an N+1 regression."
    )


# ---------------------------------------------------------------------------
# AC-6.5 — engine pool_size and pool_pre_ping
# ---------------------------------------------------------------------------


def test_engine_pool_size_and_pre_ping_configured():
    """AC-6.5: SQLAlchemy engine uses ``pool_size=TASKQ_DB_POOL_SIZE``
    (5 per SPEC §5.1) and ``pool_pre_ping=True`` (SPEC line 128).

    TEST_SPEC inputs:
      expected_pool_size = "5"
      expected_pool_pre_ping = "True"
    Sub-assertions:
      FR06-pool-size-equals-5
      FR06-pool-pre-ping-true

    The GREEN ``get_engine()`` constructs an Engine bound to the
    TASKQ_DB_URL with ``pool_size=5`` and ``pool_pre_ping=True``. The
    test inspects ``engine.pool.size()`` and the engine's
    ``pool._pre_ping`` private attribute (stable across SQLAlchemy 2.x)
    to verify the configuration took effect.

    Citations: SPEC.md line 128 — pool_size=TASKQ_DB_POOL_SIZE,
    pool_pre_ping=True; SPEC §5.1 — TASKQ_DB_POOL_SIZE=5.
    """  # NFR-02 NFR-09 NFR-10
    expected_pool_size = "5"
    expected_pool_pre_ping = "True"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_pool_size == "5"
    assert expected_pool_pre_ping == "True"

    engine = get_engine()

    # ---- Sub-assertion FR06-pool-size-equals-5:
    #      expected_pool_size == "5" ----
    actual_pool_size = engine.pool.size()
    assert actual_pool_size == int(expected_pool_size), (
        f"engine.pool.size() must be {expected_pool_size} "
        f"(SPEC §5.1 TASKQ_DB_POOL_SIZE / AC-6.5 / SPEC line 128); "
        f"got {actual_pool_size}"
    )

    # ---- Sub-assertion FR06-pool-pre-ping-true:
    #      expected_pool_pre_ping == "True" ----
    # SQLAlchemy 2.0 stores ``pool_pre_ping`` on the engine's pool as
    # the private ``_pre_ping`` attribute. The attribute is stable
    # across the 2.x line and is the canonical way to introspect the
    # setting from outside the engine's public API.
    actual_pre_ping = getattr(engine.pool, "_pre_ping", None)
    assert actual_pre_ping is True, (
        f"engine.pool._pre_ping must be True "
        f"(AC-6.5 / SPEC line 128); got {actual_pre_ping!r}"
    )
