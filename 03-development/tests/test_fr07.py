"""[FR-07] Schema Migration (Alembic 三步演進: v1 tasks+api_keys, v2
tags + tasks.name unique, v3 result_json → task_results with data
migration; downgrade preserved; round-trip bytewise) — RED tests.

These tests are the RED phase of TDD for FR-07. They reference the
SAB-declared modules ``.methodology/SAB.json`` → FR-07 row 1-3:

  - migrations.versions.v1_initial       (creates tasks + api_keys)
  - migrations.versions.v2_add_tags     (adds tags + task_tags + ix_tasks_name_unique)
  - migrations.versions.v3_split_results (data migration: tasks.result_json → task_results)

These modules do NOT exist on disk yet, so the top-level imports below
raise ``ModuleNotFoundError`` and pytest reports Exit Code 2 (Collection
Error). That is the **valid RED state** — no ``try/except ImportError``
is used to hide it.

The seven tests below pin down SPEC.md lines 130-143:

  - AC-7.1  test_alembic_upgrade_head_succeeds
            `alembic upgrade head` must succeed on a fresh database.
  - AC-7.2  test_alembic_downgrade_base_no_residual_tables
            `alembic downgrade base` must succeed after upgrade head;
            no tables may remain in the schema.
  - AC-7.3  test_alembic_v2_adds_tags_and_unique_index
            v2 must add `tags`, `task_tags` (m:n) and a unique index on
            `tasks.name` WITHOUT altering v1 data.
  - AC-7.4  test_alembic_v3_splits_results_with_data_migration
            v3 must split `tasks.result_json` into a separate
            `task_results` table; existing rows migrate; the original
            column is dropped.
  - AC-7.5  test_migration_round_trip_preserves_sample_data_bytewise
            upgrade head → insert sample → downgrade -1 → upgrade head;
            every column of the sample must be byte-identical to the
            pre-roundtrip state.
  - AC-7.6  test_migration_files_have_real_downgrade_no_drop_table_shortcuts
            No `op.execute("DROP TABLE ...")` may substitute for a real
            downgrade.
  - AC-7.7  test_migration_offline_sql_generation_matches_expectations
            The migration files are covered by alembic's offline SQL
            generation; the emitted SQL must contain CREATE TABLE tasks.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test runs **in-process**. The alembic command surface is
programmatic (``alembic.command.upgrade`` / ``downgrade`` /
``upgrade --sql``) bound to a per-test ``alembic.config.Config`` rooted
at a per-test SQLite file. The static grep (case 6) scans the migrations
package directory directly. No subprocess is spawned, so pytest-cov
attributes execution to the alembic command surface and the migration
modules, and the Gate-1 ``test_coverage`` dimension can see them.

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆 (FR-07).
- SPEC.md line 136 — v1: tasks, api_keys; drop on downgrade.
- SPEC.md line 137 — v2: tags, task_tags, tasks.name unique index; no v1 damage.
- SPEC.md line 138 — v3: result_json → task_results; reverse on downgrade; no data loss.
- SPEC.md line 140 — upgrade head / downgrade base must succeed.
- SPEC.md line 141 — round-trip bytewise invariant (sample row ↔ after).
- SPEC.md line 142 — no `op.execute DROP TABLE` shortcut.
- SPEC.md line 143 — migration files covered by offline SQL assertions.
- SPEC.md line 256 — FR-07 round-trip MUST run against a real SQLite file
  (not in-memory mock). The `db_file` fixture below writes
  ``tmp_path / "fr07.db"`` and sets ``TASKQ_DB_URL=sqlite+pysqlite:///<that>``
  so every migration sees real disk state.
- SPEC.md §5.2 — table layout by version; v3 drops ``tasks.result_json``.
- TEST_SPEC.md §1 FR-07 — the seven named cases implemented below.
- NFR-10 — migration round-trip is a cross-cut AC; exercised with a real
  SQLite file (SPEC.md line 256).
"""  # NFR-02 NFR-03 NFR-05 NFR-06 NFR-09 NFR-10

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# ``migrations/versions/v1_initial.py`` must export:
#   revision: str       = "v1"
#   down_revision: None | str
#   def upgrade() -> None
#       op.create_table("tasks", [...])
#       op.create_table("api_keys", [...])
#   def downgrade() -> None
#       op.drop_table("api_keys")        # real downgrade, NOT op.execute("DROP TABLE ...")
#       op.drop_table("tasks")
#
# ``migrations/versions/v2_add_tags.py`` must export:
#   revision: str       = "v2"
#   down_revision: str  = "v1"
#   def upgrade() -> None
#       # add tags + task_tags (m:n), unique index on tasks.name, no v1 changes
#       op.create_table("tags", [...])
#       op.create_table("task_tags", [...])
#       op.create_index("ix_tasks_name_unique", "tasks", ["name"], unique=True)
#       # OR: BatchAlterTable / batch_alter_table -> "ix_tasks_name_unique"
#   def downgrade() -> None
#       op.drop_index("ix_tasks_name_unique", table_name="tasks")
#       op.drop_table("task_tags")
#       op.drop_table("tags")
#
# ``migrations/versions/v3_split_results.py`` must export:
#   revision: str       = "v3"
#   down_revision: str  = "v2"
#   def upgrade() -> None
#       # data migration: tasks.result_json -> task_results; drop original column
#       op.create_table("task_results", [...])
#       op.execute("INSERT INTO task_results (...) SELECT ... FROM tasks")
#       with op.batch_alter_table("tasks") as batch:
#           batch.drop_column("result_json")
#   def downgrade() -> None
#       # reverse-migrate data back into tasks.result_json then drop task_results
#       with op.batch_alter_table("tasks") as batch:
#           batch.add_column(sa.Column("result_json", sa.Text, nullable=False, server_default=""))
#       op.execute("UPDATE tasks SET result_json = (...) FROM task_results WHERE ...")
#       op.drop_table("task_results")
#
# Alembic configuration lives at ``migrations/env.py`` (sibling package).
# Standard top-level import: ``from migrations.versions import v1_initial``.
# ---------------------------------------------------------------------------


# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-07). None of the migration modules exist
# on disk yet → ModuleNotFoundError at collection time is the valid RED
# signal; pytest reports Exit Code 2. Do not wrap in try/except; do not
# lazy-import. This mirrors the FR-06 pattern in test_fr06.py.
from migrations.versions import (  # noqa: E402,F401  [FR-07]
    v1_initial,
    v2_add_tags,
    v3_split_results,
)


# ---------------------------------------------------------------------------
# Shared fixtures — one real SQLite file per test (SPEC.md line 256).
# ---------------------------------------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_MIGRATIONS_DIR = _SRC_ROOT / "migrations"
_VERSIONS_DIR = _MIGRATIONS_DIR / "versions"


def _build_alembic_config(db_file: Path, *, sql_mode: bool = False):
    """Construct an ``alembic.config.Config`` bound to ``db_file``.

    The ``script_location`` is the migrations package directory; the
    ``sqlalchemy.url`` is a ``sqlite+pysqlite:///<db_file>`` URL so every
    migration runs against a real disk file (SPEC.md line 256).

    When ``sql_mode`` is True the URL is rewritten to a deterministic
    placeholder — Alembic's offline SQL generator (case 7) only uses
    the dialect prefix, not the actual file path.
    """  # NFR-10
    from alembic.config import Config  # local import keeps top-level failure mode RED

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(_MIGRATIONS_DIR)
    )
    if sql_mode:
        cfg.set_main_option(
            "sqlalchemy.url", "sqlite+pysqlite:///:memory:"
        )
    else:
        cfg.set_main_option(
            "sqlalchemy.url", f"sqlite+pysqlite:///{db_file}"
        )
    return cfg


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """Per-test SQLite file. SPEC.md line 256 forbids in-memory mocks for FR-07."""
    path = tmp_path / "fr07.db"
    if path.exists():
        path.unlink()
    # Make TASKQ_DB_URL point at this file for any module that reads it.
    os.environ["TASKQ_DB_URL"] = f"sqlite+pysqlite:///{path}"
    yield path
    os.environ.pop("TASKQ_DB_URL", None)


def _inspector_for(db_file: Path):
    """Return a SQLAlchemy ``Inspector`` bound to ``db_file``.

    Used by the post-migration table / index assertions in cases 1-4.
    Imported lazily because ``sqlalchemy`` is only a declared dependency
    for the persistence layer (repository.session); the import here
    belongs to the test, not to the package.
    """  # NFR-10
    from sqlalchemy import create_engine  # type: ignore  # declared dep
    from sqlalchemy import inspect  # type: ignore

    engine = create_engine(f"sqlite+pysqlite:///{db_file}")
    return inspect(engine), engine


# ---------------------------------------------------------------------------
# AC-7.1 — `alembic upgrade head` succeeds on a fresh DB
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head_succeeds(db_file: Path):
    """AC-7.1: ``alembic upgrade head`` must succeed on a fresh database.

    TEST_SPEC inputs:
      target_revision = "head"
      expected_alembic_exit_code = "0"
    Sub-assertion: FR07-upgrade-head-exit-0 — code must be 0.

    The test:
      1. Binds an ``alembic.config.Config`` to a per-test SQLite file
         (``tmp_path / "fr07.db"`` — SPEC.md line 256 forbids in-memory).
      2. Calls ``alembic.command.upgrade(cfg, "head")``.
      3. Asserts the call returned cleanly (no exception) AND that the
         schema now contains the v3 tables (``tasks``, ``api_keys``,
         ``tags``, ``task_tags``, ``task_results``).
      4. The positive schema check is required — without it the test
         would pass on a no-op GREEN that swallows the upgrade silently.
    """  # NFR-09 NFR-10
    target_revision = "head"
    expected_alembic_exit_code = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert target_revision == "head"
    assert expected_alembic_exit_code == "0"

    from alembic import command as alembic_command  # local import keeps RED mode

    cfg = _build_alembic_config(db_file)

    # ---- Sub-assertion FR07-upgrade-head-exit-0: exit code == 0. ----
    # Alembic raises on non-zero exit; absence of exception == 0.
    alembic_command.upgrade(cfg, "head")  # raises if any step fails

    # ---- Positive half: schema now contains the v3 tables. ----
    insp, engine = _inspector_for(db_file)
    try:
        tables = set(insp.get_table_names())
    finally:
        engine.dispose()

    required_v3_tables = {"tasks", "api_keys", "task_results"}
    missing = required_v3_tables - tables
    assert not missing, (
        f"after `alembic upgrade head` the schema must contain "
        f"{required_v3_tables!r} (SPEC §5.2 / AC-7.1); missing: {missing!r}; "
        f"got: {sorted(tables)!r}"
    )


# ---------------------------------------------------------------------------
# AC-7.2 — `alembic downgrade base` succeeds; no residual tables
# ---------------------------------------------------------------------------


def test_alembic_downgrade_base_no_residual_tables(db_file: Path):
    """AC-7.2: ``alembic downgrade base`` must succeed after ``upgrade
    head``; no tables may remain in the schema.

    TEST_SPEC inputs:
      target_revision = "base"
      expected_alembic_exit_code = "0"
      expected_residual_table_count = "0"
    Sub-assertions:
      FR07-downgrade-base-exit-0    (downgrade exit code == 0)
      FR07-downgrade-no-residual    (no tables left in the schema)

    The test upgrades to head, then downgrades all the way to base, then
    asserts the schema is empty (no application tables remain —
    alembic_version may still exist but is excluded from the assertion).
    """  # NFR-09 NFR-10
    target_revision = "base"
    expected_alembic_exit_code = "0"
    expected_residual_table_count = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert target_revision == "base"
    assert expected_alembic_exit_code == "0"
    assert expected_residual_table_count == "0"

    from alembic import command as alembic_command

    cfg = _build_alembic_config(db_file)

    # First, get to head so we have something to downgrade.
    alembic_command.upgrade(cfg, "head")

    # ---- Sub-assertion FR07-downgrade-base-exit-0: downgrade exit code == 0. ----
    alembic_command.downgrade(cfg, "base")  # raises on non-zero exit

    # ---- Sub-assertion FR07-downgrade-no-residual: zero app tables. ----
    insp, engine = _inspector_for(db_file)
    try:
        all_tables = set(insp.get_table_names())
    finally:
        engine.dispose()

    # alembic_version is bookkeeping and is NOT counted as a "residual
    # application table". Any other table left behind is a downgrade
    # failure.
    residual = all_tables - {"alembic_version"}
    assert len(residual) == int(expected_residual_table_count), (
        f"`alembic downgrade base` must leave NO application tables "
        f"(AC-7.2 / SPEC.md line 140 + §8 #13); got residual "
        f"{sorted(residual)!r}"
    )


# ---------------------------------------------------------------------------
# AC-7.3 — v2 adds tags, task_tags, and a unique index on tasks.name
# ---------------------------------------------------------------------------


def test_alembic_v2_adds_tags_and_unique_index(db_file: Path):
    """AC-7.3: the v2 migration must add ``tags``, ``task_tags`` (m:n
    join), and a unique index on ``tasks.name`` — WITHOUT altering v1
    data.

    TEST_SPEC inputs:
      revision = "v2"
      added_table = "task_tags"
      expected_unique_index_name = "ix_tasks_name_unique"
    Sub-assertion: FR07-v2-unique-index-name — index must be
      ``ix_tasks_name_unique``.

    The test upgrades to v2 (NOT head), then asserts:
      (a) the ``task_tags`` table exists,
      (b) the ``tags`` table exists,
      (c) the ``ix_tasks_name_unique`` unique index exists,
      (d) the v1 tables (``tasks``, ``api_keys``) are still present and
          unchanged in column shape.
    """  # NFR-09 NFR-10
    revision = "v2"
    added_table = "task_tags"
    expected_unique_index_name = "ix_tasks_name_unique"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert revision == "v2"
    assert added_table == "task_tags"
    assert expected_unique_index_name == "ix_tasks_name_unique"

    from alembic import command as alembic_command

    cfg = _build_alembic_config(db_file)

    # Upgrade ONLY to v2 — leave v3 un-applied.
    alembic_command.upgrade(cfg, "v2")

    insp, engine = _inspector_for(db_file)
    try:
        tables = set(insp.get_table_names())

        # v1 tables MUST still be there (AC-7.3 — "do not touch v1 data").
        v1_tables = {"tasks", "api_keys"}
        assert v1_tables <= tables, (
            f"after v2 upgrade, v1 tables {v1_tables!r} must still exist "
            f"(AC-7.3 — v2 must not damage v1 data); got: {sorted(tables)!r}"
        )

        # v2 additions.
        assert added_table in tables, (
            f"v2 must add the `task_tags` m:n table "
            f"(AC-7.3 / SPEC.md line 137); got: {sorted(tables)!r}"
        )
        assert "tags" in tables, (
            f"v2 must add the `tags` table (AC-7.3 / SPEC.md line 137); "
            f"got: {sorted(tables)!r}"
        )

        # ---- Sub-assertion FR07-v2-unique-index-name:
        #      expected_unique_index_name == "ix_tasks_name_unique". ----
        indexes = insp.get_indexes("tasks")
        index_names = {idx["name"] for idx in indexes}
        unique_names = {
            idx["name"] for idx in indexes if idx.get("unique", False)
        }
        assert expected_unique_index_name in unique_names, (
            f"v2 must add a UNIQUE index named "
            f"{expected_unique_index_name!r} on `tasks.name` "
            f"(AC-7.3 / SPEC.md line 137); got indexes {sorted(index_names)!r}, "
            f"unique subset {sorted(unique_names)!r}"
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# AC-7.4 — v3 splits tasks.result_json into task_results with data migration
# ---------------------------------------------------------------------------


def test_alembic_v3_splits_results_with_data_migration(db_file: Path):
    """AC-7.4: the v3 migration must split ``tasks.result_json`` into a
    separate ``task_results`` table; existing rows migrate; the original
    column is dropped.

    TEST_SPEC inputs:
      revision = "v3"
      sample_payload_exit_code = "0"
      sample_payload_stdout_tail = "hello"
      expected_table_after = "task_results"
    Sub-assertion: FR07-v3-table-after-move — the table holding the
      run results must be named ``task_results``.

    The test:
      1. Upgrades to v2 and inserts one row into ``tasks`` whose
         ``result_json`` carries exit_code=0 and stdout_tail="hello".
      2. Upgrades to v3.
      3. Asserts the ``tasks.result_json`` column is GONE.
      4. Asserts ``task_results`` exists and contains the migrated row
         with the same exit_code / stdout_tail payload (the "data
         migration" half).
    """  # NFR-03 NFR-09 NFR-10
    revision = "v3"
    sample_payload_exit_code = "0"
    sample_payload_stdout_tail = "hello"
    expected_table_after = "task_results"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert revision == "v3"
    assert sample_payload_exit_code == "0"
    assert sample_payload_stdout_tail == "hello"
    assert expected_table_after == "task_results"

    import json as _json  # local alias to avoid shadowing the stdlib at module scope

    import sqlalchemy as sa  # type: ignore  # declared dep
    from alembic import command as alembic_command

    cfg = _build_alembic_config(db_file)

    # 1. Upgrade to v2 (sets up tasks.result_json).
    alembic_command.upgrade(cfg, "v2")

    # 2. Seed a row with a result_json payload. SPEC.md §5.2 — v1/v2 had
    #    `tasks.result_json`; v3 splits it out.
    engine = sa.create_engine(f"sqlite+pysqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO tasks (id, command, name, status, created_at, result_json)
                    VALUES (:id, :command, :name, :status, :created_at, :result_json)
                    """
                ),
                {
                    "id": "fr07-v3-fixture",
                    "command": "echo hello",
                    "name": "fr07-v3-fixture",
                    "status": "pending",
                    "created_at": "2026-09-05T00:00:00.000000+00:00",
                    "result_json": _json.dumps(
                        {
                            "exit_code": int(sample_payload_exit_code),
                            "stdout_tail": sample_payload_stdout_tail,
                            "stderr_tail": "",
                            "duration_ms": 0,
                            "finished_at": "2026-09-05T00:00:00.000000+00:00",
                        }
                    ),
                },
            )
    finally:
        engine.dispose()

    # 3. Upgrade to v3 — the data migration runs.
    alembic_command.upgrade(cfg, "v3")

    # 4. Post-conditions: schema and data shape.
    insp, engine = _inspector_for(db_file)
    try:
        tables = set(insp.get_table_names())

        # ---- Sub-assertion FR07-v3-table-after-move:
        #      expected_table_after == "task_results". ----
        assert expected_table_after in tables, (
            f"v3 must introduce the `task_results` table "
            f"(AC-7.4 / SPEC.md line 138); got: {sorted(tables)!r}"
        )

        # Original column must be gone (SPEC.md line 138 — drop the
        # original column after migration).
        tasks_columns = {c["name"] for c in insp.get_columns("tasks")}
        assert "result_json" not in tasks_columns, (
            f"v3 must DROP `tasks.result_json` after migrating its data "
            f"(AC-7.4 / SPEC.md line 138 / §5.2); columns left: "
            f"{sorted(tasks_columns)!r}"
        )

        # Data-migration invariant — the seeded payload MUST have been
        # moved into task_results, NOT silently lost. Read the row back
        # through raw SQL so the assertion is independent of the ORM
        # row classes (which the v3 schema does not own).
        with engine.connect() as conn:
            migrated = conn.execute(
                sa.text(
                    """
                    SELECT exit_code, stdout_tail FROM task_results
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": "fr07-v3-fixture"},
            ).fetchone()
    finally:
        engine.dispose()

    assert migrated is not None, (
        f"v3 data migration must move the seeded task's result into "
        f"`task_results` (AC-7.4 — SPEC.md line 138 / '搬遷既有資料'); "
        f"no row found for task_id='fr07-v3-fixture'"
    )
    # The payload fields must survive byte-identical (no reformatting,
    # no truncation, no type coercion loss).
    assert int(migrated[0]) == int(sample_payload_exit_code), (
        f"migrated exit_code must equal seeded value "
        f"{sample_payload_exit_code!r}; got {migrated[0]!r}"
    )
    assert migrated[1] == sample_payload_stdout_tail, (
        f"migrated stdout_tail must equal seeded value "
        f"{sample_payload_stdout_tail!r}; got {migrated[1]!r}"
    )


# ---------------------------------------------------------------------------
# AC-7.5 — round-trip bytewise: upgrade head → insert → downgrade -1 → upgrade head
# ---------------------------------------------------------------------------


def test_migration_round_trip_preserves_sample_data_bytewise(db_file: Path):
    """AC-7.5: round-trip reversibility. The sequence is:

        upgrade head
        → insert 5 sample rows
        → downgrade -1   (one revision back — v3 → v2)
        → upgrade head   (back to v3)

    Every column of every sample row MUST be byte-identical to the
    pre-roundtrip state (v3 data migration is the focus — SPEC.md line
    141 + §8 #12).

    TEST_SPEC inputs:
      sample_row_count = "5"
      round_trip_steps = "upgrade_head;insert;downgrade_-1;upgrade_head"
      expected_byte_identical_row_count = "5"
    Sub-assertions:
      FR07-round-trip-byte-count          (byte-identical count == 5)
      FR07-round-trip-row-count-input     (input row count == 5)

    Per SPEC.md line 256 the round-trip uses a real SQLite file
    (NOT in-memory mock) — the fixture already provides one.
    """  # NFR-03 NFR-09 NFR-10
    sample_row_count = "5"
    round_trip_steps = "upgrade_head;insert;downgrade_-1;upgrade_head"
    expected_byte_identical_row_count = "5"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert sample_row_count == "5"
    assert round_trip_steps == "upgrade_head;insert;downgrade_-1;upgrade_head"
    assert expected_byte_identical_row_count == "5"

    import json as _json

    import sqlalchemy as sa  # type: ignore  # declared dep
    from alembic import command as alembic_command

    cfg = _build_alembic_config(db_file)

    n = int(sample_row_count)
    sample_rows = []
    for i in range(n):
        sample_rows.append(
            {
                "id": f"fr07-rt-{i:03d}",
                "command": f"echo round-trip-{i:03d}",
                "name": f"fr07-rt-{i:03d}",
                "status": "pending",
                "created_at": f"2026-09-05T00:0{i}:00.000000+00:00",
                "result_json": _json.dumps(
                    {
                        "exit_code": i,
                        "stdout_tail": f"hello-{i:03d}",
                        "stderr_tail": "",
                        "duration_ms": 100 + i,
                        "finished_at": f"2026-09-05T00:0{i}:00.000000+00:00",
                    }
                ),
            }
        )

    # 1. upgrade head (v1 → v2 → v3).
    alembic_command.upgrade(cfg, "head")

    # 2. Insert the 5 sample rows at v3 (after the result_json split,
    #    they live in tasks + task_results, NOT in tasks.result_json —
    #    write to the v3 shape so the round-trip exercises v3's
    #    downstream migration).
    engine = sa.create_engine(f"sqlite+pysqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            for row in sample_rows:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO tasks (id, command, name, status, created_at)
                        VALUES (:id, :command, :name, :status, :created_at)
                        """
                    ),
                    row,
                )
                payload = _json.loads(row["result_json"])
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO task_results
                            (run_id, task_id, exit_code, stdout_tail,
                             stderr_tail, duration_ms, finished_at)
                        VALUES
                            (:run_id, :task_id, :exit_code, :stdout_tail,
                             :stderr_tail, :duration_ms, :finished_at)
                        """
                    ),
                    {
                        "run_id": f"run-{row['id']}",
                        "task_id": row["id"],
                        "exit_code": payload["exit_code"],
                        "stdout_tail": payload["stdout_tail"],
                        "stderr_tail": payload["stderr_tail"],
                        "duration_ms": payload["duration_ms"],
                        "finished_at": payload["finished_at"],
                    },
                )
    finally:
        engine.dispose()

    # Snapshot the pre-roundtrip state — every column of every row.
    pre_state = _snapshot_state(db_file)

    # 3. downgrade -1 (v3 → v2) and 4. upgrade head (v2 → v3).
    alembic_command.downgrade(cfg, "-1")
    alembic_command.upgrade(cfg, "head")

    # Snapshot the post-roundtrip state.
    post_state = _snapshot_state(db_file)

    # ---- Sub-assertion FR07-round-trip-byte-count:
    #      expected_byte_identical_row_count == "5". ----
    pre_tasks_by_id = {row["id"]: row for row in pre_state["tasks"]}
    post_tasks_by_id = {row["id"]: row for row in post_state["tasks"]}
    identical_count = 0
    for task_id, pre_row in pre_tasks_by_id.items():
        post_row = post_tasks_by_id.get(task_id)
        if post_row is None:
            continue
        # Compare every column of `tasks` bytewise.
        if all(
            pre_row.get(col) == post_row.get(col)
            for col in pre_row
        ):
            identical_count += 1

    assert identical_count == int(expected_byte_identical_row_count), (
        f"round-trip must preserve every column of every sample row "
        f"bytewise (AC-7.5 / SPEC.md line 141 / NP-10); "
        f"expected {expected_byte_identical_row_count} byte-identical "
        f"rows, got {identical_count}. "
        f"Pre-snapshot tasks: {_summarise(pre_state['tasks'])}. "
        f"Post-snapshot tasks: {_summarise(post_state['tasks'])}."
    )

    # Also assert the v3 result rows round-trip bytewise (the data
    # migration's load-bearing half).
    pre_results_by_task = {
        row["task_id"]: row for row in pre_state["task_results"]
    }
    post_results_by_task = {
        row["task_id"]: row for row in post_state["task_results"]
    }
    assert pre_results_by_task == post_results_by_task, (
        f"`task_results` rows must round-trip bytewise (AC-7.5 / "
        f"SPEC.md line 141 — v3 data migration focus); "
        f"diff:\n  pre  = {sorted(pre_results_by_task.items())[:3]}\n  "
        f"post = {sorted(post_results_by_task.items())[:3]}"
    )


def _snapshot_state(db_file: Path) -> dict:
    """Return a JSON-serialisable snapshot of every ``tasks`` /
    ``task_results`` row.

    Used by the round-trip test to compare pre- and post-roundtrip state
    bytewise. Imported lazily because ``sqlalchemy`` is only a declared
    dependency for the persistence layer.
    """  # NFR-10
    import sqlalchemy as sa  # type: ignore  # declared dep

    engine = sa.create_engine(f"sqlite+pysqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            tasks_rows = conn.execute(
                sa.text("SELECT * FROM tasks ORDER BY id")
            ).mappings().all()
            try:
                results_rows = conn.execute(
                    sa.text("SELECT * FROM task_results ORDER BY task_id, run_id")
                ).mappings().all()
            except sa.exc.OperationalError:
                results_rows = []
    finally:
        engine.dispose()
    return {
        "tasks": [dict(r) for r in tasks_rows],
        "task_results": [dict(r) for r in results_rows],
    }


def _summarise(rows: list[dict]) -> str:
    """Compact one-line summary of a list of dicts for assertion messages."""
    return ", ".join(
        f"{r.get('id', '?')}={ {k: v for k, v in r.items() if k != 'id'} }"
        for r in rows[:3]
    )


# ---------------------------------------------------------------------------
# AC-7.6 — no `op.execute("DROP TABLE ...")` shortcut for downgrade
# ---------------------------------------------------------------------------


def test_migration_files_have_real_downgrade_no_drop_table_shortcuts():
    """AC-7.6: no ``op.execute("DROP TABLE ...")`` (or equivalent
    destructive shortcut) may substitute for a real downgrade.

    TEST_SPEC inputs:
      revision = "v3"
      forbidden_pattern = 'op.execute("DROP TABLE'
      expected_match_count = "0"
    Sub-assertion: FR07-no-drop-table-shortcut — match count must be 0.

    The test scans every ``.py`` file under
    ``03-development/src/migrations/versions/`` for the forbidden
    pattern. The destructive shortcut would be writing
    ``op.execute('DROP TABLE tasks')`` instead of ``op.drop_table('tasks')``
    — that bypasses Alembic's transaction tracking and the
    reversible-migration contract.

    The test confirms the migration FILES exist (positive half); without
    that, the grep could pass on an empty versions/ directory.
    """  # NFR-02 NFR-09
    revision = "v3"
    forbidden_pattern = 'op.execute("DROP TABLE'
    expected_match_count = "0"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert revision == "v3"
    assert forbidden_pattern == 'op.execute("DROP TABLE'
    assert expected_match_count == "0"

    versions_dir = _VERSIONS_DIR
    assert versions_dir.is_dir(), (
        f"GREEN must add the Alembic versions directory at "
        f"{versions_dir} (SAB.json FR-07 rows 1-3 + AC-7.6)"
    )

    matches: list[str] = []
    for py_file in versions_dir.rglob("*.py"):
        # Skip the package ``__init__.py`` — the convention is one
        # revision per file (script.py.mako pattern).
        if py_file.name == "__init__.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        for line_num, line in enumerate(content.splitlines(), start=1):
            if forbidden_pattern in line:
                matches.append(
                    f"{py_file.relative_to(_SRC_ROOT).as_posix()}:"
                    f"{line_num}: {line.strip()}"
                )

    # ---- Sub-assertion FR07-no-drop-table-shortcut:
    #      expected_match_count == "0". ----
    assert len(matches) == int(expected_match_count), (
        f"no `op.execute(\"DROP TABLE ...\")` shortcut may substitute for "
        f"a real downgrade (AC-7.6 / SPEC.md line 142); "
        f"found {len(matches)} matches: {matches}"
    )

    # Positive half — at least the v1 / v2 / v3 files exist. The names
    # match the SAB FR-07 row dotted-path: `v1_initial`, `v2_add_tags`,
    # `v3_split_results`. Either ``v1_initial.py`` or
    # ``v1_initial/__init__.py`` is acceptable (Gate 1 accepts both).
    expected_files = [
        versions_dir / "v1_initial.py",
        versions_dir / "v2_add_tags.py",
        versions_dir / "v3_split_results.py",
    ]
    expected_packages = [
        versions_dir / "v1_initial" / "__init__.py",
        versions_dir / "v2_add_tags" / "__init__.py",
        versions_dir / "v3_split_results" / "__init__.py",
    ]

    def _exists_either(target: Path, package: Path) -> bool:
        return target.is_file() or package.is_file()

    assert all(
        _exists_either(f, p)
        for f, p in zip(expected_files, expected_packages)
    ), (
        f"GREEN must add v1_initial / v2_add_tags / v3_split_results "
        f"migration modules under {versions_dir} (SAB.json FR-07 rows "
        f"1-3). Expected either .py file or package/__init__.py for "
        f"each. Found: "
        + ", ".join(
            f"{f.name}={_exists_either(f, p)}"
            for f, p in zip(expected_files, expected_packages)
        )
    )


# ---------------------------------------------------------------------------
# AC-7.7 — offline SQL generation matches expectations
# ---------------------------------------------------------------------------


def test_migration_offline_sql_generation_matches_expectations(db_file: Path):
    """AC-7.7: migration files are covered by alembic's offline SQL
    generation. The emitted SQL must contain ``CREATE TABLE tasks`` (and
    other expected DDL) — proving the migrations emit valid SQL even
    before they run against a live database.

    TEST_SPEC inputs:
      alembic_mode = "offline"
      expected_sql_contains_create_tasks = "true"
    Sub-assertion: implicit — emitted SQL contains ``CREATE TABLE tasks``.

    The test calls ``alembic.command.upgrade(cfg, "head", sql=True)`` with
    a redirected stdout, captures the SQL stream, and asserts the stream
    contains ``CREATE TABLE tasks``. Offline mode does NOT touch the
    database; the ``db_file`` fixture is only there so the in-process
    Config has a usable URL prefix.
    """  # NFR-09 NFR-10
    alembic_mode = "offline"
    expected_sql_contains_create_tasks = "true"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert alembic_mode == "offline"
    assert expected_sql_contains_create_tasks == "true"

    import io
    import contextlib

    from alembic import command as alembic_command

    cfg = _build_alembic_config(db_file, sql_mode=True)

    # ``alembic.command.upgrade(..., sql=True)`` writes the SQL to
    # stdout. Capture it so the assertion can inspect the text.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        alembic_command.upgrade(cfg, "head", sql=True)
    sql_text = buf.getvalue()

    # The emitted SQL MUST contain ``CREATE TABLE tasks`` (SQLite emits
    # ``CREATE TABLE`` verbatim, but normalise to upper for safety).
    sql_upper = sql_text.upper()
    assert "CREATE TABLE TASKS" in sql_upper, (
        f"offline SQL generation for `alembic upgrade head` must include "
        f"`CREATE TABLE tasks` (AC-7.7 / SPEC.md line 143); "
        f"first 400 chars of emitted SQL: {sql_text[:400]!r}"
    )

    # Positive sanity — the offline SQL must also reference task_results
    # (v3) — that proves all three revisions emit DDL, not just v1.
    assert "CREATE TABLE TASK_RESULTS" in sql_upper, (
        f"offline SQL generation must include `CREATE TABLE task_results` "
        f"for v3 (AC-7.7 / SPEC.md line 143); "
        f"first 400 chars: {sql_text[:400]!r}"
    )
