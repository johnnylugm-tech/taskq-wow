"""[FR-05] Per-token token-bucket rate limiter — Phase-3 GREEN business logic.

Constructs and consults the per-token bucket with capacity
``TASKQ_RATE_BURST`` and refill rate ``TASKQ_RATE_PER_SEC``. Returns a
``RateLimitDecision`` carrying ``allowed``, ``retry_after_seconds``, and
``tokens_remaining`` so the API layer can render the 429 + problem+json
+ Retry-After response (SPEC.md line 118).

The decision is backed by ``repository.rate_buckets`` so the state
survives worker restarts (SPEC.md line 119). The whole read-refill-
spend-write cycle runs inside ``repository.rate_buckets.locked_bucket``,
which holds the row-level lock for the duration — one transaction, so
two workers charging the same token cannot both spend the last one
(NP-13 / SPEC.md line 119 單一交易內以 row-level lock 進行).

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
from typing import Optional, Tuple

import taskq_api.config as _config
from taskq_api.repository.rate_buckets import locked_bucket, upsert_bucket

# One request costs one token (SPEC.md line 117 — the bucket is charged
# per request, not per byte or per route).
_TOKENS_PER_REQUEST = 1.0

# Monotonic counter of bucket rejections (allowed == False). Read by
# ``taskq_api.api.metrics`` to surface the FR-09 /v1/metrics
# ``rate_limit_rejections`` aggregate. Module-scoped (not per-bucket)
# because SPEC.md line 158 specifies a single process-wide count.
_REJECTION_COUNT = 0


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

        The fetch, the refill arithmetic, and the write all happen
        inside a single ``locked_bucket`` block, so the row-level lock
        spans the whole read-modify-write and concurrent workers
        serialise on it (SPEC.md line 119). Splitting this into a
        ``fetch_bucket`` + ``upsert_bucket`` pair would release the
        lock in between and let two workers spend the same token.

        Citations:
        - SPEC.md line 117 — capacity + refill rate.
        - SPEC.md line 118 — 429 + Retry-After (秒).
        - SPEC.md line 119 — state persisted, single row-locked transaction.
        - TEST_SPEC.md §1 FR-05 AC-5.1, AC-5.2.
        """  # NFR-09 NFR-10
        now = datetime.now(timezone.utc)
        with locked_bucket(key_id) as bucket:
            tokens = self._refilled_tokens(bucket, now)
            allowed, retry_after, tokens = self._spend(tokens)
            upsert_bucket(key_id, tokens=tokens, last_refill_at=now.isoformat())
        if not allowed:
            # Increment the process-wide counter; guarded so an unexpected
            # exception in the bookkeeping cannot swallow the rejection
            # decision the caller depends on.
            global _REJECTION_COUNT
            try:
                _REJECTION_COUNT += 1
            except Exception:  # noqa: BLE001
                pass
        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=retry_after,
            tokens_remaining=tokens,
        )

    def _refilled_tokens(self, bucket: Optional[dict], now: datetime) -> float:
        """Return ``bucket``'s balance brought forward to ``now``.

        An unseen bucket starts full (a token's first request must not
        be rejected). A known bucket earns ``refill_per_sec`` tokens per
        elapsed second, capped at ``capacity`` — the bucket never
        accumulates burst beyond ``TASKQ_RATE_BURST`` (SPEC.md line 117).

        Citations: SPEC.md line 117 — 容量 TASKQ_RATE_BURST, 補充速率
        TASKQ_RATE_PER_SEC.
        """  # NFR-09
        if bucket is None:
            return self.capacity
        tokens = float(bucket["tokens"])
        elapsed = (now - datetime.fromisoformat(bucket["last_refill_at"]))
        earned = max(0.0, elapsed.total_seconds()) * self.refill_per_sec
        return min(self.capacity, tokens + earned)

    def _spend(self, tokens: float) -> Tuple[bool, int, float]:
        """Charge one token, returning ``(allowed, retry_after, remaining)``.

        With a full token available the request is allowed and needs no
        cooldown. Otherwise the client is told how long until one token
        has refilled, rounded UP to the next whole second (the
        delta-seconds form of SPEC.md line 118 '秒') and never below 1 —
        a ``Retry-After: 0`` would invite an immediate retry that is
        certain to be rejected again.

        Citations: SPEC.md line 118 — 429 + Retry-After header (秒);
        RFC 7231 §7.1.3 — delta-seconds form.
        """  # NFR-09
        if tokens >= _TOKENS_PER_REQUEST:
            return True, 0, tokens - _TOKENS_PER_REQUEST
        shortfall = _TOKENS_PER_REQUEST - tokens
        retry_after = max(1, math.ceil(shortfall / self.refill_per_sec))
        return False, retry_after, tokens


# ---------------------------------------------------------------------------
# Module-level default limiter — reads ``taskq_api.config`` on every call
# so a config reload re-tunes the bucket without touching the call sites.
# The limiter is rebuilt only when the configured values actually change;
# a benign race just constructs an equivalent limiter twice.
# ---------------------------------------------------------------------------

_default_limiter: Optional[RateLimiter] = None
_default_settings: Optional[Tuple[float, float]] = None


def _default_rate_limiter() -> RateLimiter:
    """Return the shared limiter, rebuilding it if config changed.

    Citations: SPEC.md line 117 — TASKQ_RATE_BURST, TASKQ_RATE_PER_SEC
    are the only knobs; SPEC.md §5.1 — canonical TASKQ_* keys.
    """  # NFR-09
    global _default_limiter, _default_settings
    settings = (
        float(_config.TASKQ_RATE_BURST),
        float(_config.TASKQ_RATE_PER_SEC),
    )
    if _default_limiter is None or _default_settings != settings:
        _default_limiter = RateLimiter(
            capacity=settings[0], refill_per_sec=settings[1]
        )
        _default_settings = settings
    return _default_limiter


def check_rate_limit(key_id: str) -> RateLimitDecision:
    """Consult the default token-bucket for ``key_id``.

    Citations: SPEC.md line 117 — TASKQ_RATE_BURST, TASKQ_RATE_PER_SEC;
    TEST_SPEC.md §1 FR-05 AC-5.2 — direct seam for persistence test.
    """  # NFR-09 NFR-10
    return _default_rate_limiter().check(key_id)


def get_rate_limit_rejections() -> int:
    """Return the process-wide rate-limit rejection count (FR-09).

    Citations: SPEC.md line 158 — ``rate_limit_rejections`` aggregate
    surfaced via ``GET /v1/metrics`` (admin scope).
    """  # NFR-09 NFR-10
    return _REJECTION_COUNT
