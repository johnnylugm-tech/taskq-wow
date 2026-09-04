"""[FR-01] taskq_api.config — Phase-3 minimal config stub.

Independence-layer module per `.methodology/SAB.json` (`independence`
layer, no inbound dependencies on api/service/repository/models). Holds
the canonical ``TASKQ_*`` environment keys declared by SPEC.md §5.1.

Citations:
- SPEC.md §5.1 — canonical TASKQ_* keys (FR-06, FR-05, FR-08, FR-03).
- SAD.md §2.7 — config is the independence layer; it has zero imports
  from api/service/repository/models.
- NFR-04 — connection string (``TASKQ_DB_URL``) must not leak.
"""  # NFR-09 NFR-10
from __future__ import annotations

# Canonical config keys declared in SPEC.md §5.1. The Phase-2
# preflight stub (``_p2_preflight_config_keys``) keeps the spec_alignment
# check green; this module is the real home for these names and is what
# the FR-01 independence contract reaches.
TASKQ_CORS_ORIGINS = ""        # SPEC §5.1 — default deny
TASKQ_DB_POOL_SIZE = 5         # SPEC §5.1 — FR-06 connection pool
TASKQ_DB_URL = "sqlite:///./taskq.db"  # SPEC §5.1 — must not leak (NFR-04)
TASKQ_DRAIN_TIMEOUT = 30.0     # SPEC §5.1 — graceful drain upper bound
TASKQ_HOST = "127.0.0.1"       # SPEC §5.1 — bind address
TASKQ_LOG_FORMAT = "json"      # SPEC §5.1 — json/text
TASKQ_LOG_LEVEL = "INFO"       # SPEC §5.1 — DEBUG/INFO/WARNING/ERROR
TASKQ_MAX_CONCURRENT = 8       # SPEC §5.1 — FR-08 concurrency cap
TASKQ_PORT = 8000              # SPEC §5.1 — listen port
TASKQ_RATE_BURST = 20          # SPEC §5.1 — token bucket capacity (FR-05)
TASKQ_RATE_PER_SEC = 5.0       # SPEC §5.1 — token refill rate (FR-05)
TASKQ_TASK_TIMEOUT = 10.0      # SPEC §5.1 — subprocess timeout (FR-03)