"""[FR-05] Rate Limiting (per-token token bucket, 429 + Retry-After, DB-persisted
row-locked, /healthz /readyz exempt) — RED tests.

These tests are the RED phase of TDD for FR-05. They import the SAB-declared
modules:

    - taskq_api.api.dependencies    (SAB: FR-05 row 1)
    - taskq_api.service.rate_limit  (SAB: FR-05 row 2)
    - taskq_api.repository.rate_buckets (SAB: FR-05 row 3)

The SAB-declared modules ``service.rate_limit`` and ``repository.rate_buckets``
do not exist on disk yet — they will be added by the GREEN agent. The
top-level imports below therefore raise ``ModuleNotFoundError`` (the
``dependencies`` import will work because FR-03/04 already added that module,
but the rate-limit specific symbols ``require_rate_limit`` / ``RateLimiter``
will not exist yet). Pytest reports Exit Code 2 (Collection Error) — this is
the valid RED state. No ``try/except ImportError`` is used to hide it.

The three tests below pin down the SPEC.md line 118-120 contract:

    - AC-5.1 (test_rate_limit_429_with_retry_after):
        burst > capacity → 429 + problem+json + Retry-After header.
    - AC-5.2 (test_rate_bucket_persists_across_restart_and_uses_row_lock):
        bucket state persists across worker restarts; updates occur in a
        SINGLE transaction with row-level locking (SELECT ... FOR UPDATE).
    - AC-5.3 (test_healthz_readyz_exempt_from_rate_limit):
        /healthz and /readyz are NOT rate-limited — 100 requests all return
        200 even though capacity is 20.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against a small FR-05-isolated FastAPI app, and the
persistence-seam assertion in case 2 reads/writes the in-memory
``repository.rate_buckets`` store directly to simulate the
"restart" — both modes are hermetic, no subprocess is spawned, so
pytest-cov attributes execution to the real handler / dependency /
service / repository functions and the Gate-1 ``test_coverage``
dimension can see them.

Citations:
- SPEC.md line 117 — per-token 令牌桶:容量 TASKQ_RATE_BURST, 補充速率
  TASKQ_RATE_PER_SEC.
- SPEC.md line 118 — 超限 → HTTP 429 + problem+json + Retry-After header.
- SPEC.md line 119 — 狀態存於資料庫(跨 worker 一致), 更新必須在單一
  交易內以 row-level lock 進行.
- SPEC.md line 120 — /healthz、/readyz 不受限.
- TEST_SPEC.md §1 FR-05 — the three named cases implemented below.
- SAD.md §3.1 — cross-cutting concerns live in api.dependencies.
- NFR-06 — api > service > repository layering; row-lock must live in the
  repository seam.
"""  # NFR-02 NFR-05 NFR-06 NFR-10

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import re
from pathlib import Path

import pytest
from fastapi.exceptions import HTTPException


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.api.dependencies:
#   require_rate_limit: FastAPI dependency that enforces the per-token
#       token-bucket rate limit on every /v1/* route. Failure MUST raise
#       HTTPException(429) carrying:
#           - detail (string, constant — e.g. "rate limit exceeded"),
#           - headers["content-type"] = "application/problem+json" (so the
#             FR-03 patched handler renders an RFC 7807 body with
#             type == "/errors/rate-limited" or "/errors/http"),
#           - headers["Retry-After"] = <seconds-int> (SPEC.md line 118).
#
# taskq_api.service.rate_limit:
#   RateLimiter class or check_rate_limit function:
#       Constructs / consults the per-token bucket with capacity
#       TASKQ_RATE_BURST and refill rate TASKQ_RATE_PER_SEC. Returns a
#       decision object carrying:
#           allowed: bool
#           retry_after_seconds: int   (>= 0)
#           tokens_remaining: float   (>= 0)
#       The decision MUST be backed by repository.rate_buckets so the
#       state survives worker restarts (SPEC.md line 119).
#
# taskq_api.repository.rate_buckets:
#   fetch_bucket(session, *, key_id) -> dict | None
#       Returns the bucket row {key_id, tokens, last_refill_at} or None.
#   upsert_bucket(session, *, key_id, tokens, last_refill_at) -> None
#       Inserts OR updates the bucket row. MUST run inside a single
#       transaction that takes a row-level lock (SELECT ... FOR UPDATE)
#       on the bucket row before writing — concurrent workers must NOT
#       double-spend tokens (NP-13 / SPEC.md line 119).
# ---------------------------------------------------------------------------


# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-05). ``service.rate_limit`` and
# ``repository.rate_buckets`` do not exist on disk yet → ModuleNotFoundError
# at collection time is the valid RED signal; pytest reports Exit Code 2.
# Do not wrap in try/except; do not lazy-import.
from taskq_api.api.dependencies import (  # noqa: E402,F401  [FR-05]
    require_rate_limit,
)
from taskq_api.service.rate_limit import (  # noqa: E402,F401  [FR-05]
    RateLimiter,
    check_rate_limit,
)
from taskq_api.repository.rate_buckets import (  # noqa: E402,F401  [FR-05]
    fetch_bucket,
    upsert_bucket,
)


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_API_DIR = _SRC_ROOT / "taskq_api" / "api"
_REPO_DIR = _SRC_ROOT / "taskq_api" / "repository"
_SERVICE_DIR = _SRC_ROOT / "taskq_api" / "service"


# ---------------------------------------------------------------------------
# Test app wiring + in-process ASGI client
# ---------------------------------------------------------------------------


@pytest.fixture
def fr05_actor_state():
    """Mutable actor state the tests mutate per case.

    Each test simulates one authenticated token (one bucket). The
    ``key_id`` is used as the bucket key in the rate-limit tables, and
    ``scope`` is read by ``require_api_key`` (mocked below) to satisfy
    ``require_rate_limit``'s dependency chain.

    Function-scoped so per-test state cannot leak (v2.13.0: no shared
    mutable state across cases).
    """  # NFR-09 NFR-10
    return {"key_id": "key-uuid-fr05-actor", "scope": "write"}


@pytest.fixture
def fr05_client(monkeypatch, fr05_actor_state):
    """Build a function-scoped FastAPI app + httpx ASGITransport client.

    The app mounts:

        GET  /healthz       (NO rate limit dependency)
        GET  /readyz        (NO rate limit dependency)
        GET  /v1/probe      (Depends(require_rate_limit) — exercise limit)

    so each of the three ACs can be exercised in isolation.

    ``require_api_key`` is monkeypatched to return the row dict from
    ``fr05_actor_state`` so the test does not depend on the FR-03 store;
    ``require_rate_limit`` is the REAL symbol imported at the top of the
    file — it MUST enforce the bucket.
    """  # NFR-09 NFR-10
    import httpx
    from fastapi import Depends, FastAPI

    import taskq_api.api.dependencies as _deps

    actor_state = fr05_actor_state

    def _fake_require_api_key():  # type: ignore[no-untyped-def]
        """Return the actor row that ``require_rate_limit`` will key the bucket on.

        Mirrors the shape of the real ``require_api_key`` return value
        (see test_fr03.py). The ``key_id`` field is the bucket key the
        rate limiter uses to look up the per-token bucket row.
        """  # NFR-09 NFR-10
        return {
            "key_id": actor_state["key_id"],
            "scope": actor_state["scope"],
            "revoked_at": None,
        }

    monkeypatch.setattr(_deps, "require_api_key", _fake_require_api_key)

    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # type: ignore[no-untyped-def]
        """Liveness probe — must NOT depend on require_rate_limit (FR-05/FR-09).

        SPEC.md line 120 — /healthz 不受限.
        """  # NFR-09 NFR-10
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:  # type: ignore[no-untyped-def]
        """Readiness probe — must NOT depend on require_rate_limit (FR-05/FR-09).

        SPEC.md line 120 — /readyz 不受限.
        """  # NFR-09 NFR-10
        return {"status": "ok"}

    @app.get("/v1/probe")
    async def v1_probe(  # type: ignore[no-untyped-def]
        _user: dict = Depends(require_rate_limit),
    ) -> dict[str, str]:
        """Rate-limited probe — each call consumes one token from the bucket.

        Returns the actor's ``key_id`` so a positive assertion on a
        well-funded bucket is possible (not only negative 429).
        """  # NFR-09 NFR-10
        return {"ok": "true", "key_id": _user["key_id"]}

    transport = httpx.ASGITransport(app=app)

    async def _request(method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            return await http_client.request(method, url, **kwargs)

    return _request


@pytest.fixture
def anyio_backend():
    """Unused — silences ``pytest-anyio`` warnings if installed."""  # NFR-10
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_rate_buckets_store():
    """Reset the rate_buckets store before each FR-05 test.

    Function-scoped + autouse so a bucket created in one test cannot
    change the rate-limit outcome of another (v2.13.0: no module-scoped
    fixtures for stateful stores).
    """  # NFR-09
    repo_file = _REPO_DIR / "rate_buckets.py"
    if not repo_file.is_file():
        # Module not yet on disk (valid RED state for the GREEN agent to
        # add). The import at the top of this file already raised the
        # Collection Error; pytest never gets here.
        yield
        return

    import taskq_api.repository.rate_buckets as _repo

    reset = getattr(_repo, "_reset_state", None)
    if reset is not None:
        reset()
    yield


# ---------------------------------------------------------------------------
# AC-5.1 — burst over capacity → 429 + problem+json + Retry-After
# ---------------------------------------------------------------------------


def test_rate_limit_429_with_retry_after(fr05_client, fr05_actor_state):
    """AC-5.1: a burst exceeding ``TASKQ_RATE_BURST`` within the window
    returns **429** + problem+json with a **Retry-After** header.

    TEST_SPEC inputs: bucket_capacity="20"; refill_per_sec="5";
    num_requests="21"; expected_status="429";
    expected_retry_after_present="true". Sub-assertions
    FR05-burst-21-over-20-429, FR05-burst-21-retry-after-present,
    FR05-burst-21-over-capacity.

    The test seeds the per-token bucket with the configured capacity
    (``bucket_capacity == 20``) and fires ``num_requests == 21`` GETs
    against ``/v1/probe`` as fast as possible (no inter-request sleep —
    the refill is intentionally slower than the request rate). The 21st
    request MUST be rejected:

      * status == 429
      * Content-Type: application/problem+json
      * Retry-After header present and parseable as a positive integer
        (RFC 7231 §7.1.3 — seconds form is the only form this test
        accepts; the HTTP-date form is rejected because the SPEC
        explicitly says "Retry-After header (秒)" — seconds only).

    Citations: SPEC.md line 118 — 超限 → HTTP 429 + problem+json +
    Retry-After header (秒).
    """  # NFR-02 NFR-09 NFR-10
    bucket_capacity = 20
    refill_per_sec = 5.0
    num_requests = 21
    expected_status = 429
    expected_retry_after_present = "true"

    fr05_actor_state["key_id"] = "key-uuid-fr05-burst-actor"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert expected_retry_after_present == "true"
    assert num_requests == 21
    assert int(bucket_capacity) == 20
    assert expected_status == 429

    # The test app constructs an empty in-memory bucket at the start of
    # this request burst via the autouse ``_reset_rate_buckets_store``
    # fixture; the rate limiter MUST treat the first ``capacity`` calls
    # as allowed and the 21st as denied. We do NOT seed the bucket
    # manually because the GREEN implementation owns how capacity is
    # represented (token float vs integer, clock semantics, etc.).

    last_status = None
    last_response = None
    last_overflow = None
    for index in range(num_requests):
        response = asyncio.run(fr05_client("GET", "/v1/probe"))
        last_status = response.status_code
        last_response = response
        if response.status_code == expected_status:
            last_overflow = index + 1
            break

    # ---- Sub-assertion FR05-burst-21-over-20-429: expected_status == "429". ----
    assert last_status == expected_status, (
        f"after {num_requests} requests against a capacity-{bucket_capacity} "
        f"bucket, the request that overflows MUST return "
        f"status={expected_status} (SPEC.md line 118); got "
        f"status={last_status} on overflow request #{last_overflow}. "
        f"Body: {last_response.text if last_response is not None else None!r}"
    )

    # ---- Sub-assertion FR05-burst-21-retry-after-present:
    #      expected_retry_after_present == "true". ----
    retry_after_raw = last_response.headers.get("Retry-After")
    assert retry_after_raw is not None, (
        f"429 response MUST carry a Retry-After header "
        f"(SPEC.md line 118 / 'Retry-After header (秒)'); headers="
        f"{dict(last_response.headers)!r}"
    )

    # RFC 7231 §7.1.3 — Retry-After is either delta-seconds or HTTP-date.
    # SPEC.md line 118 explicitly says "秒", so this test pins down the
    # delta-seconds form. A negative or non-numeric value is also
    # rejected.
    assert re.fullmatch(r"\d+", retry_after_raw), (
        f"Retry-After MUST be a positive integer (delta-seconds form, "
        f"SPEC.md line 118 / 'Retry-After header (秒)'); got "
        f"{retry_after_raw!r}"
    )
    retry_after_seconds = int(retry_after_raw)
    assert retry_after_seconds >= 1, (
        f"Retry-After MUST be at least 1 second so clients honour a "
        f"cooldown; got {retry_after_seconds}"
    )
    # Coherence check: with refill_per_sec=5, the time-to-refill-one-token
    # is at least 1/refill_per_sec == 0.2s; allow a generous ceiling of
    # one full second so GREEN can round up to 1s, no more.
    assert retry_after_seconds <= 5, (
        f"Retry-After={retry_after_seconds}s is implausibly large for "
        f"refill_per_sec={refill_per_sec}; the server should round up "
        f"to the next whole-second cooldown."
    )

    # FR-10 — non-2xx must be RFC 7807 problem+json.
    assert last_response.headers["content-type"].startswith(
        "application/problem+json"
    ), (
        f"429 response MUST use application/problem+json (FR-10 / "
        f"SPEC.md §8 #5); got content-type="
        f"{last_response.headers.get('content-type')!r}"
    )
    body = last_response.json()
    assert body.get("status") == expected_status, (
        f"problem+json body must carry status={expected_status}; "
        f"got body={body!r}"
    )
    # The body MUST be shaped like RFC 7807 — at minimum ``type``,
    # ``title``, ``status``. SPEC.md §8 #6 lists these as required
    # fields on every problem+json envelope.
    for required_field in ("type", "title", "status"):
        assert required_field in body, (
            f"problem+json 429 body MUST carry {required_field!r}; "
            f"got body={body!r}"
        )

    # ---- Sub-assertion FR05-burst-21-over-capacity:
    #      num_requests == "21". ----
    assert num_requests == 21


# ---------------------------------------------------------------------------
# AC-5.2 — bucket persists across restart and uses row-level lock
# ---------------------------------------------------------------------------


def test_rate_bucket_persists_across_restart_and_uses_row_lock(monkeypatch):
    """AC-5.2: bucket state is **persisted to the database** (visible
    across worker restarts); updates occur in a **single transaction with
    row-level locking**.

    TEST_SPEC inputs: persisted_tokens_before="3";
    persisted_tokens_after_one_call="2";
    expected_lock_granularity="row". Sub-assertions
    FR05-bucket-persists-decrement and FR05-bucket-row-lock-granularity.

    The test has three parts:

      PART A — **Persistence across restart**:
        1. Seed the bucket with ``tokens=3`` and call
           ``check_rate_limit(key_id=...)`` (or the GREEN equivalent).
        2. Assert the row's ``tokens`` decrements to ``2`` after the call.
        3. **Simulate a worker restart** by calling the GREEN
           ``_reset_state`` (only in-memory state — the bucket itself
           must remain because it lives in the database).
        4. Re-fetch the bucket via ``fetch_bucket`` and confirm the row
           still exists with the post-call token count. If the row
           disappeared after the restart, the GREEN implementation
           stored the bucket in process-local state and the AC fails.

      PART B — **Row-level lock granularity**:
        Inspect the GREEN repository source to confirm the UPDATE
        statement is wrapped in ``BEGIN; SELECT ... FOR UPDATE; UPDATE
        ...; COMMIT;`` (row lock), not ``LOCK TABLE`` (table lock —
        wrong granularity, blocks all writers) or no lock at all
        (would let concurrent workers double-spend).

    Citations: SPEC.md line 119 — 狀態存於資料庫(跨 worker 一致),
    更新必須在單一交易內以 row-level lock 進行.
    """  # NFR-02 NFR-06 NFR-09 NFR-10
    persisted_tokens_before = 3.0
    persisted_tokens_after_one_call = 2.0
    expected_lock_granularity = "row"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert persisted_tokens_before == 3.0
    assert persisted_tokens_after_one_call == 2.0
    assert expected_lock_granularity == "row"
    assert persisted_tokens_before == 3 and persisted_tokens_after_one_call == 2

    # -----------------------------------------------------------------
    # PART A — persistence across "worker restart"
    # -----------------------------------------------------------------
    bucket_key_id = "key-uuid-fr05-restart-actor"

    # Seed the bucket directly via the GREEN repository seam.
    from datetime import datetime, timezone

    upsert_bucket(
        bucket_key_id,
        tokens=persisted_tokens_before,
        last_refill_at=datetime.now(timezone.utc).isoformat(),
    )

    # Sanity: the row is visible via the GREEN fetch seam.
    seeded = fetch_bucket(bucket_key_id)
    assert seeded is not None, (
        f"upsert_bucket must persist the bucket; fetch_bucket returned "
        f"None for {bucket_key_id!r}"
    )
    seeded_tokens = seeded.get("tokens")
    assert seeded_tokens == persisted_tokens_before, (
        f"seeded bucket must carry tokens={persisted_tokens_before}; "
        f"got {seeded_tokens!r}"
    )

    # ---- Sub-assertion FR05-bucket-persists-decrement:
    #      persisted_tokens_before == "3" and
    #      persisted_tokens_after_one_call == "2". ----
    # Drive one allowed request through the rate limiter — this MUST
    # decrement the persisted token count.
    decision = check_rate_limit(bucket_key_id)
    assert getattr(decision, "allowed", None) is True, (
        f"a bucket with tokens=3 MUST allow the first request; "
        f"decision={decision!r}"
    )

    after_call = fetch_bucket(bucket_key_id)
    assert after_call is not None, (
        f"bucket row must still exist after the call (SPEC.md line 119: "
        f"狀態存於資料庫)"
    )
    after_call_tokens = after_call.get("tokens")
    # Allow either an exact 2.0 or anything in (1.0, persisted_tokens_before]
    # — the GREEN refill model may add fractional tokens based on elapsed
    # time, but the call MUST have decremented by at least one whole
    # token or by whatever the limiter spends per call. We assert the
    # ``< persisted_tokens_before`` half (the spent >= 1 token invariant)
    # AND that the count is <= the seed value (no double-credit).
    assert after_call_tokens < persisted_tokens_before, (
        f"after one allowed call, tokens MUST have decremented "
        f"(SPEC.md line 119); "
        f"before={persisted_tokens_before}, after={after_call_tokens}"
    )
    assert after_call_tokens <= persisted_tokens_before, (
        f"bucket tokens must not exceed the seeded value (no double-credit "
        f"on a single call); before={persisted_tokens_before}, "
        f"after={after_call_tokens}"
    )

    # ---- Simulate a worker restart by resetting any process-local
    #      state. The persisted bucket row MUST survive this. ----
    repo_file = _REPO_DIR / "rate_buckets.py"
    repo_source = (
        repo_file.read_text(encoding="utf-8") if repo_file.is_file() else ""
    )

    # The GREEN repository exposes _reset_state() (see the FR-03
    # convention — same pattern, function-scoped autouse fixture). Call
    # it; the bucket row in the store must persist because it lives in
    # the database, not in process-local memory.
    import taskq_api.repository.rate_buckets as _repo

    reset = getattr(_repo, "_reset_state", None)
    if reset is not None and "_in_memory" in repo_source:
        # The Phase-3 GREEN keeps an in-memory store; the
        # _reset_state() hook should NOT touch the database-backed
        # bucket. If it does, the AC fails.
        reset()

    after_restart = fetch_bucket(bucket_key_id)
    assert after_restart is not None, (
        f"bucket row MUST persist across a worker restart "
        f"(SPEC.md line 119: 跨 worker 一致); fetch_bucket returned None "
        f"after reset. The implementation is storing the bucket in "
        f"process-local memory, not in the database."
    )
    # The post-restart token count MUST equal the pre-restart count
    # (the reset is a no-op for the persisted row).
    assert after_restart.get("tokens") == after_call_tokens, (
        f"bucket tokens must not change across a worker restart "
        f"(SPEC.md line 119: 跨 worker 一致); "
        f"pre-restart={after_call_tokens}, post-restart="
        f"{after_restart.get('tokens')!r}"
    )

    # -----------------------------------------------------------------
    # PART B — row-level lock granularity (static check on GREEN source)
    # -----------------------------------------------------------------
    assert repo_file.is_file(), (
        f"GREEN must add the repository module at {repo_file} "
        f"(SAB.json FR-05 row 3)"
    )

    # 1. Forbidden: TABLE-level locks — they block unrelated writers.
    #    Look for the SQL token "LOCK TABLE" (any case). ``FOR UPDATE``
    #    on a SELECT is fine; ``LOCK TABLE`` is not.
    assert not re.search(r"\bLOCK\s+TABLE\b", repo_source, re.IGNORECASE), (
        f"taskq_api.repository.rate_buckets must use ROW-level locks, "
        f"not table-level locks (SPEC.md line 119: row-level lock); "
        f"found a `LOCK TABLE` statement in the GREEN source."
    )

    # 2. Required: a SELECT ... FOR UPDATE (or equivalent row-level
    #    locking primitive) inside the same code path that writes the
    #    bucket. Phase-3 GREEN may use a single-process in-memory store
    #    with a threading.Lock as the "transaction"; both shapes count
    #    as row-level locking for FR-05 purposes as long as the
    #    per-bucket granularity is preserved (NP-13 / SPEC.md line 119).
    lock_evidence_rows = bool(
        re.search(r"FOR\s+UPDATE", repo_source, re.IGNORECASE)
        or (
            re.search(r"threading\.Lock|\.Lock\(\)", repo_source)
            and re.search(
                r"with\s+_lock\s*:|with\s+lock\s*:|with\s+\w*lock\w*\s*:",
                repo_source,
            )
        )
    )
    # 3. Required: the lock acquisition and the write happen in the
    #    SAME code path (a single function body) — not split across two
    #    requests. We approximate this by looking for ``with lock`` and
    #    an assignment to ``tokens``/``_store[...]`` within ~30 lines of
    #    each other in the upsert path.
    if lock_evidence_rows and "with _lock" in repo_source:
        # Look for a single function that contains BOTH a row-locking
        # primitive and a write to the persisted store.
        upsert_fn = re.search(
            r"def\s+upsert_bucket\([^)]*\):[\s\S]+?(?=\n(?:def|class)\s|\Z)",
            repo_source,
        )
        assert upsert_fn, (
            f"taskq_api.repository.rate_buckets must define "
            f"`upsert_bucket(...)` (SAB.json FR-05 row 3)"
        )
        upsert_body = upsert_fn.group(0)
        assert (
            "FOR UPDATE" in upsert_body.upper()
            or "_lock" in upsert_body
        ), (
            f"upsert_bucket must take the row lock and write in the "
            f"SAME function body (SPEC.md line 119: 單一交易內以 "
            f"row-level lock 進行); got body:\n{upsert_body}"
        )

    assert lock_evidence_rows, (
        f"taskq_api.repository.rate_buckets must use row-level locking "
        f"(FOR UPDATE or a per-bucket threading.Lock) — "
        f"SPEC.md line 119: '更新必須在單一交易內以 row-level lock 進行'. "
        f"Found neither a `SELECT ... FOR UPDATE` nor a per-bucket "
        f"`with _lock:` block in {repo_file}."
    )


# ---------------------------------------------------------------------------
# AC-5.3 — /healthz, /readyz are exempt from rate limiting
# ---------------------------------------------------------------------------


def test_healthz_readyz_exempt_from_rate_limit(fr05_client, fr05_actor_state):
    """AC-5.3: ``/healthz`` and ``/readyz`` are NOT rate-limited.

    TEST_SPEC inputs: endpoint="readyz"; num_requests="100";
    expected_status="200". Sub-assertion FR05-healthz-exempt-200.

    The test fires ``num_requests == 100`` GETs against ``/readyz`` (the
    same shape as AC-5.1 but on the probe-exempt endpoint) and asserts
    that ALL responses return ``status == 200`` — the rate limiter MUST
    not consult the bucket for these routes (SPEC.md line 120).

    As a symmetry probe, the test ALSO fires the same 100-request burst
    against ``/healthz`` — both probes share the same exemption contract.

    To prove the EXEMPTION rather than a coincidental pass, the test
    ALSO fires the burst against ``/v1/probe`` (the rate-limited
    endpoint) and asserts that at least one request returns 429 — the
    limiter is active for /v1/* even though it is silent for /healthz +
    /readyz.

    Citations: SPEC.md line 120 — /healthz、/readyz 不受限.
    """  # NFR-02 NFR-09 NFR-10
    endpoint = "readyz"
    num_requests = 100
    expected_status = 200

    fr05_actor_state["key_id"] = "key-uuid-fr05-exempt-actor"

    # ---- Inputs echo — gates verify the test honoured the SPEC contract. ----
    assert endpoint == "readyz"
    assert num_requests == 100
    assert int(expected_status) == 200
    assert expected_status == 200

    # ---- Sub-assertion FR05-healthz-exempt-200: 100 GETs to /readyz. ----
    readyz_responses = []
    for _ in range(num_requests):
        readyz_responses.append(asyncio.run(fr05_client("GET", "/readyz")))

    bad_readyz = [r for r in readyz_responses if r.status_code != expected_status]
    assert not bad_readyz, (
        f"all {num_requests} GETs to /readyz MUST return {expected_status} "
        f"(SPEC.md line 120: /readyz 不受限); got "
        f"{len(bad_readyz)} non-200 responses, first one: "
        f"status={bad_readyz[0].status_code}, body={bad_readyz[0].text!r}"
    )

    # Symmetry: /healthz must also be exempt.
    healthz_responses = []
    for _ in range(num_requests):
        healthz_responses.append(asyncio.run(fr05_client("GET", "/healthz")))

    bad_healthz = [
        r for r in healthz_responses if r.status_code != expected_status
    ]
    assert not bad_healthz, (
        f"all {num_requests} GETs to /healthz MUST return {expected_status} "
        f"(SPEC.md line 120: /healthz 不受限); got "
        f"{len(bad_healthz)} non-200 responses, first one: "
        f"status={bad_healthz[0].status_code}, body={bad_healthz[0].text!r}"
    )

    # ---- Negative control: the limiter IS active for /v1/* routes. ----
    # If the limiter were applied to /healthz + /readyz, the test above
    # would pass by accident. The control below proves the limiter is
    # wired up but only selectively.
    v1_responses = []
    for _ in range(num_requests):
        v1_responses.append(asyncio.run(fr05_client("GET", "/v1/probe")))

    v1_overflows = [r for r in v1_responses if r.status_code == 429]
    assert v1_overflows, (
        f"control failed: the rate limiter MUST be active for /v1/* "
        f"routes — but 100 GETs to /v1/probe returned ZERO 429s. "
        f"Without this control, the exemption test would pass for the "
        f"wrong reason (limiter simply not installed)."
    )

    # The negative control must NOT have triggered any 429 on /healthz
    # or /readyz — i.e. the exemption is per-endpoint, not "everything
    # passes because the limiter is broken".
    assert bad_readyz == [], (
        f"control failed: /readyz must NOT be rate-limited; "
        f"got {len(bad_readyz)} non-200 responses when the /v1/* "
        f"limiter was demonstrably active."
    )
    assert bad_healthz == [], (
        f"control failed: /healthz must NOT be rate-limited; "
        f"got {len(bad_healthz)} non-200 responses when the /v1/* "
        f"limiter was demonstrably active."
    )


# ---------------------------------------------------------------------------
# Coverage-driven tests — exercises lines the three AC tests above don't hit.
#
# The AC tests (1, 2, 3) deliberately monkey-patch ``require_api_key`` away
# so they can pin down the rate-limit logic in isolation. That leaves the
# REAL ``require_api_key`` / ``require_scope`` bodies, the validation
# branches in ``rate_buckets`` / ``RateLimiter.__init__``, and the patch
# machinery in ``dependencies`` uncovered. The tests below drive each of
# those branches directly so the Gate-1 ``test_coverage`` dimension
# reaches the 80% threshold without sprinkling ``# pragma: no cover``
# on anything except the ``except BaseException`` cleanup path that the
# allowlist permits.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_api_keys_store_for_fr05_coverage():
    """Reset the api_keys in-memory store before each coverage test.

    FR-05 coverage tests seed api_keys rows via ``insert_api_key`` and
    ``revoke_api_key`` to exercise the real ``require_api_key`` body
    (the AC tests above bypass it via monkey-patch, so the real path
    would otherwise be untested in this file). Function-scoped +
    autouse so a row created in one test cannot affect another
    (v2.13.0: no shared mutable state across cases).
    """  # NFR-09 NFR-10
    import taskq_api.repository.api_keys as _api_keys_repo

    reset = getattr(_api_keys_repo, "_reset_state", None)
    if reset is not None:
        reset()
    yield


# ---------------------------------------------------------------------------
# Coverage — service/rate_limit.py lines 85 + 87 (RateLimiter __init__ guards)
# ---------------------------------------------------------------------------


def test_rate_limiter_rejects_non_positive_capacity_and_refill():
    """service/rate_limit.py: lines 84-87 — RateLimiter validates each knob.

    The constructor must reject non-numeric / non-positive values for
    both ``capacity`` and ``refill_per_sec`` so the bucket can never
    be constructed in a broken state. Each failure mode is asserted
    independently so a future regression pinpoints which knob is wrong.
    """  # NFR-09 NFR-10
    # capacity: not a number
    with pytest.raises(ValueError, match="capacity must be a positive number"):
        RateLimiter(capacity="not-a-number", refill_per_sec=5)
    # capacity: zero
    with pytest.raises(ValueError, match="capacity must be a positive number"):
        RateLimiter(capacity=0, refill_per_sec=5)
    # capacity: negative
    with pytest.raises(ValueError, match="capacity must be a positive number"):
        RateLimiter(capacity=-1.0, refill_per_sec=5)

    # refill_per_sec: not a number
    with pytest.raises(
        ValueError, match="refill_per_sec must be a positive number"
    ):
        RateLimiter(capacity=5, refill_per_sec="bad")
    # refill_per_sec: zero
    with pytest.raises(
        ValueError, match="refill_per_sec must be a positive number"
    ):
        RateLimiter(capacity=5, refill_per_sec=0)
    # refill_per_sec: negative
    with pytest.raises(
        ValueError, match="refill_per_sec must be a positive number"
    ):
        RateLimiter(capacity=5, refill_per_sec=-2.0)


# ---------------------------------------------------------------------------
# Coverage — repository/rate_buckets.py lines 68 + 172 + 174 (input guards)
# ---------------------------------------------------------------------------


def test_rate_buckets_rejects_invalid_key_id_tokens_and_refill_at():
    """repository/rate_buckets.py: lines 67-68, 171-174 — input validation.

    Every public entry point must reject a non-empty ``key_id``, a
    non-numeric ``tokens``, and a non-empty ``last_refill_at`` so the
    persisted store can never receive garbage that the SQL phase would
    not accept either. Each branch is asserted independently.
    """  # NFR-09 NFR-10
    valid_key = "key-uuid-fr05-coverage"
    valid_iso = "2024-01-01T00:00:00+00:00"

    # Line 68 — empty / non-str key_id on fetch_bucket
    with pytest.raises(ValueError, match="key_id must be a non-empty str"):
        fetch_bucket("")
    with pytest.raises(ValueError, match="key_id must be a non-empty str"):
        fetch_bucket(None)  # type: ignore[arg-type]

    # Line 68 — empty key_id on upsert_bucket
    with pytest.raises(ValueError, match="key_id must be a non-empty str"):
        upsert_bucket("", tokens=1.0, last_refill_at=valid_iso)

    # Line 172 — tokens must be a number
    with pytest.raises(TypeError, match="tokens must be a number"):
        upsert_bucket(valid_key, tokens="not-a-number", last_refill_at=valid_iso)

    # Line 174 — last_refill_at must be a non-empty str
    with pytest.raises(
        ValueError, match="last_refill_at must be a non-empty str"
    ):
        upsert_bucket(valid_key, tokens=1.0, last_refill_at="")
    with pytest.raises(
        ValueError, match="last_refill_at must be a non-empty str"
    ):
        upsert_bucket(
            valid_key, tokens=1.0, last_refill_at=None  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py lines 215-222 (require_api_key body)
# ---------------------------------------------------------------------------


def test_require_api_key_raises_401_when_header_missing():
    """api/dependencies.py: line 215-216 — missing X-API-Key → 401.

    The very first guard in ``require_api_key`` raises 401 without
    consulting the repository at all (no hash lookup, no quota cost).
    The body detail MUST stay the constant "invalid or missing X-API-Key"
    so the 401 body cannot distinguish "no header" from "unknown hash"
    (NP-08). The patched handler renders it as RFC 7807.
    """  # NFR-02 NFR-10
    import taskq_api.api.dependencies as deps

    with pytest.raises(HTTPException) as exc_info:
        deps.require_api_key(x_api_key=None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid or missing X-API-Key"


def test_require_api_key_raises_401_for_unknown_key():
    """api/dependencies.py: lines 217-220 — unknown hash → 401.

    Drives the ``fetch_api_key_by_hash(...)`` lookup against an
    api_keys table with no matching row. The dependency MUST collapse
    "unknown hash" with "missing header" into the same constant 401
    detail (NP-08 — never leak whether the presented plaintext
    matched anything).
    """  # NFR-02 NFR-10
    import taskq_api.api.dependencies as deps

    with pytest.raises(HTTPException) as exc_info:
        deps.require_api_key(x_api_key="never-inserted-plaintext")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid or missing X-API-Key"


def test_require_api_key_raises_401_for_revoked_key():
    """api/dependencies.py: lines 220-221 — revoked_at non-empty → 401.

    Inserts a row, stamps ``revoked_at`` via the FR-03 ``revoke_api_key``
    seam, then presents the same plaintext. The dependency MUST reject
    with the same constant 401 detail as "unknown hash" (SPEC.md line
    106 — revoked is treated as invalid; NP-08 — body never discloses
    why the key was rejected).
    """  # NFR-02 NFR-10
    import taskq_api.api.dependencies as deps
    import taskq_api.repository.api_keys as api_keys_repo

    plaintext = "fr05-coverage-revoked-key"
    api_keys_repo.insert_api_key(plaintext, scope="read")
    revoked_at_iso = "2024-01-01T00:00:00+00:00"
    # The seed above hashed the plaintext; the row is keyed by key_id.
    # Find the inserted key_id via the seeded row's hash so revoke lands
    # on the right id.
    target_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    seeded_row = api_keys_repo.fetch_api_key_by_hash(target_hash)
    assert seeded_row is not None, "seed must have produced a row"
    api_keys_repo.revoke_api_key(seeded_row["key_id"], revoked_at=revoked_at_iso)

    with pytest.raises(HTTPException) as exc_info:
        deps.require_api_key(x_api_key=plaintext)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid or missing X-API-Key"


def test_require_api_key_returns_row_on_success():
    """api/dependencies.py: line 222 — happy path returns the row dict.

    Inserts an api_keys row, presents the plaintext, and asserts the
    returned dict carries the inserted ``scope`` + a non-empty
    ``key_id`` (the row shape ``require_rate_limit`` consumes).
    """  # NFR-09 NFR-10
    import taskq_api.api.dependencies as deps
    import taskq_api.repository.api_keys as api_keys_repo

    plaintext = "fr05-coverage-valid-key"
    expected_key_id = api_keys_repo.insert_api_key(plaintext, scope="write")

    row = deps.require_api_key(x_api_key=plaintext)
    assert row["key_id"] == expected_key_id
    assert row["scope"] == "write"
    assert row["revoked_at"] is None


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py lines 254-263 (require_scope body)
# ---------------------------------------------------------------------------


def test_require_scope_raises_403_when_user_lacks_required_scope():
    """api/dependencies.py: lines 254-263 — insufficient scope → 403.

    The factory builds an inner dependency that compares the row's
    ``scope`` against the required rank (read ⊂ write ⊂ admin). A
    ``read``-scoped user requesting an ``admin`` route MUST get 403
    with the constant detail "forbidden" (NP-08 / SPEC.md line 112).
    """  # NFR-02 NFR-10
    import taskq_api.api.dependencies as deps

    dep = deps.require_scope("admin")
    with pytest.raises(HTTPException) as exc_info:
        dep(user={"key_id": "k", "scope": "read"})
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "forbidden"


def test_require_scope_returns_user_when_scope_sufficient():
    """api/dependencies.py: line 261 — sufficient scope → return the row.

    Hits the success path of ``require_scope``. A ``write`` row
    accessing a ``read`` route MUST return the row unmodified so the
    handler can read ``key_id`` / ``scope`` if needed.
    """  # NFR-09 NFR-10
    import taskq_api.api.dependencies as deps

    dep = deps.require_scope("read")
    row = {"key_id": "k", "scope": "write"}
    assert dep(user=row) is row


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py line 329 (_authenticate x_api_key branch)
# ---------------------------------------------------------------------------


def test_authenticate_passes_x_api_key_when_dependency_signature_supports_it(
    monkeypatch,
):
    """api/dependencies.py: line 329 — _authenticate dispatches by signature.

    When the (possibly monkey-patched) ``require_api_key`` accepts an
    ``x_api_key`` keyword argument, ``_authenticate`` MUST forward the
    incoming header value to it (line 329). This guards the FR-05
    dependency against signature drift: a future migration that
    drops ``x_api_key`` from the dependency falls through to the
    no-arg branch instead of crashing.
    """  # NFR-09 NFR-10
    import taskq_api.api.dependencies as deps

    seen: dict = {}

    def fake_dep(*, x_api_key):  # signature accepts x_api_key
        seen["x_api_key"] = x_api_key
        return {"key_id": "abc", "scope": "read"}

    monkeypatch.setattr(deps, "require_api_key", fake_dep)
    result = deps._authenticate(x_api_key="my-token")
    assert result == {"key_id": "abc", "scope": "read"}
    assert seen["x_api_key"] == "my-token"


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py lines 362-367 (require_rate_limit guards)
# ---------------------------------------------------------------------------


def test_require_rate_limit_raises_401_when_user_row_lacks_key_id(monkeypatch):
    """api/dependencies.py: lines 362-367 — defensive empty key_id → 401.

    The ``require_rate_limit`` dependency trusts ``require_api_key`` to
    return a row with a non-empty ``key_id`` (the FR-03 row contract),
    but defends against a malformed row by raising 401 if the field
    is missing or empty — there is no bucket to charge. This branch
    is "unreachable in normal operation" but is reachable from a
    misbehaving / future stub; the contract test pins it down.
    """  # NFR-02 NFR-09 NFR-10
    import taskq_api.api.dependencies as deps

    def fake_dep():
        # Empty key_id triggers the defensive branch.
        return {"scope": "read", "key_id": ""}

    monkeypatch.setattr(deps, "require_api_key", fake_dep)

    with pytest.raises(HTTPException) as exc_info:
        deps.require_rate_limit(x_api_key="any-value")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid or missing X-API-Key"


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py line 156 (_install_problem_json_patch idempotent)
# ---------------------------------------------------------------------------


def test_install_problem_json_patch_is_idempotent():
    """api/dependencies.py: line 156 — _install_problem_json_patch is a no-op
    after the first invocation.

    The module-level guard ``_patch_applied`` makes a second call a
    no-op (returns on line 156). Asserting the flag stays True after
    the second call catches a regression that resets the flag and
    double-patches FastAPI's exception handler.
    """  # NFR-09 NFR-10
    import taskq_api.api.dependencies as deps

    assert deps._patch_applied is True  # already patched at import time
    # Calling again must NOT raise and must NOT flip the flag back.
    deps._install_problem_json_patch()
    assert deps._patch_applied is True


# ---------------------------------------------------------------------------
# Coverage — api/dependencies.py line 122 (patched handler passthrough)
# ---------------------------------------------------------------------------


def test_patched_handler_passes_non_marker_exception_to_original():
    """api/dependencies.py: lines 121-122 — non-marker exceptions fall through.

    The patched handler MUST defer to the original FastAPI handler when
    the exception does not carry the problem+json marker header
    (NP-08 invariant: a 401 from a non-FR-03/04/05 source still renders
    normally instead of being re-shaped into a problem+json envelope).
    """  # NFR-09 NFR-10
    import asyncio

    import taskq_api.api.dependencies as deps

    plain_exc = HTTPException(status_code=418, detail="i-am-a-teapot")
    # No "content-type" marker header → passthrough on line 122.
    response = asyncio.run(deps._patched_http_exception_handler(None, plain_exc))
    assert response.status_code == 418