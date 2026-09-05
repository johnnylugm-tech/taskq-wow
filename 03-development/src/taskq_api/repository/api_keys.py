"""[FR-03] In-memory ``api_keys`` repository — Phase-3 GREEN backing store.

Mirrors the shape of ``taskq_api.repository.tasks`` so the Phase-4 SQL
swap (SQLAlchemy 2.x + Alembic per FR-06) can replace this module
without touching the rest of the codebase. Three functions make up the
contract:

- ``insert_api_key(plaintext, *, scope)`` hashes the plaintext via
  ``taskq_api.service.auth.hash_api_key`` and stores ONLY the 64-char
  hex digest plus metadata. Plaintext is never retained (SPEC.md line
  104 / NFR-02).
- ``fetch_api_key_by_hash(key_hash)`` returns the row or ``None``. The
  row carries ``key_id, key_hash, scope, created_at, revoked_at`` so
  downstream code can do the revocation check (SPEC.md line 106) without
  a second round-trip.
- ``revoke_api_key(key_id, *, revoked_at)`` stamps ``revoked_at`` and
  returns ``True`` on success, ``False`` if the id is unknown. A
  revoked key is treated as invalid by ``require_api_key`` regardless of
  hash match (SPEC.md line 106).

Citations:
- SPEC.md line 104 — SHA-256 雜湊儲存於 api_keys.key_hash;plaintext never stored.
- SPEC.md line 105 — `python -m taskq_api key create --scope <scope>` 印一次.
- SPEC.md line 106 — revoked_at 非空的金鑰一律視為無效.
- SPEC.md §8 #18 — 查 api_keys:無明文;key_hash 64 hex.
- SAD.md §2.4 — repository is the persistence boundary.
- TEST_SPEC.md §1 FR-03 — six-case contract.
- NFR-02 / NFR-04 — no plaintext in any persisted column.
"""  # NFR-02 NFR-04 NFR-11
from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

# NOTE: this module MUST NOT import from ``taskq_api.service`` — the SAB
# layer contract in ``.importlinter`` reserves ``service`` as a layer
# above the persistence boundary. SHA-256 is a one-liner over ``hashlib``
# so we inline it here instead of depending on ``service.auth.hash_api_key``;
# ``service.auth`` keeps its hash helper for callers in higher layers
# (the FastAPI dependency in ``api.dependencies`` and the CLI).

_lock = threading.Lock()
_store: dict[str, dict] = {}


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 with microsecond precision.

    Citations: SPEC.md line 104 — created_at / revoked_at timestamp
    columns.
    """
    return datetime.now(timezone.utc).isoformat()


def _reset_state() -> None:
    """Reset the in-memory store (test seam — FR-03 RED tests only).

    Citations: ``03-development/tests/test_fr03.py`` autouse fixture
    ``_reset_api_keys_store``; v2.13.0 — no module-scoped fixtures for
    stateful stores.
    """
    global _store
    _store = {}


def insert_api_key(plaintext: str, *, scope: str) -> str:
    """Insert a new api_keys row and return the generated ``key_id``.

    Plaintext is hashed synchronously and then discarded — the row
    stores only the 64-char hex digest plus ``scope``, ``created_at``,
    and ``revoked_at`` (initially ``None``).

    Citations: SPEC.md line 104 — SHA-256 雜湊儲存;plaintext never
    persisted; TEST_SPEC.md §1 FR-03 row 2 — key_hash is 64 hex.
    """  # NFR-02 NFR-09
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext must be a non-empty str")
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty str")
    key_id = str(uuid.uuid4())
    row = {
        "key_id": key_id,
        "key_hash": hashlib.sha256(plaintext.encode("utf-8")).hexdigest(),
        "scope": scope,
        "created_at": _now_iso(),
        "revoked_at": None,
    }
    with _lock:
        _store[key_id] = row
    return key_id


def fetch_api_key_by_hash(key_hash: str) -> Optional[dict]:
    """Return the row whose ``key_hash`` matches, or ``None`` if unknown.

    The returned dict is a copy of the stored row so callers cannot
    accidentally mutate the in-memory store. The ``revoked_at`` field is
    passed through verbatim so the auth dependency can decide whether
    the key is currently usable (SPEC.md line 106).

    Citations: SPEC.md line 104 — lookup by hash; SPEC.md line 106 —
    revoke check uses the same row; TEST_SPEC.md §1 FR-03 row 5 —
    revoked row exists in the table but is rejected.
    """  # NFR-02 NFR-10
    if not isinstance(key_hash, str):
        raise TypeError("key_hash must be a str")
    for row in _store.values():
        if row["key_hash"] == key_hash:
            # Return a shallow copy so callers cannot mutate the store.
            return dict(row)
    return None


def revoke_api_key(key_id: str, *, revoked_at: str) -> bool:
    """Stamp ``revoked_at`` on the row identified by ``key_id``.

    Returns ``True`` if a row was updated, ``False`` if the id is
    unknown. After this call, ``require_api_key`` will reject requests
    presenting the matching plaintext with 401 (SPEC.md line 106).

    Citations: SPEC.md line 106 — revoked_at 非空一律視為無效;
    TEST_SPEC.md §1 FR-03 row 5 — same plaintext, post-revocation → 401.
    """  # NFR-09
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("key_id must be a non-empty str")
    if not isinstance(revoked_at, str) or not revoked_at:
        raise ValueError("revoked_at must be a non-empty str")
    with _lock:
        row = _store.get(key_id)
        if row is None:
            return False
        row["revoked_at"] = revoked_at
        return True
