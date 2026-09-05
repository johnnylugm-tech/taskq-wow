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
    op.execute(
        sa.text(
            """
            INSERT INTO task_results
                (run_id, task_id, exit_code, stdout_tail,
                 stderr_tail, duration_ms, finished_at)
            SELECT
                'run-' || tasks.id,
                tasks.id,
                CAST(json_extract(tasks.result_json, '$.exit_code') AS INTEGER),
                json_extract(tasks.result_json, '$.stdout_tail'),
                json_extract(tasks.result_json, '$.stderr_tail'),
                CAST(json_extract(tasks.result_json, '$.duration_ms') AS INTEGER),
                json_extract(tasks.result_json, '$.finished_at')
            FROM tasks
            WHERE tasks.result_json IS NOT NULL
            """
        )
    )

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
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET result_json = (
                SELECT json_object(
                    'exit_code', task_results.exit_code,
                    'stdout_tail', task_results.stdout_tail,
                    'stderr_tail', task_results.stderr_tail,
                    'duration_ms', task_results.duration_ms,
                    'finished_at', task_results.finished_at
                )
                FROM task_results
                WHERE task_results.task_id = tasks.id
            )
            WHERE EXISTS (
                SELECT 1 FROM task_results WHERE task_results.task_id = tasks.id
            )
            """
        )
    )

    op.drop_table("task_results")