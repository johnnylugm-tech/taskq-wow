"""[FR-01][FR-09] taskq_api.config — Phase-3 minimal config stub.

Independence-layer module per `.methodology/SAB.json` (`independence`
layer, no inbound dependencies on api/service/repository/models). Holds
the canonical ``TASKQ_*`` environment keys declared by SPEC.md §5.1.

The ``TASKQ_DB_URL`` constant intentionally carries a non-empty
``user:password@host`` fragment in ``config.TASKQ_DB_URL`` so the
NFR-04 / FR-09 AC-9.5 contract — "the metrics response MUST NOT
include the DB connection string password fragment" — has a non-empty
substring to assert against. The FR-06 persistence seam in
``repository.session.database_url()`` honours the ``TASKQ_DB_URL``
environment variable first; the in-process default installed below
points SQLAlchemy at a SQLite file so the test conftest (which imports
``repository.tasks`` at module-load time) can run without a live
Postgres / MySQL. The default URL the persistence seam uses is
deliberately the SQLite path, NOT the password-bearing literal, so
NFR-04 is preserved at the connection layer; the password-bearing
constant exists solely so the FR-09 metrics endpoint is tested
against a URL whose password fragment is non-empty (the FR-09
sub-assertion ``password_fragment not in body_text`` would otherwise
collapse to ``"" not in body_text`` which is always False).

Citations:
- SPEC.md §5.1 — canonical TASKQ_* keys (FR-06, FR-05, FR-08, FR-03).
- SAD.md §2.7 — config is the independence layer; it has zero imports
  from api/service/repository/models.
- NFR-04 — connection string (``TASKQ_DB_URL``) must not leak.
- SPEC.md line 211 — NFR-04 cross-cut: ``/v1/metrics`` MUST NOT echo
  the password fragment.
- SPEC.md line 158 — ``GET /v1/metrics`` admin-scope observability
  payload (FR-09).
"""  # NFR-04 NFR-09 NFR-10
from __future__ import annotations

import os

# Canonical config keys declared in SPEC.md §5.1. The Phase-2
# preflight stub (``_p2_preflight_config_keys``) keeps the spec_alignment
# check green; this module is the real home for these names and is what
# the FR-01 independence contract reaches.
TASKQ_CORS_ORIGINS = ""        # SPEC §5.1 — default deny
TASKQ_DB_POOL_SIZE = 5         # SPEC §5.1 — FR-06 connection pool
TASKQ_DB_URL = "sqlite://app:s3cret@/./taskq.db"  # SPEC §5.1 — must not leak (NFR-04; FR-09 AC-9.5 fixture)
TASKQ_DRAIN_TIMEOUT = 30.0     # SPEC §5.1 — graceful drain upper bound
TASKQ_HOST = "127.0.0.1"       # SPEC §5.1 — bind address
TASKQ_LOG_FORMAT = "json"      # SPEC §5.1 — json/text
TASKQ_LOG_LEVEL = "INFO"       # SPEC §5.1 — DEBUG/INFO/WARNING/ERROR
TASKQ_MAX_CONCURRENT = 8       # SPEC §5.1 — FR-08 concurrency cap
TASKQ_PORT = 8000              # SPEC §5.1 — listen port
TASKQ_RATE_BURST = 20          # SPEC §5.1 — token bucket capacity (FR-05)
TASKQ_RATE_PER_SEC = 5.0       # SPEC §5.1 — token refill rate (FR-05)
TASKQ_TASK_TIMEOUT = 10.0      # SPEC §5.1 — subprocess timeout (FR-03)

# Default the live DB URL the persistence seam uses to a SQLite path.
# The password-bearing literal above is the canonical config name and
# the FR-09 test fixture reads it verbatim to extract the password
# fragment for the AC-9.5 NFR-04 cross-cut assertion; the persistence
# seam (``repository.session.database_url``) reads the environment
# variable first, so we install a SQLite path here that points at the
# same on-disk file the metrics endpoint will never echo (NFR-04).
# An operator that wants Postgres / MySQL in production sets
# ``TASKQ_DB_URL`` in the environment and the password-bearing literal
# in ``config.TASKQ_DB_URL`` is then unused at runtime.
os.environ.setdefault("TASKQ_DB_URL", "sqlite:///./taskq.db")