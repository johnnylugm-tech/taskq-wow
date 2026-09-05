"""[FR-07] v1_initial — initial schema: ``tasks`` + ``api_keys``.

Establishes the foundational tables defined by SPEC.md §5.2. The
``tasks.result_json`` column is part of v1 (and v2); v3 splits it out
into ``task_results``.

Downgrade reverses the upgrade by dropping each table individually
via Alembic's ``op.drop_table`` — the destructive shortcut is
explicitly forbidden by AC-7.6 / SPEC.md line 142.

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆.
- SPEC.md line 136 — v1: tasks, api_keys; drop on downgrade.
- SPEC.md line 142 — no destructive shortcut for downgrade.
- SPEC.md §5.2 — table layout: tasks (id, command, name, status,
  created_at, result_json) + api_keys.
"""  # NFR-11
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v1"
down_revision: None | str = None
branch_labels: None | str = None
depends_on: None | str = None


def upgrade() -> None:
    """[FR-07] v1 upgrade — create ``tasks`` + ``api_keys``.

    Columns mirror SPEC.md §5.2: ``id`` (string PK), ``command``,
    ``name``, ``status``, ``created_at`` (ISO 8601 string), and
    ``result_json`` (TEXT, JSON payload of the last run). The
    ``result_json`` column lives here in v1 and v2; v3 splits it into
    the dedicated ``task_results`` table.
    """  # NFR-09 NFR-10
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("command", sa.String(length=1000), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
    )


def downgrade() -> None:
    """[FR-07] v1 downgrade — drop ``tasks`` + ``api_keys``.

    Real downgrade via ``op.drop_table``; the destructive shortcut is
    forbidden by AC-7.6 / SPEC.md line 142.
    """  # NFR-02 NFR-09
    op.drop_table("api_keys")
    op.drop_table("tasks")