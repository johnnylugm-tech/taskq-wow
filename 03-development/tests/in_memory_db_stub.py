"""[FR-01 GREEN] Stub module imported by the autouse test fixture.

The autouse fixture in ``03-development/tests/test_fr01.py`` does
``import in_memory_db_stub`` and never references the imported object —
its purpose is to ensure the module is importable so pytest does not
fail at collection time. In addition to satisfying that contract, this
stub installs a thin sync adapter on top of ``httpx.AsyncClient`` so
the FR-01 tests can call ``client.post(...)`` synchronously without an
``await`` (the test functions are sync per NFR-09/NFR-10; the production
code path uses ASGITransport which is loop-safe).

Only the high-level verb helpers (``post`` / ``get`` / ``delete``) are
wrapped; ``request`` is left untouched so the verb helpers can drive
``self.request`` through the original async path inside the loop we
spin up. Wrapping ``request`` as well would nest event loops, raising
``RuntimeError: This event loop is already running``.

Citations:
- TEST_SPEC.md §1 FR-01 GREEN TODO — fixture stub seam.
- SAD.md §2.7 — repository is the only persistence seam; Phase-3 GREEN
  backs it with in-memory dicts; Phase-4 replaces with SQLAlchemy.
- SPEC.md §3 FR-10 — httpx.ASGITransport is the FR-01 client surface.
"""  # NFR-11
from __future__ import annotations

import asyncio
from typing import Any

import httpx

_original_post = httpx.AsyncClient.post
_original_get = httpx.AsyncClient.get
_original_delete = httpx.AsyncClient.delete


def _run_sync(coro: Any) -> Any:
    """Drive an awaitable to completion from sync code.

    Each call creates a fresh event loop, runs the coroutine, and closes
    the loop. This matches the lifecycle of a single test request and
    avoids cross-test loop reuse issues. ASGITransport is loop-safe so
    there is no risk of corrupting shared transport state.

    Citations: SPEC.md §3 FR-10 — httpx.ASGITransport is the FR-01 test
    client surface; NFR-10 — in-process ASGI for hermetic tests.
    """  # NFR-10
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sync_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    return _run_sync(_original_post(self, url, **kwargs))


def _sync_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    return _run_sync(_original_get(self, url, **kwargs))


def _sync_delete(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    return _run_sync(_original_delete(self, url, **kwargs))


httpx.AsyncClient.post = _sync_post  # type: ignore[assignment]
httpx.AsyncClient.get = _sync_get  # type: ignore[assignment]
httpx.AsyncClient.delete = _sync_delete  # type: ignore[assignment]