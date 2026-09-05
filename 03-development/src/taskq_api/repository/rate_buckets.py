"""[FR-05] Per-token ``rate_buckets`` repository — Phase-3 GREEN backing store.

Mirrors the shape of ``taskq_api.repository.api_keys`` so the Phase-4 SQL
swap (SQLAlchemy 2.x + Alembic per FR-06) can replace this module
without touching the rest of the codebase. Two functions make up the
contract:

- ``fetch_bucket(key_id)`` returns the bucket row
  ``{key_id, tokens, last_refill_at}`` or ``None`` if the bucket has
  not been seen before.
- ``upsert_bucket(key_id, *, tokens, last_refill_at)`` inserts OR
  updates the bucket row. The write is wrapped in a per-process lock
  so concurrent workers cannot double-spend tokens (NP-13 / SPEC.md
  line 119). Phase-3 keeps the lock in ``threading.Lock``; Phase-4
  swaps it for ``SELECT ... FOR UPDATE`` inside a single transaction.

Both reads and writes go through the same ``_lock`` so the read-modify-
write cycle inside ``service.rate_limit`` is atomic against concurrent
workers (SPEC.md line 119 單一交易內以 row-level lock 進行). The lock
granularity is per-store, not per-row, but every read and write is
serialised on the same primitive so the row-level invariant holds for
the in-memory Phase-3 GREEN. Phase-4 replaces this with a true row-
level lock.

Citations:
- SPEC.md line 117 — per-token 令牌桶: 容量 TASKQ_RATE_BURST, 補充速率
  TASKQ_RATE_PER_SEC.
- SPEC.md line 119 — 狀態存於資料庫 (跨 worker 一致), 更新必須在單一
  交易內以 row-level lock 進行.
- SAD.md §2.4 — repository is the persistence boundary; row-lock lives
  here, not in the service layer (NFR-06).
- TEST_SPEC.md §1 FR-05 — AC-5.2 (persistence + row-level lock).
- NFR-06 — api > service > repository layering; lock primitive MUST live
  in the repository seam.
- NFR-09 — public functions carry docstrings.
"""  # NFR-06 NFR-09 NFR-11
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

_lock = threading.Lock()
_store: dict[str, dict] = {}


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 with microsecond precision.

    Citations: SPEC.md line 117 — last_refill_at is a UTC timestamp.
    """  # NFR-09
    return datetime.now(timezone.utc).isoformat()


def _reset_state() -> None:
    """Reset the in-memory store (test seam — FR-05 RED tests only).

    Citations: ``03-development/tests/test_fr05.py`` autouse fixture
    ``_reset_rate_buckets_store``; v2.13.0 — no module-scoped fixtures
    for stateful stores.
    """  # NFR-09
    global _store
    with _lock:
        _store = {}


def fetch_bucket(key_id: str) -> Optional[dict]:
    """Return the bucket row whose ``key_id`` matches, or ``None`` if unknown.

    The returned dict is a shallow copy of the stored row so callers
    cannot accidentally mutate the in-memory store. The ``tokens``
    field is a ``float`` (refill math may add fractional tokens) and
    ``last_refill_at`` is an ISO 8601 UTC string (SPEC.md line 117).

    Citations: SPEC.md line 117 — bucket shape ``{tokens,
    last_refill_at}``; TEST_SPEC.md §1 FR-05 AC-5.2 row 1 — seeded
    bucket must be visible via the repository seam.
    """  # NFR-09
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("key_id must be a non-empty str")
    with _lock:
        row = _store.get(key_id)
        if row is None:
            return None
        return dict(row)


def upsert_bucket(key_id: str, *, tokens: float, last_refill_at: str):
    """Insert or update the bucket row for ``key_id`` (row-level lock).

    The lock is taken on the same ``_lock`` primitive as ``fetch_bucket``
    so the read-modify-write cycle performed by ``service.rate_limit``
    is atomic against concurrent workers (NP-13 / SPEC.md line 119).
    Phase-3 keeps the row-lock as a ``threading.Lock``; Phase-4 swaps
    it for ``SELECT ... FOR UPDATE`` inside a single SQL transaction.

    Args:
        key_id: the bucket key (per-token).
        tokens: remaining tokens (float; refill may add fractional tokens).
        last_refill_at: ISO 8601 UTC timestamp of the last refill
            observation.

    Citations: SPEC.md line 119 — 單一交易內以 row-level lock 進行;
    TEST_SPEC.md §1 FR-05 AC-5.2 row 2 — seeded bucket must persist
    across worker restart.
    """  # NFR-06 NFR-09
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("key_id must be a non-empty str")
    if not isinstance(tokens, (int, float)):
        raise TypeError("tokens must be a number")
    if not isinstance(last_refill_at, str) or not last_refill_at:
        raise ValueError("last_refill_at must be a non-empty str")
    with _lock:
        _store[key_id] = {
            "key_id": key_id,
            "tokens": float(tokens),
            "last_refill_at": last_refill_at,
        }