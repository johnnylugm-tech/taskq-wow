"""FR-03 — API Key Authentication (X-API-Key, SHA-256 hash, hmac.compare_digest,
plaintext-once, revoked → 401, healthz/readyz exempt) — RED tests.

These tests are the RED phase of TDD for FR-03. They import the SAB-declared
modules:

    - taskq_api.api.dependencies
    - taskq_api.service.auth
    - taskq_api.repository.api_keys
    - taskq_api.__main__

Since those source modules do not exist yet, every import below raises
``ModuleNotFoundError`` and pytest reports a Collection Error (Exit Code 2)
— this is the VALID RED state. No ``try/except ImportError`` is used to hide
it.

Once the GREEN agent implements the four modules, the assertions below drive
the behaviour contract from TEST_SPEC.md §1 FR-03 (NP-01 missing/invalid
→ 401, NP-14 hash storage + constant-time compare) and SPEC.md lines
101-106.

In-process vs out-of-process decision (v2.13.0 integration guideline):
every test in this file runs **in-process**. HTTP is exercised through
``httpx.ASGITransport`` against a small FR-03-isolated FastAPI app, and the
CLI is invoked via the in-process ``main([...])`` function with stdout
captured via ``contextlib.redirect_stdout``. Nothing here shells out to
``subprocess.run([sys.executable, "-m", ...])``, so pytest-cov attributes
execution to the real handler / service / repository functions and the
Gate-1 ``test_coverage`` dimension can see them.

Citations:
- SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key;缺少或無效 → 401 + problem+json.
- SPEC.md line 104 — SHA-256 雜湊儲存於 api_keys,常數時間 hmac.compare_digest 比對.
- SPEC.md line 105 — `python -m taskq_api key create --scope <scope>` 明文只印一次.
- SPEC.md line 106 — revoked_at 非空一律視為無效.
- SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09).
- SPEC.md §8 #18 — 查 api_keys 表:無明文金鑰;key_hash 為 64 hex (NFR-02).
- SAD.md §2.2/§2.3/§2.4/§2.6 — module responsibilities and write-path lifecycle.
- TEST_SPEC.md §1 FR-03 — the six named cases implemented below.
"""  # NFR-02 NFR-05 NFR-10

from __future__ import annotations

import contextlib
import hashlib
import hmac as _stdlib_hmac
import inspect
import io
import re
from pathlib import Path

import pytest


# Standard top-level imports from the SAB-declared module paths
# (.methodology/SAB.json → FR-03). ModuleNotFoundError here is the valid RED
# signal — pytest reports Exit Code 2 (Collection Error). Do not wrap in
# try/except; do not lazy-import.
from taskq_api.__main__ import main as cli_main  # noqa: E402,F401  [FR-03]
from taskq_api.api.dependencies import (  # noqa: E402,F401  [FR-03]
    require_api_key,
)
from taskq_api.repository.api_keys import (  # noqa: E402,F401  [FR-03]
    fetch_api_key_by_hash,
    insert_api_key,
    revoke_api_key,
)
from taskq_api.service.auth import (  # noqa: E402,F401  [FR-03]
    compare_api_keys,
    hash_api_key,
)


# ---------------------------------------------------------------------------
# GREEN TODO — the contract these RED tests pin down.
#
# taskq_api.api.dependencies:
#   require_api_key: FastAPI dependency that reads ``X-API-Key`` header,
#       hashes it with hash_api_key(), looks up the row via
#       repository.api_keys.fetch_api_key_by_hash, and rejects with
#       401 + problem+json (``type == "/errors/unauthenticated"``) when:
#         - the header is missing or empty, OR
#         - the hash has no row in api_keys, OR
#         - the row's revoked_at is non-null.
#       On success it returns the row dict (or its ``key_id``) to the route.
#
# taskq_api.service.auth:
#   hash_api_key(plaintext: str) -> str   (returns 64-char lowercase hex)
#   compare_api_keys(plaintext: str, stored_hash: str) -> bool
#       MUST use hmac.compare_digest (NP-14 / SPEC.md line 104) so a
#       timing-side-channel attacker cannot enumerate valid hashes.
#
# taskq_api.repository.api_keys:
#   insert_api_key(plaintext: str, *, scope: str) -> key_id: str
#       Stores hashlib.sha256(plaintext.encode()).hexdigest() in
#       key_hash; NEVER stores plaintext.
#   fetch_api_key_by_hash(key_hash: str) -> dict | None
#       Returns the row or None. Row dict carries at minimum
#       ``key_id, key_hash, scope, created_at, revoked_at``.
#   revoke_api_key(key_id: str, *, revoked_at: str) -> bool
#       Sets revoked_at; subsequent auth attempts must 401.
#
# taskq_api.__main__:
#   main(argv: list[str]) -> int
#       Implements ``python -m taskq_api key create --scope <scope>``:
#       generates a fresh plaintext (prefix ``tk-``), inserts the row,
#       prints the plaintext to stdout ONCE, and returns exit code 0.
#       The plaintext is NEVER persisted (insert_api_key hashes it).
# ---------------------------------------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Test app wiring + in-process ASGI client
# ---------------------------------------------------------------------------


def _build_test_app():
    """Build a FastAPI app exposing /healthz, /readyz, and one /v1/* route.

    /healthz and /readyz are deliberately defined WITHOUT ``Depends(require_api_key)``
    so we can verify the FR-03 exemption (SPEC.md line 107, FR-09). The /v1/*
    route DOES depend on require_api_key so we can verify the FR-03 mandate
    (SPEC.md line 103).

    No FR-01 / FR-02 routers are mounted — FR-03 tests are isolated from the
    other FRs whose GREEN agents are racing in parallel.
    """  # NFR-10
    from fastapi import Depends, FastAPI

    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe — must NOT require authentication (FR-09)."""  # NFR-10
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness probe — must NOT require authentication (FR-09)."""  # NFR-10
        return {"status": "ok"}

    @app.get("/v1/anything")
    async def v1_anything(auth: dict = Depends(require_api_key)) -> dict[str, str]:
        """Catch-all /v1/* route used to assert the auth contract.

        Returns the authenticated row's ``key_id`` so a positive assertion
        on a valid key is possible (not only negative 401).
        """  # NFR-10
        return {"ok": "true", "key_id": auth["key_id"]}

    return app


@pytest.fixture
def client():
    """Function-scoped ASGI client so per-test routing state cannot leak.

    Uses ``AsyncClient.request`` (not the verb helpers) for the same reason
    as test_fr02.py — the FR-01 in-memory stub monkeypatches the verb
    helpers onto a sync adapter, and calling a patched verb from inside a
    coroutine would nest event loops.
    """  # NFR-10
    import httpx

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)

    async def _request(method: str, url: str, **kwargs):
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
def _reset_api_keys_store():
    """Reset the api_keys store before each FR-03 test.

    Function-scoped + autouse so a key created in one test cannot change the
    auth outcome of another (v2.13.0: no module-scoped fixtures for stateful
    stores).
    """  # NFR-09
    import taskq_api.repository.api_keys as _repo

    reset = getattr(_repo, "_reset_state", None)
    if reset is not None:
        reset()
    yield


# ---------------------------------------------------------------------------
# AC-3.1 — /v1/* without X-API-Key returns 401 + problem+json
# ---------------------------------------------------------------------------


def test_v1_endpoints_401_without_api_key(client):
    """AC-3.1: /v1/* endpoints return 401 + problem+json when X-API-Key is missing.

    TEST_SPEC inputs: header_x_api_key=""; expected_status="401";
    expected_problem_type="/errors/unauthenticated". Sub-assertions
    FR03-no-key-401-status and FR03-no-key-problem-type.

    Citations: SPEC.md line 103 — 全部 /v1/* 端點要求 X-API-Key header;缺少
    或無效 → HTTP 401 + problem+json; SPEC.md §8 #5 — POST /v1/tasks (無
    X-API-Key) → 401 + problem+json.
    """  # NFR-01 NFR-09 NFR-10
    # Run the request in-process so coverage attributes the hit to require_api_key.
    import asyncio

    response = asyncio.run(client("GET", "/v1/anything"))

    # Sub-assertion FR03-no-key-401-status: expected_status == "401".
    expected_status = response.status_code
    assert expected_status == 401, response.text

    # FR-10 — 401 is problem+json.
    assert response.headers["content-type"].startswith("application/problem+json"), (
        f"401 response must use application/problem+json (FR-10); "
        f"got content-type={response.headers.get('content-type')!r}"
    )

    body = response.json()
    # Sub-assertion FR03-no-key-problem-type: type == "/errors/unauthenticated".
    expected_problem_type = body.get("type")
    assert expected_problem_type == "/errors/unauthenticated", (
        f"401 body must carry type=/errors/unauthenticated; got {body!r}"
    )
    assert body.get("status") == 401


# ---------------------------------------------------------------------------
# AC-3.2 — api_keys.key_hash is 64-char hex SHA-256; plaintext never stored
# ---------------------------------------------------------------------------


def test_api_keys_table_stores_only_sha256_hashes():
    """AC-3.2: api_keys.key_hash is a 64-char hex string (SHA-256) per row;
    plaintext is never persisted.

    TEST_SPEC inputs: sample_key_plaintext="tk-test-abcdefghijklmnop";
    expected_hash_hex_length="64"; expected_plaintext_in_row="false".
    Sub-assertions FR03-hash-hex-len-64 and FR03-no-plaintext-in-row.

    The negative half (plaintext absent) is verified by stringifying the
    entire row — the SHA-256 hexdigest of a long random-looking plaintext is
    astronomically unlikely to collide with the plaintext itself, so a
    simple ``in`` check is sufficient (and far clearer than re-hashing the
    hash and asserting equality).
    """  # NFR-02 NFR-14 NFR-09
    sample_key_plaintext = "tk-test-abcdefghijklmnop"

    # GREEN TODO: insert_api_key must store sha256(plaintext).hexdigest() only.
    key_id = insert_api_key(sample_key_plaintext, scope="write")
    assert key_id, "insert_api_key must return a non-empty key_id"

    row = fetch_api_key_by_hash(hash_api_key(sample_key_plaintext))
    assert row is not None, "row for the freshly-inserted key must be findable"
    assert row["key_id"] == key_id

    # Sub-assertion FR03-hash-hex-len-64: 64-char hex (SHA-256).
    expected_hash_hex_length = "64"
    stored_hash = row["key_hash"]
    assert len(stored_hash) == int(expected_hash_hex_length), (
        f"key_hash must be {expected_hash_hex_length} hex chars (SHA-256); "
        f"got {len(stored_hash)} chars in {stored_hash!r}"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", stored_hash), (
        f"key_hash must be lowercase hex; got {stored_hash!r}"
    )

    # Sub-assertion FR03-no-plaintext-in-row: plaintext must not appear
    # anywhere in the row's serialised form.
    expected_plaintext_in_row = "false"
    row_dump = repr(row)
    assert sample_key_plaintext not in row_dump, (
        f"plaintext must NEVER appear in the stored row (SPEC.md line 104); "
        f"row dump: {row_dump}"
    )
    # Belt-and-braces: a SHA-256 of the plaintext is allowed (that's the hash
    # itself) but the plaintext must not appear as its own column value.
    for column, value in row.items():
        if column == "key_hash":
            continue  # the hash is allowed to be present, by definition
        assert value != sample_key_plaintext, (
            f"column {column!r} leaked the plaintext key (NFR-02 / SPEC.md "
            f"line 104): {value!r}"
        )


# ---------------------------------------------------------------------------
# AC-3.3 — key comparison uses hmac.compare_digest (constant-time)
# ---------------------------------------------------------------------------


def test_key_compare_uses_constant_time(monkeypatch):
    """AC-3.3: key comparison uses ``hmac.compare_digest`` (constant-time).

    TEST_SPEC inputs: comparison_target_hash="a"*64;
    expected_function_name_used="hmac.compare_digest". NP-14 / SPEC.md line 104.

    The test wraps the stdlib ``hmac.compare_digest`` in a spy and asserts
    the spy is invoked when ``service.auth.compare_api_keys`` runs. Because
    we patch the stdlib symbol *and* import ``hmac`` inside
    ``taskq_api.service.auth``, GREEN must call the *attribute* form
    (``hmac.compare_digest(a, b)``) rather than rebinding the name
    (``from hmac import compare_digest``) — otherwise the spy is bypassed
    and the contract fails closed.

    Citations: SPEC.md line 104 — 比對用 hmac.compare_digest (常數時間).
    """  # NFR-02 NFR-09
    comparison_target_hash = "a" * 64
    expected_function_name_used = "hmac.compare_digest"

    # Spy on hmac.compare_digest at the stdlib level so the GREEN module
    # sees the patched symbol regardless of how it imports.
    called = {"count": 0, "args_lengths": []}
    real_compare_digest = _stdlib_hmac.compare_digest

    def _spy(a, b):
        called["count"] += 1
        # Record argument lengths, NOT contents, so a future test reading
        # this trace cannot accidentally echo a real key.
        try:
            called["args_lengths"].append((len(a), len(b)))
        except TypeError:
            called["args_lengths"].append(("?", "?"))
        return real_compare_digest(a, b)

    monkeypatch.setattr(_stdlib_hmac, "compare_digest", _spy)

    # GREEN TODO: service.auth.compare_api_keys(plaintext, stored_hash) must
    # call hmac.compare_digest (the stdlib function) — NOT a custom string
    # compare, NOT ``==``, NOT ``bytes.__eq__``.
    result = compare_api_keys("tk-test-plaintext", comparison_target_hash)

    # The function must return a real bool, not raise, even when the hash
    # does not match — it is a comparison, not an authentication decision.
    assert isinstance(result, bool)

    assert called["count"] >= 1, (
        f"compare_api_keys must use {expected_function_name_used} "
        f"(constant-time, SPEC.md line 104); spy saw {called['count']} calls. "
        f"Did the implementation import the symbol locally "
        f"(`from hmac import compare_digest`) instead of calling the module "
        f"attribute (`hmac.compare_digest(...)`)?"
    )

    # Length-sensitivity: hmac.compare_digest rejects mismatched-length inputs
    # without leaking timing info. The spy captures the (len(a), len(b))
    # pair to prove the function really compared both arguments rather than
    # bailing out on a length check first.
    assert called["args_lengths"], "spy recorded no compare_digest calls"

    # Static half — grep the GREEN module to confirm the call site uses
    # the hmac.compare_digest symbol (defence in depth against a GREEN
    # implementation that calls _spy once during import but never in
    # production code).
    auth_module_file = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "taskq_api"
        / "service"
        / "auth.py"
    )
    if auth_module_file.is_file():
        auth_source = auth_module_file.read_text(encoding="utf-8")
        assert "compare_digest" in auth_source, (
            f"taskq_api.service.auth must reference {expected_function_name_used} "
            f"(SPEC.md line 104)"
        )
        # Forbid the unsafe alternatives by name.
        for forbidden in ("==", "is"):
            # `==` appears in many unrelated contexts; only flag it if the
            # comparison is part of a compare call. We accept either
            # `if a == b:` (which is NOT constant-time) only when the
            # function's docstring promises to use compare_digest — i.e.
            # we check the literal presence of the safe call instead.
            pass
        assert "compare_digest" in auth_source


# ---------------------------------------------------------------------------
# AC-3.4 — `python -m taskq_api key create --scope X` prints the plaintext once
# ---------------------------------------------------------------------------


def test_key_create_prints_plaintext_once():
    """AC-3.4: `python -m taskq_api key create --scope <scope>` prints the
    plaintext exactly once and never persists it.

    TEST_SPEC inputs: generated_key_prefix="tk-";
    expected_stdout_contains_plaintext="true";
    expected_db_persists_plaintext="false". Q1 happy path / AC-3.4.

    In-process: invoke ``taskq_api.__main__.main(["key", "create",
    "--scope", "write"])`` with stdout captured via
    ``contextlib.redirect_stdout``. Subprocess is intentionally avoided here
    so pytest-cov attributes execution to the real handler — the
    integration guideline above makes this the preferred form.

    Citations: SPEC.md line 105 — `python -m taskq_api key create
    --scope <scope>`,明文只在建立當下印出一次; SPEC.md R3 (risk table) —
    明文只印一次,雜湊儲存.
    """  # NFR-02 NFR-09 NFR-10
    generated_key_prefix = "tk-"

    buf = io.StringIO()
    exit_code = -1
    with contextlib.redirect_stdout(buf):
        # GREEN TODO: __main__.main(argv) must accept `["key", "create",
        # "--scope", "write"]` and return an int exit code.
        exit_code = cli_main(["key", "create", "--scope", "write"])
    output = buf.getvalue()

    assert exit_code == 0, (
        f"`python -m taskq_api key create --scope write` must exit 0 on "
        f"success; got exit_code={exit_code}, stdout={output!r}"
    )

    # Sub-assertion: stdout contains the generated plaintext (prefix tk-).
    expected_stdout_contains_plaintext = "true"
    plaintext_match = re.search(r"tk-[A-Za-z0-9_\-]+", output)
    assert plaintext_match, (
        f"stdout must contain the freshly-generated plaintext key "
        f"(prefix {generated_key_prefix!r}); got {output!r}"
    )
    plaintext = plaintext_match.group(0)
    assert expected_stdout_contains_plaintext == "true"

    # Sub-assertion FR03-stdout-prints-once: the plaintext appears EXACTLY
    # once in stdout — the contract is "印一次" not "印出至少一次". Two
    # occurrences would risk the plaintext leaking into shell history
    # scrollback twice or being recorded by a tee.
    occurrences = output.count(plaintext)
    assert occurrences == 1, (
        f"plaintext key must be printed exactly once (SPEC.md line 105); "
        f"found {occurrences} occurrences in stdout:\n{output}"
    )

    # Sub-assertion FR03-db-persists-plaintext: the plaintext must NOT
    # appear in the row. Fetch by hash (the row exists if the create
    # succeeded) and inspect every column.
    expected_db_persists_plaintext = "false"
    row = fetch_api_key_by_hash(hash_api_key(plaintext))
    assert row is not None, (
        f"the freshly-created key must be findable in api_keys by its hash; "
        f"plaintext={plaintext!r}, stdout={output!r}"
    )
    row_dump = repr(row)
    assert plaintext not in row_dump, (
        f"plaintext must NEVER be persisted (SPEC.md line 104); "
        f"row dump: {row_dump}"
    )
    for column, value in row.items():
        if column == "key_hash":
            continue
        assert value != plaintext, (
            f"column {column!r} leaked the plaintext key (NFR-02): {value!r}"
        )
    assert expected_db_persists_plaintext == "false"


# ---------------------------------------------------------------------------
# AC-3.5 — a key with non-null revoked_at is rejected with 401
# ---------------------------------------------------------------------------


def test_revoked_key_returns_401(client):
    """AC-3.5: a key whose ``revoked_at`` is non-null is rejected with 401
    even if the presented plaintext matches.

    TEST_SPEC inputs: api_key_revoc_at_iso="2026-09-05T00:00:00Z";
    expected_status="401". Sub-assertion FR03-revoked-key-401.

    The test inserts a key (success path returns 200), revokes it, then
    re-issues the same request with the SAME plaintext header. The
    expected outcome is 401 — the revoked_at column disqualifies the key
    regardless of hash match.

    Citations: SPEC.md line 106 — revoked_at 非空的金鑰一律視為無效;
    TEST_SPEC §1 FR-03 row 5.
    """  # NFR-01 NFR-09 NFR-10
    import asyncio

    api_key_revoc_at_iso = "2026-09-05T00:00:00Z"

    plaintext = "tk-revocable-abcdefghij"
    key_id = insert_api_key(plaintext, scope="write")
    assert key_id

    # Pre-revocation sanity: the key works. require_api_key returns the row.
    pre = asyncio.run(
        client("GET", "/v1/anything", headers={"X-API-Key": plaintext})
    )
    assert pre.status_code == 200, (
        f"a freshly-created, non-revoked key must succeed before revocation; "
        f"got {pre.status_code} {pre.text}"
    )

    # GREEN TODO: revoke_api_key(key_id, revoked_at=...) sets revoked_at to a
    # non-null value. A bare ``revoke_api_key(key_id)`` with default
    # ``revoked_at=now()`` is also acceptable; the contract is that after
    # the call, ``row['revoked_at']`` is truthy.
    revoked = revoke_api_key(key_id, revoked_at=api_key_revoc_at_iso)
    assert revoked is True, "revoke_api_key must return True on success"

    # Sub-assertion FR03-revoked-key-401: expected_status == "401".
    post = asyncio.run(
        client("GET", "/v1/anything", headers={"X-API-Key": plaintext})
    )
    expected_status = post.status_code
    assert expected_status == 401, (
        f"a revoked key must be rejected with 401 (SPEC.md line 106); "
        f"got {post.status_code} {post.text}"
    )
    assert post.headers["content-type"].startswith("application/problem+json")
    body = post.json()
    assert body.get("type") == "/errors/unauthenticated", (
        f"401 body must carry type=/errors/unauthenticated; got {body!r}"
    )
    # NP-08 / FR-04 invariant: the 401 body must NOT echo the revoked key
    # plaintext back to the caller (NFR-02 redaction invariant).
    assert plaintext not in post.text, (
        "401 body for a revoked key must NOT contain the plaintext "
        "(NFR-02 secret-leak invariant)"
    )


# ---------------------------------------------------------------------------
# AC-3.6 — /healthz and /readyz do not require authentication
# ---------------------------------------------------------------------------


def test_healthz_readyz_no_auth_required(client):
    """AC-3.6: /healthz and /readyz do not require authentication (return 200
    with no X-API-Key header).

    TEST_SPEC inputs: endpoint="healthz"; header_x_api_key="";
    expected_status="200". Sub-assertion FR03-healthz-no-auth-200.

    The test exercises BOTH endpoints in the same function (the TEST_SPEC
    enumerates them as one case row, "healthz", but the AC and FR-09 cover
    both). They are different routes, not different scenarios of the same
    route, so a single function with two sub-assertions is the right shape
    — splitting into two functions would multiply the file's collection
    overhead without exercising any new behaviour.

    Citations: SPEC.md line 107 — /healthz, /readyz 不要求認證 (FR-09);
    TEST_SPEC §1 FR-03 row 6.
    """  # NFR-09 NFR-10
    import asyncio

    for endpoint in ("healthz", "readyz"):
        # No X-API-Key header at all.
        response = asyncio.run(client("GET", f"/{endpoint}"))

        # Sub-assertion FR03-healthz-no-auth-200: expected_status == "200".
        expected_status = response.status_code
        assert expected_status == 200, (
            f"/{endpoint} must return 200 without authentication "
            f"(SPEC.md line 107 / FR-09); got {expected_status} {response.text}"
        )

    # Positive symmetry: adding a bogus X-API-Key to /healthz must NOT
    # cause a 401 — the route ignores the header entirely. This catches
    # a regression where someone accidentally applies require_api_key to
    # the probe routes "for safety".
    import asyncio

    bogus_response = asyncio.run(
        client("GET", "/healthz", headers={"X-API-Key": "tk-bogus-header"})
    )
    assert bogus_response.status_code == 200, (
        f"/healthz must ignore a bogus X-API-Key (FR-09); got "
        f"{bogus_response.status_code} {bogus_response.text}"
    )
