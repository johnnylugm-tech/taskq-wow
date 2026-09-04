"""[FR-01] Test configuration — reset the in-memory store per test.

The repository layer (``taskq_api.repository.tasks``) keeps a
process-local dict as its Phase-3 GREEN backing store. The RED tests
under ``test_fr01.py`` expect each test to start from a deterministic
fixture (one pre-seeded ``task-uuid-001`` row), so this conftest resets
the store at the start of each FR-01 test.

Citations:
- TEST_SPEC.md §1 FR-01 — test fixture expectations.
- SAD.md §2.7 — repository is the persistence seam.
"""  # NFR-10
from __future__ import annotations

import pytest

import taskq_api.repository.tasks as _repo


@pytest.fixture(autouse=True)
def _reset_in_memory_store(request):
    """[FR-01] Reset the in-memory store before each FR-01 test.

    Citations: TEST_SPEC.md §1 FR-01 — each test starts from a known
    fixture row ``task-uuid-001``.
    """  # NFR-10
    if "test_fr01" in request.node.nodeid and hasattr(_repo, "_reset_state"):
        _repo._reset_state()
    yield