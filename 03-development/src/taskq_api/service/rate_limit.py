"""[FR-05] Per-token token-bucket rate limiter — Phase-3 GREEN business logic.

Constructs and consults the per-token bucket with capacity
``TASKQ_RATE_BURST`` and refill rate ``TASKQ_RATE_PER_SEC``. Returns a
``RateLimitDecision`` carrying ``allowed``, ``retry_after_seconds``, and
``tokens_remaining`` so the API layer can render the 429 + problem+json
+ Retry-After response (SPEC.md line 118).

The decision is backed by ``repository.rate_buckets`` so the state
survives worker restarts (SPEC.md line 119). The read-modify-write cycle
is atomic against concurrent workers via the repository's row-level
lock (NP-13 / SPEC.md line 119 單一交易內以 row-level lock 進行).

Citations:
- SPEC.md line 117 — per-token 令牌桶: 容量 TASKQ_RATE_BURST, 補充速率
  TASKQ_RATE_PER_SEC.
- SPEC.md line 118 — 超限 → HTTP 429 + problem+json + Retry-After header
  (秒).
- SPEC.md line 119 — 狀態存於資料庫 (跨 worker 一致), 更新必須在單一
  交易內以 row-level lock 進行.
- SPEC.md line 120 — /healthz, /readyz 不受限 (the dependency is only
  attached to /v1/* routes by the API layer).
- TEST_SPEC.md §1 FR-05 — AC-5.1 (429 + Retry-After) + AC-5.2
  (persistence + row-lock) + AC-5.3 (exemption).
- SAD.md §2.4 — service layer consults the repository seam.
- NFR-06 — api > service > repository > models layering.
- NFR-11 — keep this module ≤ 40 lines of real logic per function.
"""  # NFR-06 NFR-09 NFR-10 NFR-11
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import taskq_api.config as _config
from taskq_api.repository.rate_buckets import (
    fetch_bucket as _fetch_bucket,
    upsert_bucket as _upsert_bucket,
)


@dataclass
class RateLimitDecision:
    """Result of a per-token rate-limit check.

    Attributes:
        allowed: True iff the request consumed a token; False when the
            bucket was empty and the request MUST be rejected with 429.
        retry_after_seconds: positive integer seconds the client should
            wait before retrying (RFC 7231 §7.1.3 delta-seconds form,
            SPEC.md line 118 'Retry-After header (秒)'). Zero when
            ``allowed`` is True (no cooldown needed).
        tokens_remaining: token balance AFTER this call (>= 0). Capped
            at ``capacity``.

    Citations: TEST_SPEC.md §1 FR-05 — decision shape.
    """  # NFR-09 NFR-10

    allowed: bool
    retry_after_seconds: int
    tokens_remaining: float


class RateLimiter:
    """Per-token token-bucket rate limiter.

    The bucket starts at capacity for an unseen ``key_id`` and refills
    at ``refill_per_sec`` tokens per second, capped at capacity. Every
    successful call consumes exactly one token; every rejected call
    computes a ``retry_after_seconds`` from the time required to refill
    one token (rounded up to the next whole second, minimum 1).

    Citations:
    - SPEC.md line 117 — TASKQ_RATE_BURST, TASKQ_RATE_PER_SEC.
    - SPEC.md line 118 — 429 + Retry-After (秒) on exhaustion.
    - SPEC.md line 119 — state persisted in repository (row-locked).
    """  # NFR-06 NFR-09

    def __init__(self, *, capacity: float, refill_per_sec: float) -> None:
        if not isinstance(capacity, (int, float)) or capacity <= 0:
            raise ValueError("capacity must be a positive number")
        if not isinstance(refill_per_sec, (int, float)) or refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be a positive number")
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)

    def check(self, key_id: str) -> RateLimitDecision:
        """Charge one token against ``key_id``'s bucket.

        Algorithm:
            1. Fetch the bucket row (or initialise to capacity).
            2. Refill based on elapsed time since ``last_refill_at``,
               capped at capacity.
            3. If tokens >= 1: consume one, allowed=True, retry_after=0.
               Else: allowed=False, retry_after = ceil((1 - tokens) /
               refill_per_sec), minimum 1.
            4. Upsert the bucket row (row-locked by the repository).

        Citations:
        - SPEC.md line 117 — capacity + refill rate.
        - SPEC.md line 118 — 429 + Retry-After (秒).
        - SPEC.md line 119 — state persisted (row-locked).
        - TEST_SPEC.md §1 FR-05 AC-5.1, AC-5.2.
        """  # NFR-09 NFR-10
        now = datetime.now(timezone.utc)
        bucket = _fetch_bucket(key_id)
        if bucket is None:
            tokens = self.capacity
            last_refill = now
        else:
            tokens = float(bucket["tokens"])
            last_refill = datetime.fromisoformat(bucket["last_refill_at"])

        elapsed = (now - last_refill).total_seconds()
        if elapsed > 0:
            tokens = min(self.capacity, tokens + elapsed * self.refill_per_sec)

        if tokens >= 1.0:
            tokens -= 1.0
            allowed = True
            retry_after = 0
        else:
            allowed = False
            needed = 1.0 - tokens
            retry_after = max(1, math.ceil(needed / self.refill_per_sec))

        _upsert_bucket(
            key_id,
            tokens=tokens,
            last_refill_at=now.isoformat(),
        )

        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=retry_after,
            tokens_remaining=tokens,
        )


# ---------------------------------------------------------------------------
# Module-level default limiter — consults ``taskq_api.config`` so Phase-4
# (config reload) can re-tune without touching the call sites.
# ---------------------------------------------------------------------------

_default_limiter = RateLimiter(
    capacity=_config.TASKQ_RATE_BURST,
    refill_per_sec=_config.TASKQ_RATE_PER_SEC,
)


def check_rate_limit(key_id: str) -> RateLimitDecision:
    """Consult the default token-bucket for ``key_id``.

    Citations: SPEC.md line 117 — TASKQ_RATE_BURST, TASKQ_RATE_PER_SEC;
    TEST_SPEC.md §1 FR-05 AC-5.2 — direct seam for persistence test.
    """  # NFR-09 NFR-10
    return _default_limiter.check(key_id)