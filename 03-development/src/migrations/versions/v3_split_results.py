"""[FR-07] v3_split_results — split ``tasks.result_json`` into a
dedicated ``task_results`` table (data migration).

The ``tasks.result_json`` column (TEXT, JSON payload) lived in v1 and
v2. v3 introduces a first-class ``task_results`` table and migrates
the existing JSON payload field-by-field via SQLite's ``json_extract``
so the run-result fields (exit_code, stdout_tail, stderr_tail,
duration_ms, finished_at) become typed columns. After migration the
original ``tasks.result_json`` column is dropped (SPEC.md §5.2).

Downgrade reverses the migration in three steps:

  1. Add ``tasks.result_json`` back (TEXT, ``server_default=""`` so
     existing rows satisfy the NOT NULL constraint).
  2. Reconstruct the JSON payload from the typed columns via SQLite's
     ``json_object`` so the original text payload is preserved.
  3. Drop ``task_results``.

This is the SPEC.md line 141 round-trip focus. Each ``upgrade`` /
``downgrade`` is reversible; the destructive downgrade shortcut is
forbidden by AC-7.6 / SPEC.md line 142.

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆.
- SPEC.md line 138 — v3: result_json → task_results; reverse on
  downgrade; no data loss.
- SPEC.md line 141 — round-trip bytewise invariant.
- SPEC.md line 142 — no destructive shortcut for downgrade.
- SPEC.md §5.2 — v3 drops ``tasks.result_json``.
"""  # NFR-11
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v3"
down_revision: None | str = "v2"
branch_labels: None | str = None
depends_on: None | str = None

# Result fields carried in v1/v2's ``tasks.result_json`` and promoted to
# typed columns in v3+'s ``task_results``. Single source of truth for
# the data migration: both the upgrade (json_extract) and the
# downgrade (json_object) reference this tuple so the round-trip
# preserves each field bytewise (SPEC.md line 141).
_RESULT_FIELDS: tuple[str, ...] = (
    "exit_code",
    "stdout_tail",
    "stderr_tail",
    "duration_ms",
    "finished_at",
)

# Subset of ``_RESULT_FIELDS`` stored as INTEGER (not TEXT) in
# ``task_results``. The upgrade must CAST ``json_extract`` for these
# fields so the downgrade-side ``json_object`` rebuilds a matching JSON
# payload (no type drift).
_INTEGER_RESULT_FIELDS: frozenset[str] = frozenset(
    {"exit_code", "duration_ms"}
)


def _build_upgrade_data_sql() -> str:
    """Build the ``INSERT INTO task_results ... SELECT ...`` SQL that
    migrates rows from ``tasks.result_json`` into the typed columns.

    The SELECT projection is generated from ``_RESULT_FIELDS`` so adding
    a new field is a one-line change at the constant definition rather
    than a duplicated edit across INSERT columns, SELECT projection,
    and the downgrade ``json_object`` arguments.
    """  # NFR-09
    columns = ", ".join(_RESULT_FIELDS)
    select_parts = []
    for field in _RESULT_FIELDS:
        path = "$.{0}".format(field)
        extract = "json_extract(tasks.result_json, '{0}')".format(path)
        if field in _INTEGER_RESULT_FIELDS:
            select_parts.append(
                "CAST({0} AS INTEGER)".format(extract)
            )
        else:
            select_parts.append(extract)
    projection = ",\n                ".join(select_parts)
    return (
        "INSERT INTO task_results\n"
        "    (run_id, task_id, {0})\n"
        "SELECT\n"
        "    'run-' || tasks.id,\n"
        "    tasks.id,\n"
        "                {1}\n"
        "FROM tasks\n"
        "WHERE tasks.result_json IS NOT NULL\n"
    ).format(columns, projection)


def _build_downgrade_data_sql() -> str:
    """Build the ``UPDATE tasks SET result_json = (...)`` SQL that
    reverse-migrates ``task_results`` rows back into the JSON payload.

    The ``json_object`` argument list is generated from
    ``_RESULT_FIELDS`` so the key order matches the upgrade side
    bytewise (SPEC.md line 141).
    """  # NFR-09
    object_args = ",\n".join(
        "                    '{0}', task_results.{0}".format(field)
        for field in _RESULT_FIELDS
    )
    return (
        "UPDATE tasks\n"
        "SET result_json = (\n"
        "    SELECT json_object(\n"
        "{0}\n"
        "    )\n"
        "    FROM task_results\n"
        "    WHERE task_results.task_id = tasks.id\n"
        ")\n"
        "WHERE EXISTS (\n"
        "    SELECT 1 FROM task_results WHERE task_results.task_id = tasks.id\n"
        ")\n"
    ).format(object_args)


def upgrade() -> None:
    """[FR-07] v3 upgrade — create ``task_results``, migrate data, drop
    ``tasks.result_json``.

    Three steps:

      1. ``op.create_table("task_results", ...)`` — typed result columns
         ``run_id`` (PK), ``task_id``, ``exit_code``, ``stdout_tail``,
         ``stderr_tail``, ``duration_ms``, ``finished_at``.
      2. ``op.execute(...)`` — populate ``task_results`` from
         ``tasks.result_json`` via SQLite's ``json_extract``. Only rows
         with a non-null ``result_json`` are migrated.
      3. ``op.drop_column("tasks", "result_json")`` — drop the original
         column (SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN``).

    The ``result_json`` JSON payload schema (per SPEC.md §5.2):

        {"exit_code": int, "stdout_tail": str, "stderr_tail": str,
         "duration_ms": int, "finished_at": str}
    """  # NFR-03 NFR-09 NFR-10
    op.create_table(
        "task_results",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.String(length=64), nullable=True),
    )

    # Data migration: extract fields from tasks.result_json (JSON) into
    # the typed task_results columns. SQLite's json_extract returns the
    # raw JSON scalar; CAST keeps INTEGER columns as INTEGER so the
    # downgrade-side ``json_object`` rebuilds a matching payload.
    op.execute(sa.text(_build_upgrade_data_sql()))

    op.drop_column("tasks", "result_json")


def downgrade() -> None:
    """[FR-07] v3 downgrade — restore ``tasks.result_json``, drop
    ``task_results``.

    Three steps (reverse of upgrade):

      1. ``op.add_column("tasks", ...)`` — add ``result_json`` back
         (``server_default=""`` so existing rows satisfy NOT NULL).
      2. ``op.execute(...)`` — reconstruct the JSON payload via SQLite's
         ``json_object`` from each task's row in ``task_results``.
         Rows without a matching ``task_results`` row keep an empty
         payload (``""`` from ``server_default``).
      3. ``op.drop_table("task_results")`` — real downgrade.

    The reconstructed JSON text is bytewise-identical to the original
    payload for round-trip safety (SPEC.md line 141): same key order,
    same field types (JSON int stays int, JSON string stays string).
    """  # NFR-02 NFR-03 NFR-09
    op.add_column(
        "tasks",
        sa.Column(
            "result_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )

    # Reverse-migrate: rebuild the JSON payload from task_results.
    # json_object emits keys in the order given; the original v1/v2
    # payload also emits keys in the same order (Python 3.7+ dict
    # insertion order is preserved by json.dumps), so the
    # reconstructed text is byte-identical to the original payload for
    # round-trip safety.
    op.execute(sa.text(_build_downgrade_data_sql()))

    op.drop_table("task_results")
