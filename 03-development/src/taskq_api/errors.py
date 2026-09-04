"""[FR-01] taskq_api.errors — domain-level exception types.

Independence-layer module per `.methodology/SAB.json` (`independence`
layer, no inbound dependencies on api/service/repository/models). Holds
``DuplicateNameError`` and ``TaskNotFoundError`` so service/api layers
can catch them by name without importing the service module (the
service stays small per NFR-11; routers import from
``taskq_api.service.tasks`` to keep the current shape).

Citations:
- SPEC.md §3 FR-01 — unique name (NP-05); unknown id (404).
- SAD.md §2.7 — errors is an independence module; it has zero imports
  from api/service/repository/models.
- NFR-09 — public exception types carry docstrings.
"""  # NFR-09
from __future__ import annotations


class DuplicateNameError(Exception):
    """[FR-01] Raised when a task name violates the unique-name rule (NP-05).

    Citations: SPEC.md §3 FR-01 — unique name; SPEC.md §3 FR-01 — 409
    on duplicate; TEST_SPEC.md §1 FR-01 row 5.
    """  # NFR-09


class TaskNotFoundError(Exception):
    """[FR-01] Raised when a task id is unknown to the repository.

    Citations: SPEC.md §3 FR-01 — unknown id → 404; TEST_SPEC.md §1
    FR-01 row 4.
    """  # NFR-09