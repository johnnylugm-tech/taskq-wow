"""[FR-07] v2_add_tags — add ``tags`` + ``task_tags`` (m:n) and a
unique index on ``tasks.name``.

Additive migration. The v1 tables (``tasks``, ``api_keys``) are NOT
touched; SPEC.md line 137 forbids touching v1 data. The
``ix_tasks_name_unique`` index enforces task-name uniqueness
(SPEC.md §8 #11 — ``name`` unique per active task).

Downgrade drops the index and the two new tables in reverse order;
v1 tables are untouched.

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆.
- SPEC.md line 137 — v2: tags, task_tags, tasks.name unique index; no
  v1 damage.
- SPEC.md line 142 — no destructive shortcut for downgrade.
"""  # NFR-11
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v2"
down_revision: None | str = "v1"
branch_labels: None | str = None
depends_on: None | str = None


def upgrade() -> None:
    """[FR-07] v2 upgrade — add ``tags`` + ``task_tags`` (m:n) and
    unique index ``ix_tasks_name_unique`` on ``tasks.name``.

    v1 tables are intentionally left untouched (SPEC.md line 137 — "no
    v1 damage").
    """  # NFR-09 NFR-10
    op.create_table(
        "tags",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
    )
    op.create_table(
        "task_tags",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "ix_tasks_name_unique",
        "tasks",
        ["name"],
        unique=True,
    )


def downgrade() -> None:
    """[FR-07] v2 downgrade — drop the new index + tables.

    Real downgrade via ``op.drop_index`` + ``op.drop_table``; the
    destructive shortcut is forbidden by AC-7.6 / SPEC.md line 142.
    v1 tables are NOT touched.
    """  # NFR-02 NFR-09
    op.drop_index("ix_tasks_name_unique", table_name="tasks")
    op.drop_table("task_tags")
    op.drop_table("tags")