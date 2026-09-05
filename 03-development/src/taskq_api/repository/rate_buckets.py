"""[FR-05] Per-token ``rate_buckets`` repository — Phase-3 GREEN backing store.

Mirrors the shape of ``taskq_api.repository.api_keys`` so the Phase-4 SQL
swap (SQLAlchemy 2.x + Alembic per FR-06) can replace this module
without touching the rest of the codebase. Three functions make up the
contract:

- ``fetch_bucket(key_id)`` returns the bucket row
  ``{key_id, tokens, last_refill_at}`` or ``None`` if the bucket has
  not been seen before.
- ``upsert_bucket(key_id, *, tokens, last_refill_at)`` inserts OR
  updates the bucket row.
- ``locked_bucket(key_id)`` is the TRANSACTION seam: a context manager
  that takes the row lock, yields the current row (or ``None``), and
  holds the lock until the block exits. A caller that reads, computes,
  and writes inside the block performs its read-modify-write as ONE
  atomic transaction — this is what SPEC.md line 119 (更新必須在單一
  交易內以 row-level lock 進行) requires, and it is the only way the
  token bucket in ``service.rate_limit`` avoids double-spending under
  concurrent workers (NP-13).

Calling ``fetch_bucket`` and then ``upsert_bucket`` is NOT equivalent:
each takes and releases the lock on its own, so two workers can
interleave between the read and the write and both spend the last
token. Read-modify-write callers MUST use ``locked_bucket``.

Locks are per ``key_id``, not per store — a write to one token's bucket
never blocks a write to another's. That per-bucket granularity is the
in-process stand-in for ``SELECT ... FOR UPDATE`` on a single row;
Phase-4 replaces the ``threading`` primitives with a real SQL
transaction over the same three entry points. The per-key locks are
re-entrant so ``upsert_bucket`` remains the single write path even when
it is called from inside a ``locked_bucket`` block.

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
from contextlib import contextmanager
from typing import Iterator, Optional

# ``_registry_lock`` guards the lock registry itself (creating a row lock
# must be atomic); ``_row_locks`` holds one re-entrant lock per bucket
# key — the row-level lock proper. ``_store`` is the bucket table.
_registry_lock = threading.Lock()
_row_locks: dict[str, threading.RLock] = {}
_store: dict[str, dict] = {}


def _checked_key_id(key_id: str) -> str:
    """Return ``key_id`` after rejecting the empty / non-str forms.

    Shared by every entry point so the three public functions cannot
    drift apart on what a valid bucket key is.
    """  # NFR-09
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("key_id must be a non-empty str")
    return key_id


def _row_lock(key_id: str) -> threading.RLock:
    """Return the row-level lock for ``key_id``, creating it on first use.

    Re-entrant so a ``upsert_bucket`` call nested inside a
    ``locked_bucket`` block does not deadlock against the block that
    already holds the row.

    Citations: SPEC.md line 119 — row-level lock granularity.
    """  # NFR-09
    with _registry_lock:
        lock = _row_locks.get(key_id)
        if lock is None:
            lock = threading.RLock()
            _row_locks[key_id] = lock
        return lock


def _reset_state() -> None:
    """Reset the store and the lock registry (test seam — FR-05 only).

    Citations: ``03-development/tests/test_fr05.py`` autouse fixture
    ``_reset_rate_buckets_store``; v2.13.0 — no module-scoped fixtures
    for stateful stores.
    """  # NFR-09
    global _store, _row_locks
    with _registry_lock:
        _store = {}
        _row_locks = {}


def fetch_bucket(key_id: str) -> Optional[dict]:
    """Return the bucket row whose ``key_id`` matches, or ``None`` if unknown.

    The returned dict is a shallow copy of the stored row so callers
    cannot accidentally mutate the store. The ``tokens`` field is a
    ``float`` (refill math may add fractional tokens) and
    ``last_refill_at`` is an ISO 8601 UTC string (SPEC.md line 117).

    This is a point read. A caller that intends to write back what it
    read MUST use ``locked_bucket`` instead — see the module docstring.

    Citations: SPEC.md line 117 — bucket shape ``{tokens,
    last_refill_at}``; TEST_SPEC.md §1 FR-05 AC-5.2 row 1 — seeded
    bucket must be visible via the repository seam.
    """  # NFR-09
    key_id = _checked_key_id(key_id)
    with _row_lock(key_id):
        row = _store.get(key_id)
        return dict(row) if row is not None else None


@contextmanager
def locked_bucket(key_id: str) -> Iterator[Optional[dict]]:
    """Hold the row lock for ``key_id`` across a read-modify-write.

    Yields the current bucket row (a copy) or ``None`` if the bucket is
    unseen, and keeps the row-level lock until the block exits, so the
    read, the token arithmetic, and the ``upsert_bucket`` write inside
    the block form a SINGLE transaction. Concurrent workers charging
    the same token serialise on this lock and cannot double-spend
    (NP-13 / SPEC.md line 119 單一交易內以 row-level lock 進行).

    Usage::

        with locked_bucket(key_id) as bucket:
            tokens = capacity if bucket is None else bucket["tokens"]
            upsert_bucket(key_id, tokens=tokens - 1, last_refill_at=now)

    Phase-4 swaps the body for ``BEGIN; SELECT ... FOR UPDATE; ...;
    COMMIT;`` over the same signature.

    Citations: SPEC.md line 119; TEST_SPEC.md §1 FR-05 AC-5.2.
    """  # NFR-06 NFR-09
    key_id = _checked_key_id(key_id)
    with _row_lock(key_id):
        row = _store.get(key_id)
        yield dict(row) if row is not None else None


def upsert_bucket(key_id: str, *, tokens: float, last_refill_at: str):
    """Insert or update the bucket row for ``key_id`` (row-level lock).

    Takes the same per-key lock as ``locked_bucket`` and ``fetch_bucket``,
    so a write is serialised against every other access to that bucket
    (NP-13 / SPEC.md line 119). The lock is re-entrant: calling this
    from inside a ``locked_bucket`` block extends that block's
    transaction rather than deadlocking on it.

    Args:
        key_id: the bucket key (per-token).
        tokens: remaining tokens (float; refill may add fractional tokens).
        last_refill_at: ISO 8601 UTC timestamp of the last refill
            observation.

    Citations: SPEC.md line 119 — 單一交易內以 row-level lock 進行;
    TEST_SPEC.md §1 FR-05 AC-5.2 row 2 — seeded bucket must persist
    across worker restart.
    """  # NFR-06 NFR-09
    key_id = _checked_key_id(key_id)
    if not isinstance(tokens, (int, float)):
        raise TypeError("tokens must be a number")
    if not isinstance(last_refill_at, str) or not last_refill_at:
        raise ValueError("last_refill_at must be a non-empty str")
    with _row_lock(key_id):
        _store[key_id] = {
            "key_id": key_id,
            "tokens": float(tokens),
            "last_refill_at": last_refill_at,
        }
