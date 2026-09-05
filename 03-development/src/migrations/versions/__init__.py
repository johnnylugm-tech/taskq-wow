"""[FR-07] Alembic revisions package — leaf marker only.

The three revision modules declared in ``.methodology/SAB.json`` →
FR-07 row 1-3 live as siblings of this file:

  - ``v1_initial``        — creates ``tasks`` + ``api_keys``.
  - ``v2_add_tags``       — adds ``tags`` + ``task_tags`` + unique
                            index ``ix_tasks_name_unique`` on
                            ``tasks.name``.
  - ``v3_split_results``  — splits ``tasks.result_json`` into a
                            separate ``task_results`` table (data
                            migration).

The package marker exists so ``from migrations.versions import
v1_initial`` resolves as a dotted import (FR-07 top-level imports in
``test_fr07.py``).

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆.
- SAB.json → FR-07 — module path: migrations.versions.{v1_initial,
  v2_add_tags, v3_split_results}.
"""  # NFR-11
from __future__ import annotations