"""[FR-07] Alembic migrations package — Phase-3 source root.

This package hosts the Alembic migration scripts that build the
``taskq`` schema across three revisions (v1, v2, v3). The
``script_location`` alembic config points at this directory; the
``versions`` sub-package holds the revision modules declared in
``.methodology/SAB.json`` → FR-07 row 1-3:

  - migrations.versions.v1_initial
  - migrations.versions.v2_add_tags
  - migrations.versions.v3_split_results

The package is intentionally lightweight — Alembic's discovery only
needs a directory marker here; the real configuration lives in
``migrations/env.py``.

Citations:
- SPEC.md line 130 — Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆.
- SAD.md §2.7 — migrations layer; versions/v{1,2,3} submodules.
"""  # NFR-11
from __future__ import annotations