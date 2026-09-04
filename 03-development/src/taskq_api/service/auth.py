"""[FR-03] API key hashing and constant-time comparison.

This module exposes two pure helpers used by the FR-03 GREEN contract:

- ``hash_api_key(plaintext)`` returns the 64-char lowercase hex SHA-256 of
  the plaintext. Plaintext is never stored; only the hex digest lands in
  ``api_keys.key_hash`` (SPEC.md line 104).
- ``compare_api_keys(plaintext, stored_hash)`` returns ``True`` iff the
  SHA-256 of ``plaintext`` matches ``stored_hash``. The comparison MUST
  use ``hmac.compare_digest`` (SPEC.md line 104 / NP-14) because a
  short-circuit ``==`` would let an attacker enumerate valid hashes via
  timing.

Both functions are deliberately tiny — the rest of the FR-03 pipeline
(resolving an X-API-Key header against the ``api_keys`` table, handling
``revoked_at``, returning problem+json) lives in
``taskq_api.api.dependencies`` so that this module can stay
import-graph-safe for the CLI (``taskq_api.__main__`` does not pull in
FastAPI just to create a key).

Citations:
- SPEC.md line 104 — SHA-256 雜湊儲存於 api_keys;常數時間 hmac.compare_digest.
- TEST_SPEC.md §1 FR-03 row 2/3 — hash is 64 hex chars and constant-time.
- NFR-02 — plaintext never persisted; comparison must not leak timing.
"""  # NFR-02 NFR-09
from __future__ import annotations

import hashlib
import hmac


def hash_api_key(plaintext: str) -> str:
    """Return the 64-char lowercase hex SHA-256 of ``plaintext``.

    Citations: SPEC.md line 104 — SHA-256 雜湊儲存於 api_keys.key_hash;
    SPEC.md §8 #18 — key_hash 為 64 hex (NFR-02); TEST_SPEC.md §1 FR-03
    row 2 — 64-char hex.
    """  # NFR-02 NFR-09
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a str")
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def compare_api_keys(plaintext: str, stored_hash: str) -> bool:
    """Constant-time comparison of a candidate plaintext against a stored hash.

    The candidate plaintext is hashed with SHA-256 first, then compared to
    ``stored_hash`` via ``hmac.compare_digest`` so the comparison time
    depends only on argument length, not on where the first mismatch
    occurs. Returns a plain ``bool`` so callers can chain it with normal
    Python truthiness without worrying about ``NotImplemented``.

    Citations: SPEC.md line 104 — 比對用 hmac.compare_digest (常數時間);
    TEST_SPEC.md §1 FR-03 row 3 — must call ``hmac.compare_digest``;
    NFR-02 — no plaintext-equality shortcut.
    """  # NFR-02 NFR-09
    if not isinstance(plaintext, str) or not isinstance(stored_hash, str):
        raise TypeError("compare_api_keys requires two str arguments")
    candidate = hash_api_key(plaintext)
    # Attribute form, not ``from hmac import compare_digest`` — the FR-03
    # spy patches ``hmac.compare_digest`` at the stdlib module level and
    # would otherwise be bypassed.
    return bool(hmac.compare_digest(candidate, stored_hash))
