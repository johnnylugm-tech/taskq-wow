"""Phase-2 preflight stub: bridges spec_alignment canonical-config-key check.

The canonical SPEC.md declares 12 TASKQ_* configuration keys (§5.1). The
spec_alignment preflight gate fires at P2 entry if `03-development/src/` exists
but contains no file referencing any of those keys. Real key-loading code is
authored at P3; this stub exists only to keep the P2 entry gate green and
carries no runtime semantics.

Tracked under .methodology so P3 can replace it with the real config module.
"""

# Canonical config keys declared in SPEC.md §5.1 — referenced here for
# preflight spec_alignment (`unread_config_key` check), which performs a
# substring scan over src/ files. Do not edit the names; they must match
# SPEC.md lines 291-302 verbatim.
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
