# Software Architecture Document (SAD) — taskq-api

> Software Architecture Document for the `taskq-api` harness-methodology
> Round 2 testbed (Python 3.11, FastAPI + SQLAlchemy + Alembic + asyncio).
> Source of truth: `../SPEC.md`. Citations use the form `SPEC §N` / `FR-XX` / `NFR-XX`.

---

## 1. Architecture Overview

`taskq-api` is an ASGI HTTP service that exposes a JSON task-queue API on top
of a relational database. It is the Round 2 testbed of the harness methodology,
and its architectural goal is to add four dimensions absent from the Round 1
single-process CLI: a real HTTP layer, a real database with explicit
transaction boundaries, real schema migrations with data movement, and
production-shaped async execution.

The system has three runtime surfaces:

1. **API server** — `uvicorn taskq_api.app:app` (FR-01 … FR-09). Accepts
   authenticated requests, validates payloads, orchestrates execution, and
   persists state.
2. **Admin CLI** — `python -m taskq_api` (key generation, migration, seed,
   healthcheck), implemented by `taskq_api/__main__.py` (FR-03 key create).
   It is an entry point, not a layer member: it may call `service` and
   `repository` but nothing may import it.
3. **Migration runner** — `alembic upgrade head` / `alembic downgrade -1`
   (FR-07). Three revisions, each reversible.

Architectural shape:

- Four-tier layer contract `api > service > repository > models` (NFR-06).
  `config` and `errors` are *independence* modules — they have no layer and
  may be imported by any layer (they cannot import back into any layer).
- Authentication (`X-API-Key`) and scope authorization (`read`/`write`/
  `admin`) are centralised in a single FastAPI dependency (FR-03, FR-04).
  No handler enforces auth locally.
- All non-2xx responses use RFC 7807 `application/problem+json` (FR-10).
  `detail` is a fixed allow-list; stack traces, SQL, file paths, and
  connection strings are scrubbed before serialisation (NFR-02, NFR-04).
- Persistence is ORM-only. `repository` is the only layer that imports
  `sqlalchemy` (NFR-06 forbidden contract). N+1 protection is enforced by an
  SQLAlchemy event-listener assertion (NFR-01).
- Async background execution uses `asyncio.TaskGroup` with a hard
  concurrency cap (`TASKQ_MAX_CONCURRENT`) and a graceful drain on shutdown
  (FR-08, NFR-03).

### 1.1 System Verification Target

> **Every exit gate (2, 3 and 4)**: the harness executes `make verify-system`. A
> non-zero exit fails the gate. The target name is fixed — the harness always calls
> `make verify-system`.
>
> This is the only check in the whole framework that runs the delivered system.
> Everything else reads your source text or runs your test suite, both of which
> your test doubles configure. Two rules follow, and the gate enforces both:
>
> 1. **At least one step must invoke the delivered entry point** — the program a
>    user would run (`python -m <your_package> …`, your console script, your
>    service). A target that chains `test lint coverage` re-runs dimensions the
>    gate has already scored and verifies nothing further.
> 2. **The step that does so must be able to fail.** `|| true`, a leading `-`,
>    and tool flags like `ruff --exit-zero` all keep a failure out of make's exit
>    code, which is the only thing the gate reads.
>
> Aim for a step that exercises a real acceptance criterion against real
> dependencies — a temporary database, a real file, the actual process — because
> the gate also measures which of your high-risk modules this target executed.
> Any module your test suite replaces with an `autouse` stand-in has to run for
> real here.
**Makefile target**: `verify-system`
**Exercises**: `alembic upgrade head` → full test suite → `uvicorn
taskq_api.app:app` with `/healthz` and `/readyz` smoke checks → `alembic
downgrade base` → `alembic upgrade head` (round-trip migration proof, FR-07
NFR-12). High-risk modules exercised: `taskq_api.service.runner`,
`taskq_api.service.auth`, `taskq_api.repository.session`,
`migrations/versions/v3_split_results.py`.

---

## 2. Module Design

### 2.1 Directory Structure

> **CRG Architecture Scoring**: Phase 3+ judges your code's community cohesion via
> the Code Review Graph (CRG).  CRG groups files by **directory** — each directory
> is one community.  The architecture score is the fraction of communities that are
> "healthy" (internal edge density ≥ 0.3 AND size ≤ 50 nodes).
>
> **CRG scoring formula**: Each community's cohesion = internal_edges / (internal_edges + external_edges).
> External edges = calls to libraries (stdlib, frameworks) + calls to other communities.
> Internal edge dilution is the primary risk — entry points (CLI, main.py) import many libraries,
> producing external edges with no offsetting internal edges unless they also call sibling modules.
> The fix is **not** to reduce library imports — it is to ensure every function body also calls at least one
> sibling within the same directory.
>
> **Required edge budget**: To reach cohesion ≥ 0.3 with E external edges, you need
> I ≥ ceil(0.4286 × E) internal edges. Each function-body call to a hub function = 1 internal edge.
> Module-level calls create 1 edge per file, but per-function-body calls multiply the count.
> Example: 48 external edges → need ≥21 internal edges. With 5 sibling files each having
> 4 function bodies calling 2 hub functions → 40 internal edges — safely above threshold.

**Design for high cohesion from the start — 6 Universal CRG Design Principles:**

**Principle 1 — Use subdirectories to control CRG community boundaries.** CRG assigns one community per directory. If you dump 10+ files into a flat `src/`, CRG's Leiden algorithm freely splits them into unpredictable communities — some will likely fall below the 0.3 cohesion threshold. Explicit subdirectories (`src/api/`, `src/core/`, `src/infrastructure/`) each become one predictable community. Aim for 3-6 source directories total (excluding tests). Fewer than 3 → oversized single community; more than 6 → too many communities to keep all above 0.3.

**Principle 2 — Every directory needs a hub module (≥2 functions for 4+ siblings).** Each directory with ≥2 files must have a shared module (`utils.py`, `common.py`, `helpers.py`) that ≥70% of sibling files import and call via standalone function calls: `result = hub.fn(...)`. This creates cross-file internal edges. Pure library-utility files that no sibling calls produce zero internal edges — they only dilute the community.

For directories with ≥4 sibling files, **one hub function is rarely enough** — a single function called from 5 files produces ~5 edges, which may not offset ~40+ external edges. Use **≥2 hub functions** so each sibling can call both from multiple function bodies, multiplying internal edge count. The tts-new infrastructure directory (5 siblings, 48 external edges) required 2 hub functions (`validate_config` + `get_config_snapshot`) called from every function body to reach ~32 internal edges and pass 0.3.

Exception: directories that form a linear processing pipeline (A→B→C) where each file calls the next in chain.

**Principle 3 — Entry points must live inside a hub directory.** Entry-point modules (CLI, `main.py`, `app.py`, daemon) unavoidably import many external libraries — httpx, FastAPI, argparse, asyncio, etc. Each external import adds an external edge. If the entry point sits alone at the project root (e.g. `src/cli.py`), those external edges dominate and cohesion drops below 0.3. Place entry points inside a directory that also contains a hub module — the entry point calls the hub (internal edges) to compensate for its external edges.

**Principle 4 — Every function body must call a hub function (not just module-level).** A file that is never imported or called by any other file in its directory contributes only external edges (its own imports) and zero internal edges — pure dilution. For each file in your design, verify it is either: (a) the hub module itself, (b) called by the hub, or (c) calls the hub. Files that fail this check should be merged into another file or directory.

Critically, **module-level calls alone are insufficient**. A module-level `_ = validate_config()` creates 1 internal edge per file regardless of how many functions it has. CRG counts edges per (caller_node, callee_node) pair — each function body that calls the hub creates a separate edge. To accumulate enough internal edges (see edge budget above), the hub function must be called **from every accessible function body** in each sibling file, not just at module level. Example: a 5-sibling directory needs ~21 internal edges; 5 module-level calls + 5×4 function-body calls = 25 edges.

**Principle 5 — Respect CRG edge-detection limits.** CRG uses Tree-sitter AST parsing and detects cross-file function calls resolved through imports. These limitations are cross-language:
- Calls between functions in the **same** file — NOT detected (zero cohesion contribution)
- `self.method()` calls inside a class — DETECTED (class hierarchy contributes edges)
- `import sibling` → `sibling.fn()` — DETECTED (cross-file import resolved)
- `result = hub.fn(...)` then `log.info(..., extra=result)` — DETECTED (standalone assignment)
- `log.info(..., extra=hub.fn(...))` — INCONSISTENTLY detected (nested arg position)
- Calls through imports at runtime (lazy imports in `__getattr__`, `__init__.py` re-exports) — may be missed if not statically resolvable

**Principle 6 — Size cap: communities stay under 50 nodes.** CRG marks any community with >50 nodes as unhealthy regardless of cohesion. A node ≈ one function or class in a file. If your directory design would produce >50 nodes (roughly 4-6 modules with 8-12 functions each), split into subdirectories. Unlike Principles 1-5, this can be relaxed slightly — the cap is 50, not 30 — so this is rarely the binding constraint unless you have large god-modules.

| Quick reference | check |
|----------------|-------|
| Source directories count? | 3-6 |
| Each dir has a hub file? | Yes |
| Hub has ≥2 functions if ≥4 sibling files? | Yes |
| Entry points inside a hub dir? | Yes |
| Each function body calls a hub function? | Yes (not just module-level) |
| Cross-file calls use standalone assignment? | Yes |
| Community size ≤ 50 nodes? | Yes |
| Edge budget: I ≥ 0.4286 × E? | Yes |

**Anti-patterns that produce low scores:**

```
❌ src/__init__.py, src/main.py, src/models.py, src/cli.py, src/audio.py
   → 5 isolated files in flat src/, zero cross-imports → cohesion=0.0

❌ src/cli.py  (imports httpx, argparse, asyncio — all external, no internal sibling calls)
   → pure external edges, no compensation → cohesion near 0

❌ tests/test_fr01.py, tests/test_fr02.py, ... tests/test_fr08.py
   → 80 nodes in one dir, no internal edges → oversized + zero cohesion

✅ src/api/{cli,main,speech,utils}.py with utils imported by all siblings → hub-and-spoke
✅ src/engines/{synthesis,splitter,parser}.py with synthesis calling both → pipeline chain
✅ src/infrastructure/{circuit,health,config,models}.py → shared domain layer
```

#### Applied directory layout for taskq-api

Source tree under `03-development/src/taskq_api/`:

```
taskq_api/
├── __init__.py
├── __main__.py                # Admin CLI entry — `python -m taskq_api key create` (FR-03)
├── config.py                  # independence module — env loader (NFR-04 scrub)
├── errors.py                  # independence module — RFC 7807 builders (FR-10)
├── api/
│   ├── __init__.py
│   ├── app.py                 # FastAPI app factory + router registration (entry + hub)
│   ├── dependencies.py        # hub: api_key_auth, require_scope, rate_limit_dep
│   ├── tasks.py               # FR-01 handlers (POST/GET/DELETE /v1/tasks)
│   ├── runs.py                # FR-02 handlers (POST /v1/tasks/{id}/run, GET runs)
│   ├── health.py              # FR-09 (/healthz, /readyz)
│   └── metrics.py             # FR-09 (/v1/metrics)
├── service/
│   ├── __init__.py
│   ├── tasks.py               # business logic for FR-01 + FR-04 (authz check ordering)
│   ├── auth.py                # FR-03 (key hashing, compare_digest) + FR-04 (scope check) [HIGH-RISK]
│   ├── rate_limit.py          # FR-05 (token bucket with row-level lock)
│   └── runner.py              # FR-02 + FR-08 (asyncio.TaskGroup + subprocess exec) [HIGH-RISK]
├── repository/
│   ├── __init__.py
│   ├── session.py             # FR-06 context-managed Session, N+1 listener [HIGH-RISK]
│   ├── tasks.py
│   ├── api_keys.py
│   ├── rate_buckets.py
│   ├── results.py
│   └── tags.py
└── models/
    ├── __init__.py
    ├── task.py
    ├── api_key.py
    ├── rate_bucket.py
    ├── result.py
    └── tag.py
```

Layer contract (`api > service > repository > models`) and independence
modules (`config`, `errors`) are enforced by `.importlinter` at the repo
root (NFR-06).

Per CRG Principle 1, four source directories (`api`, `service`,
`repository`, `models`) plus two independence modules (`config.py`,
`errors.py`) sit at the package root — within the 3–6 healthy range.
Per Principle 3 the entry point `api/app.py` lives inside `api/` alongside
its hub `api/dependencies.py`. Per Principle 2 every directory has a hub
(`api/dependencies.py`, `repository/session.py`, `service/tasks.py` for
business reuse) and ≥2 hub functions called from every sibling function
body. Per Principle 6 the per-directory node budget (≤ ~12 functions per
file × ≤ 6 files) keeps each community under 50 nodes.

#### Recorded deviations from the SRS FR Block `implementation_functions`

The SRS FR Block records illustrative dotted paths written before the
module tree existed. Where this SAD differs, the SAD tree and the §5 SAB
block are binding, and the deviation is deliberate:

| SRS FR Block path | This SAD | Reason |
|---|---|---|
| `taskq_api.api.v1.tasks.*` (FR-01, FR-02) | `taskq_api.api.tasks`, `taskq_api.api.runs` | `/v1` is a **URL** prefix, not a package level. An `api/v1/` subdirectory would create a fifth CRG community that needs its own hub module (Principle 2) while `api/` kept only `app.py` — splitting the hub from its callers for no routing benefit. Version prefix is applied once at router mount in `app.py`. |
| `taskq_api.api.v1.metrics` (FR-09) | `taskq_api.api.metrics` | Same reason. |
| `taskq_api.cli.key_create` (FR-03) | `taskq_api.__main__` | `python -m taskq_api` (SRS AC-3.4) requires `__main__.py` at the package root — a `cli/` package cannot satisfy that invocation without a root shim, so the shim *is* the module. |
| `taskq_api.dependencies.scope_guard` (FR-04) | `taskq_api.api.dependencies` | The scope guard is a FastAPI `Depends` callable and imports Starlette request types; at the package root it would force `fastapi` into an independence module and break the NFR-06 layering contract. |
| `taskq_api.repository.task_results` (FR-02) | `taskq_api.repository.results` | Module named for the entity, table still `task_results`; avoids a `task_*` prefix on every repository file. |
| `taskq_api.repository.base.BaseRepository` (FR-06) | `taskq_api.repository.session` | Session lifecycle is the shared hub (Principle 2); a `BaseRepository` class adds an inheritance layer that CRG scores as one node with no extra internal edges. |
| `taskq_api.middleware.correlation_id` (FR-10) | `taskq_api.api.app` + `taskq_api.errors` | Correlation id is assigned by ASGI middleware registered in the app factory and rendered by `errors.problem`; a standalone root `middleware` module would be a single-function file with zero sibling calls (Principle 4 dilution). |
| `migrations.versions.v2_tags` (FR-07) | `migrations.versions.v2_add_tags` | Filename only; revision content is identical. |

Migrations live in their own tree (one file per revision), exercised by
the `verify-system` target via `alembic` CLI:

```
migrations/
├── env.py
├── script.py.mako
└── versions/
    ├── v1_initial.py                  # tasks + api_keys
    ├── v2_add_tags.py                 # tags + task_tags + unique index
    └── v3_split_results.py            # [HIGH-RISK] result_json → task_results (data move)
```

#### FR ↔ Module traceability (every FR mapped to ≥1 module)

| FR | Description | Primary modules |
|----|-------------|-----------------|
| FR-01 | Task CRUD API | `taskq_api.api.tasks`, `taskq_api.service.tasks`, `taskq_api.repository.tasks` |
| FR-02 | Task run endpoint | `taskq_api.api.runs`, `taskq_api.service.runner`, `taskq_api.repository.results` |
| FR-03 | API key auth | `taskq_api.api.dependencies`, `taskq_api.service.auth`, `taskq_api.repository.api_keys`, `taskq_api.__main__` (key create) |
| FR-04 | Scope authz | `taskq_api.service.auth`, `taskq_api.api.dependencies` |
| FR-05 | Rate limit | `taskq_api.api.dependencies`, `taskq_api.service.rate_limit`, `taskq_api.repository.rate_buckets` |
| FR-06 | Persistence / txn | `taskq_api.repository.session`, `taskq_api.repository.tasks` |
| FR-07 | Alembic migration | `migrations/versions/v1_initial.py`, `v2_add_tags.py`, `v3_split_results.py` |
| FR-08 | Async executor | `taskq_api.service.runner` (uses `asyncio.TaskGroup`) |
| FR-09 | Health / metrics | `taskq_api.api.health`, `taskq_api.api.metrics`, `taskq_api.repository.session` |
| FR-10 | RFC 7807 errors | `taskq_api.errors` (allow-list `detail`), every handler |

#### NFR ↔ Module / mechanism traceability

| NFR | dimension | Mechanism / owner module |
|-----|-----------|--------------------------|
| NFR-01 | performance | `taskq_api.repository.tasks` (`selectinload`/`joinedload`), `repository.session` SQL-count listener |
| NFR-02 | security | grep gates + `taskq_api.repository.session` (no string SQL), `taskq_api.service.auth` (`compare_digest`), `taskq_api.errors` (scrubbed `detail`) |
| NFR-03 | error_handling | `taskq_api.repository.session` (context manager), `taskq_api.service.runner` (no swallowed `CancelledError`), `taskq_api.api.health` |
| NFR-04 | security | `taskq_api.config` (env scrubber), `taskq_api.errors` (log + body scrubber), `taskq_api.service.auth` (plaintext once) |
| NFR-05 | documentation | every module — docstring with `[FR-XX]` / `[NFR-XX]` tag, FastAPI `summary`/`description` |
| NFR-06 | architecture_constraints | `.importlinter` enforcing `api > service > repository > models` + forbidden `sqlalchemy` outside `repository/` |
| NFR-07 | license_compliance | `requirements.txt` + `requirements.lock`, `pip-licenses`, `08-config/SBOM.json` |
| NFR-08 | mutation_testing | `.methodology/harness_config.json` (`features.mutation_testing: true`); scope: `service/` + `repository/` |
| NFR-09 | test_assertion_quality | all tests under `03-development/tests/`; `pytest -q` skip count assertion |
| NFR-10 | integration_coverage | `03-development/tests/integration/` driven by `httpx.AsyncClient(ASGITransport(app))` |
| NFR-11 | readability | MI ≥ 80, CC ≤ 10, ≤ 400 lines/file, ≤ 15 files/dir, handler ≤ 40 lines |
| NFR-12 | execute_verification_target | `Makefile verify-system` (migration round-trip + smoke) |

### 2.2 `taskq_api.config`

| Attribute | Value |
|-----------|-------|
| Responsibility | Load `TASKQ_*` env vars, scrub connection strings, expose typed `Settings` (SPEC §5.1). |
| External Interface | `get_settings()` (cached), `scrub_db_url(url)` (strips password before log/serialisation, NFR-04). |
| Dependencies | stdlib `os`, `pydantic.BaseSettings` only — **does not import** `service`/`repository`/`api`. |
| Independence module | May be imported by any layer; imports nothing from this package except stdlib + pydantic. |

#### Logical Constraints
- All 12 `TASKQ_*` env vars declared in `pydantic` model with defaults (SPEC §5.1, NFR-09 test: `grep -c "^TASKQ_" .env.example` returns 12).
- `scrub_db_url` is called by `taskq_api.errors`, `taskq_api.api.metrics`, and the logging filter — no raw URL ever reaches stdout, log, or `/v1/metrics`.
- No `service` / `repository` / `api` import.

### 2.3 `taskq_api.errors`

| Attribute | Value |
|-----------|-------|
| Responsibility | Build RFC 7807 `application/problem+json` envelopes; provide an allow-list `detail` scrubber (FR-10, NFR-02, NFR-04). |
| External Interface | `problem(status, type_uri, title, detail, correlation_id)`, `HTTPException` subclasses (`Unauthenticated`, `Forbidden`, `NotFound`, `Conflict`, `RateLimited`, `Validation`, `NotReady`, `Internal`). |
| Dependencies | stdlib + `taskq_api.config` only (for correlation-id formatting); no `fastapi`/`sqlalchemy`. |
| Independence module | May be imported by any layer; imports `config` and stdlib only. |

#### Logical Constraints
- `detail` scrubber rejects substrings matching `sql|stack|trace|/tmp/|postgres(ql)?://|password=` etc.
- Every non-2xx response passes through this builder — handlers raise subclasses, never raw `HTTPException`.
- `correlation_id` propagated to response header `X-Correlation-Id` and server log.

### 2.4 `taskq_api.api` — HTTP entry layer

| Attribute | Value |
|-----------|-------|
| Responsibility | Wire FastAPI routes, validate bodies with `pydantic`, call into `service`, convert domain errors to RFC 7807 envelopes. |
| External Interface | `app = create_app()`; routers mounted under `/v1`. |
| Dependencies | FastAPI, httpx (test-only via tests), `taskq_api.service`, `taskq_api.errors`. **No `sqlalchemy` import.** |
| Hub | `taskq_api.api.dependencies` — `api_key_auth`, `require_scope(scope)`, `rate_limit_dep` are imported and called by every router (CRG Principle 2). |

#### Sub-modules
- `app.py` — factory; registers routers, exception handlers, CORS (default deny).
- `dependencies.py` — auth + scope + rate-limit dependencies (FR-03/04/05).
- `tasks.py` — FR-01 handlers (≤ 40 lines each per NFR-11).
- `runs.py` — FR-02 handlers.
- `health.py` — FR-09 `/healthz`, `/readyz`.
- `metrics.py` — FR-09 `/v1/metrics` (admin scope).

#### Logical Constraints
- Handlers do not hold a `Session` — they call `service.*` and pass DTOs.
- Auth scope check happens **before** resource lookup (FR-04 → 403 must not leak resource existence).
- Each router imports `dependencies` and calls at least one hub function per route handler body (CRG Principle 4).

### 2.5 `taskq_api.service` — Business logic layer

| Attribute | Value |
|-----------|-------|
| Responsibility | Orchestrate persistence + execution; enforce business invariants; isolate async subprocess management. |
| External Interface | Pure-Python functions and asyncio entry points; **no FastAPI / Starlette types**. |
| Dependencies | `taskq_api.repository`, `taskq_api.errors`, `taskq_api.config`. **No `fastapi`, no `sqlalchemy` direct imports.** |
| Hub | `service.tasks` — pure helpers (`validate_name`, `ensure_unique_name`) imported by all sibling modules. |

#### Sub-modules
- `tasks.py` — business logic for FR-01 (validation + uniqueness) and FR-04 (authz-before-lookup).
- `auth.py` — `[HIGH-RISK]` API key hashing (`hashlib.sha256`), constant-time compare, scope hierarchy (FR-03, FR-04).
- `rate_limit.py` — `[HIGH-RISK core]` token-bucket math; delegates persistence to `repository.rate_buckets` under a row-level lock (FR-05).
- `runner.py` — `[HIGH-RISK]` `asyncio.TaskGroup` driver; `asyncio.create_subprocess_exec` (no `shell=True`); `process.kill()` + `await process.wait()` on timeout (FR-02, FR-08, NFR-03).

#### Logical Constraints
- No `Session` objects passed out of `service` — return DTOs only.
- `runner.py` re-raises `asyncio.CancelledError`; never `except Exception` swallow.
- All shell-outs use `shlex.split(command)` with `*args` to `create_subprocess_exec` — `shell=True` forbidden (NFR-02 grep gate).

### 2.6 `taskq_api.repository` — Persistence layer

| Attribute | Value |
|-----------|-------|
| Responsibility | Own the only `sqlalchemy` imports in the package (NFR-06 forbidden contract); manage transaction boundaries; surface N+1 protection. |
| External Interface | `session_scope()` context manager; per-table CRUD helpers; `event.listens_for(Engine, "before_cursor_execute")` SQL counter. |
| Dependencies | SQLAlchemy 2.x, Alembic. **No `fastapi`, no `service`, no `api`.** |
| Hub | `repository.session` — `session_scope()`, `get_engine()`, `attach_n_plus_one_listener()` imported and called by every sibling CRUD module (CRG Principle 2). |

#### Sub-modules
- `session.py` — `[HIGH-RISK]` engine, `pool_pre_ping=True`, `session_scope()` commits on success / rolls back on exception.
- `tasks.py` — task CRUD with `selectinload`/`joinedload` (NFR-01 N+1 guard).
- `api_keys.py` — lookup by hashed key; `revoked_at IS NULL` filter.
- `rate_buckets.py` — atomic token bucket update (`SELECT ... FOR UPDATE` on PostgreSQL; explicit transaction on SQLite).
- `results.py` — FR-07 v3 `task_results` reads.
- `tags.py` — v2 `tags` + `task_tags`.

#### Logical Constraints
- All access is ORM or `text(...)` with bound parameters — no f-string / `%` / `+` SQL (NFR-02 grep gate).
- `session_scope()` is the only way to obtain a `Session`; handlers/services never call `create_engine` directly.
- List endpoints must emit a constant number of SQL statements — `attach_n_plus_one_listener` raises in test mode.

### 2.7 `taskq_api.models` — ORM models layer

| Attribute | Value |
|-----------|-------|
| Responsibility | Declarative ORM classes mirroring the post-v3 schema (SPEC §5.2). |
| External Interface | `taskq_api.models.{task,api_key,rate_bucket,result,tag}`. |
| Dependencies | SQLAlchemy declarative base only. **No `service`, no `api`, no `repository` runtime imports.** |
| Hub | `models/__init__.py` re-exports `Base` and each model; `repository.session` imports `Base` for `metadata.create_all` and Alembic autogenerate base. |

#### Logical Constraints
- Pure data; no business methods, no I/O.
- File count ≤ 5 (Task, ApiKey, RateBucket, Result, Tag) — well under Principle 6's 50-node ceiling.

### 2.8 `migrations/versions/` — Schema evolution

| Attribute | Value |
|-----------|-------|
| Responsibility | Three reversible Alembic revisions implementing FR-07. |
| External Interface | `alembic upgrade head` / `downgrade base`. |
| Dependencies | Alembic, SQLAlchemy metadata. |
| Hub | `migrations/env.py` — common target metadata import. |

#### Files
- `v1_initial.py` — `tasks`, `api_keys`, `rate_buckets`.
- `v2_add_tags.py` — `tags`, `task_tags`, `tasks.name` unique index.
- `v3_split_results.py` — `[HIGH-RISK]` data migration: split `tasks.result_json` into `task_results`; reverse is a `INSERT ... SELECT` back into `result_json` then drop the table (data integrity validated by §8 #12 test).

#### Logical Constraints
- Every `upgrade` has a working `downgrade`; `op.execute("DROP TABLE ...")` is forbidden as a downgrade shortcut.
- Round-trip data fidelity test (SPEC §8 #12) runs against a real SQLite file (not in-memory).

### 2.9 `taskq_api.__main__` — Admin CLI entry point

| Attribute | Value |
|-----------|-------|
| Responsibility | `python -m taskq_api key create --scope <scope>` (FR-03 / AC-3.4): generate a key, print the plaintext exactly once, persist only the SHA-256 hash. |
| External Interface | `main(argv)` — argparse dispatch; exit code 0 on success, non-zero on failure. |
| Dependencies | `taskq_api.service.auth` (hashing — single source of truth, never re-implemented here), `taskq_api.repository.session` + `repository.api_keys`, `config`, `errors`. |
| Entry point | Nothing in the package may import `__main__`; it is a leaf caller, so it cannot participate in a cycle. |

#### Logical Constraints
- No hashing, scope, or SQL logic of its own — every operation delegates
  (a second hash implementation would silently diverge from FR-03).
- Plaintext key is written to stdout once and never logged or persisted
  (NFR-04); `config.scrub_db_url` applies to any DSN it echoes.
- ≤ 40 lines per command handler (NFR-11).

### 2.10 Inter-module dependency rules

Allowed edges (per `.importlinter` contract, NFR-06):

```
api       -> service, repository, models, errors, config
service   -> repository, models, errors, config
repository-> models, errors, config
models    -> (no intra-package imports)
config    -> (no intra-package imports)
errors    -> config  (and stdlib only)
__main__  -> service, repository, models, errors, config  (entry point;
             nothing may import it)
```

Forbidden edges: any layer importing a *higher* layer; any layer other than
`repository` importing `sqlalchemy`; `models` importing any sibling module.

---

## 3. Interfaces & Data Flows

### 3.1 Request lifecycle (read path — FR-01 GET /v1/tasks/{id})

```
caller          api/tasks.py            service/tasks.py      repository/tasks.py     DB
  │  GET /v1/tasks/{id}    │                       │                     │                 │
  │ ─────────────────────► │ api_key_auth          │                     │                 │
  │                        │ ──────────────────►  │ (service.auth)      │                 │
  │                        │ ◄── ApiKeyIdentity── │                     │                 │
  │                        │ require_scope('read') │                     │                 │
  │                        │ ───► 403 if missing   │                     │                 │
  │                        │ rate_limit_dep        │                     │                 │
  │                        │ ──► 429 if exhausted  │                     │                 │
  │                        │                       │ get_task(id)        │                 │
  │                        │                       │ ──────────────────► │ SELECT ...     │
  │                        │                       │                     │ ─────────────► │
  │                        │                       │ ◄──── TaskDTO ───── │                 │
  │  200 + JSON            │ ◄── TaskDTO ───────── │                     │                 │
  │ ◄──────────────────── │                       │                     │                 │
```

Notes:
- Auth and scope checks precede any repository call (R4 mitigation: 403
  must not leak resource existence — FR-04, NFR-02).
- `dependencies.py` (`api`) is the only module that calls `service.auth` —
  no handler does its own auth.

### 3.2 Request lifecycle (write path — FR-02 POST /v1/tasks/{id}/run)

```
caller        api/runs.py              service/runner.py              asyncio.TaskGroup     subprocess
  │  POST /v1/tasks/{id}/run         │                              │                        │
  │ ───────────────────────────────► │ auth+scope+rate-limit (deps) │                        │
  │                                  │ enqueue_run(task)            │                        │
  │                                  │ ───────────────────────────► │ spawn coroutine        │
  │  202 + {run_id}                  │ ◄── RunStarted ──────────────│                        │
  │ ◄────────────────────────────── │                              │ create_subprocess_exec │
  │                                  │                              │ ─────────────────────► │
  │                                  │                              │ ◄── stdout/stderr ──── │
  │                                  │ persist_result(exit_code)    │                        │
  │                                  │ ───────────────────────────► │ write task_results     │
```

Notes:
- `shell=False` enforced — `create_subprocess_exec(*shlex.split(command))`.
- On `asyncio.wait_for` timeout: `process.kill()` then `await process.wait()`
  before raising (NFR-03, R8 mitigation).
- `CancelledError` propagates through every `try` block; never caught as
  `Exception`.

### 3.3 Migration round-trip data flow (FR-07 v3)

```
              alembic upgrade head
                     │
                     ▼
   v1: tasks(id, command, name, status, result_json)
   v2: + tags, task_tags, tasks.name UNIQUE
   v3: tasks (no result_json)  +  task_results(id, task_id, exit_code, stdout_tail, stderr_tail, duration_ms, finished_at)
        ▲
        │  upgrade reads rows from tasks.result_json,
        │  inserts equivalent rows into task_results,
        │  then drops the column.
        │
   alembic downgrade -1
        │
        ▼  inverse INSERT ... SELECT into tasks.result_json
           then DROP TABLE task_results.
   Round-trip assertion (SPEC §8 #12):
       upgrade → write sample rows → downgrade -1 → upgrade head
       → sample values match column-for-column.
```

### 3.4 Data model relationships

```
   tasks 1───* task_results
   tasks *───* tags   (via task_tags)
   api_keys 1───1 rate_buckets
```

(All FKs cascade on delete from `tasks` per FR-01 cascade requirement.)

---

## 4. NFR Handling

Each NFR below states its `dimension`, the *enforcement mechanism* (the
code path that makes it verifiable), and the *gate* that proves it. All
twelve NFRs from SPEC §4 are enumerated.

### 4.1 NFR-01 — Performance & query efficiency
- **dimension**: `performance`
- **Mechanism**: `repository.session.attach_n_plus_one_listener` counts
  SQL statements per request; raises in test mode. List queries use
  `selectinload(Task.results).selectinload(Task.tags)`.
- **Gate**: pytest-benchmark — `GET /v1/tasks/{id}` p95 < 30 ms over
  10 k rows; `GET /v1/tasks?limit=50` p95 < 80 ms; SQL count assertion
  (constant w.r.t. result count).

### 4.2 NFR-02 — HTTP & data-layer security
- **dimension**: `security`
- **Mechanism**: `repository.session` forbids raw SQL; `service.auth`
  uses `hmac.compare_digest`; `errors.problem` scrubber rejects SQL /
  stack / path substrings; CORS default-deny from `TASKQ_CORS_ORIGINS`.
- **Gate**: grep gate (`shell=True|eval(|exec(` zero hits); bandit
  `-r 03-development/src/` 0 HIGH / 0 MEDIUM; integration tests for 403
  body shape and 500 body scrub.

### 4.3 NFR-03 — Error handling, txn, async correctness
- **dimension**: `error_handling`
- **Mechanism**: `repository.session.session_scope` commits/rolls back;
  `service.runner` re-raises `CancelledError`; `/readyz` returns 503 with
  explicit `detail` when DB unreachable or migration stale.
- **Gate**: AST scan (`ast-error-handling`) verifies no bare `except:` /
  `except Exception: pass`; integration tests for 503 on DB down.

### 4.4 NFR-04 — Sensitive data redaction
- **dimension**: `security`
- **Mechanism**: `config.scrub_db_url`, `errors.problem` scrubber, log
  filter; same regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)`
  applied to stdout_tail, stderr_tail, logs, error bodies, `/v1/metrics`.
- **Gate**: unit test asserting scrubber output on adversarial inputs;
  integration test triggering 500 and asserting body + log content.

### 4.5 NFR-05 — Documentation coverage
- **dimension**: `documentation`
- **Mechanism**: every public function/class carries docstring with
  `[FR-XX]` or `[NFR-XX]` tag; every FastAPI route carries
  `summary` + `description`.
- **Gate**: `ast-docstrings` scanner; coverage must be 100%.

### 4.6 NFR-06 — Layering contract
- **dimension**: `architecture_constraints`
- **Mechanism**: `.importlinter` at repo root — `api > service >
  repository > models` contract + `sqlalchemy` forbidden contract outside
  `repository/`.
- **Gate**: `lint-imports` exit 0; lowering contract or removing
  `.importlinter` is a SPEC violation (R-K class).

### 4.7 NFR-07 — Dependency & license compliance
- **dimension**: `license_compliance`
- **Mechanism**: `requirements.txt` pinned with `==`; `requirements.lock`
  freezes transitive versions; allow-list {MIT, BSD-2-Clause, BSD-3-Clause,
  Apache-2.0, PSF}; SBOM at `08-config/SBOM.json`.
- **Gate**: `pip-licenses --format=json --with-system` exits clean;
  SBOM regenerated each release.

### 4.8 NFR-08 — Mutation testing
- **dimension**: `mutation_testing`
- **Mechanism**: `.methodology/harness_config.json`
  `features.mutation_testing: true`; scope `service/` + `repository/`
  only (runtime budget).
- **Gate**: `mutmut results` score ≥ 70.

### 4.9 NFR-09 — Verification honesty (zero-skip iron rule)
- **dimension**: `test_assertion_quality`
- **Mechanism**: every test under `03-development/tests/` has at least one
  `assert`; FR-07 migration round-trip runs against real SQLite file
  (not in-memory mock); no `pytest.skip`/`skipif`/`xfail`/
  `collect_ignore`/`--ignore`/`-k` exclusions.
- **Gate**: `pytest -q` reports `0 skipped`; `ast-assertions` reports
  `zero_assert == 0`.

### 4.10 NFR-10 — Integration coverage
- **dimension**: `integration_coverage`
- **Mechanism**: `03-development/tests/integration/` driven exclusively
  by `httpx.AsyncClient(transport=ASGITransport(app))` — never direct
  handler calls.
- **Gate**: `pytest tests/integration --cov=...` line coverage ≥ 80%;
  every error code (401/403/404/409/422/429/503) has at least one
  integration case.

### 4.11 NFR-11 — Readability
- **dimension**: `readability`
- **Mechanism**: per-file ≤ 400 lines; per-directory ≤ 15 files; handler
  ≤ 40 lines (business logic lives in `service/`); CC ≤ 10; MI ≥ 80.
- **Gate**: `readability-v2` scanner.

### 4.12 NFR-12 — System verification target
- **dimension**: `execute_verification_target`
- **Mechanism**: `Makefile verify-system` runs
  `alembic upgrade head` → full tests → `uvicorn ...` + `/healthz`/`/readyz`
  smoke → `alembic downgrade base` → `alembic upgrade head`.
- **Gate**: `make verify-system` exits 0 and stdout contains
  `verify-system: PASS`.

---

## 5. SAB Block (machine-readable — BINDING CONTRACT)

> **CONTRACT**: Field names, types, `sab:` root key, and `phase` as int must
> match `core/quality_gate/sab_parser.py:render_canonical_sab_template()`.
> Do NOT hand-write the YAML — paste from the canonical template and replace
> EXAMPLE values with your project's real values.
> Validate before committing: `python3 scripts/generate_sab.py --validate --project .`

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "2026-09-05"
  phase: 2  # MUST be int, NOT a string — parser raises on 'phase: "2"'
  project: "taskq-api"

  layers:
    - name: api
      modules:
        - name: "taskq_api.api.app"
        - name: "taskq_api.api.dependencies"
        - name: "taskq_api.api.tasks"
        - name: "taskq_api.api.runs"
        - name: "taskq_api.api.health"
        - name: "taskq_api.api.metrics"
      allowed_dependencies: ["service", "repository", "models", "independence"]
    - name: service
      modules:
        - name: "taskq_api.service.tasks"
        - name: "taskq_api.service.auth"
        - name: "taskq_api.service.rate_limit"
        - name: "taskq_api.service.runner"
      allowed_dependencies: ["repository", "models", "independence"]
    - name: repository
      modules:
        - name: "taskq_api.repository.session"
        - name: "taskq_api.repository.tasks"
        - name: "taskq_api.repository.api_keys"
        - name: "taskq_api.repository.rate_buckets"
        - name: "taskq_api.repository.results"
        - name: "taskq_api.repository.tags"
      allowed_dependencies: ["models", "independence"]
    - name: models
      modules:
        - name: "taskq_api.models.task"
        - name: "taskq_api.models.api_key"
        - name: "taskq_api.models.rate_bucket"
        - name: "taskq_api.models.result"
        - name: "taskq_api.models.tag"
      allowed_dependencies: []
    - name: migrations
      modules:
        - name: "migrations.env"
        - name: "migrations.versions.v1_initial"
        - name: "migrations.versions.v2_add_tags"
        - name: "migrations.versions.v3_split_results"
      allowed_dependencies: ["models", "independence"]
    - name: cli
      modules:
        - name: "taskq_api.__main__"
      allowed_dependencies: ["service", "repository", "models", "independence"]
    - name: independence
      modules:
        - name: "taskq_api.config"
        - name: "taskq_api.errors"
      # errors -> config is an INTRA-layer edge (both modules live in this
      # layer), so it is not expressible as a cross-layer dependency.
      allowed_dependencies: []

  allowed_dependencies:
    - from: api
      to: service
    - from: api
      to: repository
    - from: api
      to: models
    - from: service
      to: repository
    - from: service
      to: models
    - from: repository
      to: models
    - from: api
      to: independence
    - from: service
      to: independence
    - from: repository
      to: independence
    - from: migrations
      to: models
    - from: migrations
      to: independence
    - from: cli
      to: service
    - from: cli
      to: repository
    - from: cli
      to: independence

  quality_targets:
    max_complexity: 10   # SPEC NFR-11: CC <= 10
    min_coverage: 100    # SPEC §11 / §8 #2: 100% line coverage
    max_coupling: 0.3

  nfr_dimension_mapping: {}  # OPTIONAL — auto-derived from nfr_traceability.type

  nfr_traceability:
    NFR-01:
      type: performance
      dimension: performance
      target: "p95<30ms_single;p95<80ms_list;constant_sql_count"
      module: taskq_api.repository.tasks
    NFR-02:
      type: security
      dimension: security
      target: "bandit=0;grep_sql_string_concat=0;grep_shell_eval_exec=0"
      module: taskq_api.repository.session
    NFR-03:
      type: reliability
      dimension: error_handling
      target: "no_bare_except;cancelled_error_propagates;503_on_db_down"
      module: taskq_api.service.runner
    NFR-04:
      type: security
      dimension: security
      target: "db_url_password_scrubbed;stdout_stderr_metrics_redacted"
      module: taskq_api.config
    NFR-05:
      type: documentation
      dimension: documentation
      target: "100pct_docstring_coverage_with_fr_or_nfr_tag"
      module: taskq_api.errors
    NFR-06:
      type: layering
      dimension: architecture_constraints
      target: "lint_imports_exit_0;sqlalchemy_forbidden_outside_repository"
      module: taskq_api.api.app
    NFR-07:
      type: licensing
      dimension: license_compliance
      target: "all_deps_in_allowlist;SBOM.json_present"
      module: taskq_api.config
    NFR-08:
      type: mutation
      dimension: mutation_testing
      target: "mutmut_score>=70"
      module: taskq_api.service.runner
      scope_layers: ["service", "repository"]
    NFR-09:
      type: testability
      dimension: test_assertion_quality
      target: "pytest_skipped==0;zero_assert==0"
      module: taskq_api.repository.session
    NFR-10:
      type: integration
      dimension: integration_coverage
      target: "integration_cov>=80pct;all_error_codes_covered"
      module: taskq_api.api.tasks
    NFR-11:
      type: maintainability
      dimension: readability
      target: "MI>=80;CC<=10;file<=400;dir<=15;handler<=40"
      module: taskq_api.service.tasks
    NFR-12:
      type: verifiability
      dimension: execute_verification_target
      target: "make_verify_system_exit_0_with_pass_token"
      module: taskq_api.api.app

  advisory_only: []  # AUTO-FILLED by parser — omit or leave []

  gate_score_overrides: {}  # AUTO-DERIVED by parser — omit or leave {}

  fr_module_traceability:
    FR-01:
      - "taskq_api.api.tasks"
      - "taskq_api.service.tasks"
      - "taskq_api.repository.tasks"
    FR-02:
      - "taskq_api.api.runs"
      - "taskq_api.service.runner"
      - "taskq_api.repository.results"
    FR-03:
      - "taskq_api.api.dependencies"
      - "taskq_api.service.auth"
      - "taskq_api.repository.api_keys"
      - "taskq_api.__main__"
    FR-04:
      - "taskq_api.service.auth"
      - "taskq_api.api.dependencies"
    FR-05:
      - "taskq_api.api.dependencies"
      - "taskq_api.service.rate_limit"
      - "taskq_api.repository.rate_buckets"
    FR-06:
      - "taskq_api.repository.session"
      - "taskq_api.repository.tasks"
    FR-07:
      - "migrations.versions.v1_initial"
      - "migrations.versions.v2_add_tags"
      - "migrations.versions.v3_split_results"
    FR-08:
      - "taskq_api.service.runner"
    FR-09:
      - "taskq_api.api.health"
      - "taskq_api.api.metrics"
    FR-10:
      - "taskq_api.errors"

  architecture_constraints:
    - "no_circular_dependencies"
    - "api>service>repository>models_layering"
    - "sqlalchemy_forbidden_outside_repository"
    - "errors_and_config_are_independence_modules"
    - "no_shell_true_no_eval_no_exec"
    - "no_fstring_or_percent_sql_concatenation"

  high_risk_modules:
    - "taskq_api.service.runner"
    - "taskq_api.service.auth"
    - "taskq_api.repository.session"
    - "migrations.versions.v3_split_results"

  required_artifacts:
    - ".env.example"
    - ".importlinter"
    - "requirements.txt"
    - "requirements.lock"
    - "requirements-dev.txt"
    - "alembic.ini"
    - "migrations/env.py"
    - "migrations/versions/v1_initial.py"
    - "migrations/versions/v2_add_tags.py"
    - "migrations/versions/v3_split_results.py"
    - "08-config/SBOM.json"
    - ".methodology/harness_config.json"
    - "Makefile"
```
<!-- SAB:END -->

Note: Fill in the YAML above — it is used for Drift Detection and gate scoring.
Generate: `python3 scripts/generate_sab.py --project . [--overwrite]`

---

## 6. Security Design (STRIDE-lite — machine-readable, BINDING CONTRACT)

> **CONTRACT**: Field names and the `security_design:` root key are parsed
> by `core/quality_gate/security_design.py:extract_security_block()`.
> Do NOT hand-write the YAML — paste from the canonical template and
> replace EXAMPLE values with your project's real values.
> Validate: `python3 harness_cli.py check-artifact-consistency --project .`
>
> `applicability: none` is a fully valid, honest declaration for a project
> with no real attack surface (e.g. a pure CLI formatting tool) — it
> requires a `justification` (>=20 chars) and skips the rest of this
> block. This is a decidable structural check, not a keyword scorer: an
> honest `none` always passes.

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full   # full | none — none REQUIRES justification and skips the rest
  justification: ""     # required (>=20 chars) when applicability: none
  trust_boundaries:
    - id: TB-01
      name: "external HTTP input"
      description: "requests crossing from unauthenticated network clients into the ASGI app"
    - id: TB-02
      name: "API key authentication path"
      description: "X-API-Key header crossing from caller into service.auth and repository.api_keys"
    - id: TB-03
      name: "subprocess execution boundary"
      description: "taskq_api.service.runner spawning asyncio subprocesses to execute user-provided commands"
    - id: TB-04
      name: "database persistence boundary"
      description: "repository layer writing to SQLAlchemy-managed sessions (SQLite / PostgreSQL)"
    - id: TB-05
      name: "error/log egress boundary"
      description: "errors.problem and structured logs emitting to HTTP responses, /v1/metrics, and stdout"
    - id: TB-06
      name: "schema-migration execution boundary"
      description: "alembic upgrade/downgrade modifying schema and migrating data with reversible revisions"
  threats:
    - id: T-01
      boundary: TB-01
      category: tampering
      description: "malformed POST /v1/tasks body mutates task state without validation"
      mitigation: "pydantic TaskCreate schema rejects unknown fields and injection blacklisted characters; 422 + problem+json"
      owner_module: "taskq_api.api.tasks"
      nfr: NFR-02
      verified_by: "test_sec_t01_malformed_payload_rejected"
    - id: T-02
      boundary: TB-01
      category: elevation_of_privilege
      description: "unauthenticated caller invokes privileged DELETE /v1/tasks/{id}"
      mitigation: "X-API-Key required on every /v1/* route; admin scope required for DELETE; central api_key_auth + require_scope dependencies"
      owner_module: "taskq_api.api.dependencies"
      nfr: NFR-02
      verified_by: "test_sec_t02_admin_scope_enforced"
    - id: T-03
      boundary: TB-01
      category: denial_of_service
      description: "caller floods endpoints to exhaust backend resources"
      mitigation: "per-token token bucket rate limit (FR-05); 429 + Retry-After when TASHQ_RATE_BURST exceeded"
      owner_module: "taskq_api.service.rate_limit"
      nfr: NFR-02
      verified_by: "test_sec_t03_rate_limit_returns_429"
    - id: T-04
      boundary: TB-02
      category: spoofing
      description: "forged X-API-Key impersonates another tenant"
      mitigation: "sha256 hash storage; hmac.compare_digest constant-time compare; revoked_at filter; plaintext printed once"
      owner_module: "taskq_api.service.auth"
      nfr: NFR-02
      verified_by: "test_sec_t04_constant_time_compare_and_hash_storage"
    - id: T-05
      boundary: TB-02
      category: information_disclosure
      description: "403 response reveals whether a task id exists"
      mitigation: "scope check runs before resource lookup; identical problem+json body for missing scope vs missing resource"
      owner_module: "taskq_api.service.auth"
      nfr: NFR-02
      verified_by: "test_sec_t05_403_does_not_leak_resource_existence"
    - id: T-06
      boundary: TB-03
      category: elevation_of_privilege
      description: "command injection via task command string passed to subprocess"
      mitigation: "asyncio.create_subprocess_exec(*shlex.split(command)) with shell=False banned; grep gate enforces zero shell=True / eval / exec"
      owner_module: "taskq_api.service.runner"
      nfr: NFR-02
      verified_by: "test_sec_t06_no_shell_true_in_source"
    - id: T-07
      boundary: TB-03
      category: denial_of_service
      description: "long-running task exhausts subprocess slots or leaves orphan processes"
      mitigation: "TASKQ_MAX_CONCURRENT semaphore + asyncio.TaskGroup; timeout via asyncio.wait_for + process.kill + await process.wait"
      owner_module: "taskq_api.service.runner"
      nfr: NFR-03
      verified_by: "test_sec_t07_no_orphan_process_after_timeout"
    - id: T-08
      boundary: TB-04
      category: tampering
      description: "SQL injection via string-concatenated query"
      mitigation: "ORM-only repository; grep gate forbids f-string/% /+ SQL; import-linter forbids sqlalchemy outside repository"
      owner_module: "taskq_api.repository.session"
      nfr: NFR-02
      verified_by: "test_sec_t08_no_sql_string_concat"
    - id: T-09
      boundary: TB-05
      category: information_disclosure
      description: "500 error body or log line leaks stack trace, SQL, file path, or DB password"
      mitigation: "errors.problem allow-list detail scrubber; config.scrub_db_url strips password; regex redacts sk-/token=/Bearer/postgres:// from stdout/stderr/logs/metrics"
      owner_module: "taskq_api.errors"
      nfr: NFR-04
      verified_by: "test_sec_t09_500_body_and_log_scrubbed"
    - id: T-10
      boundary: TB-06
      category: tampering
      description: "v3 migration silently drops or corrupts data when splitting result_json"
      mitigation: "alembic upgrade/downgrade round-trip test against real SQLite; downgrade -1 then upgrade head must preserve every column value"
      owner_module: "migrations.versions.v3_split_results"
      nfr: NFR-03
      verified_by: "test_sec_t10_migration_roundtrip_data_integrity"
```
<!-- SEC:END -->

Note: `owner_module` must name a module declared in the §5 SAB block;
`nfr` (optional) must exist in SRS.md; `verified_by` names the test that
proves the mitigation — from Phase 5 onward, `check-artifact-consistency`
blocks if that test doesn't exist yet. Threats also seed
`bug-hunt-targets`' adversarial-review targeting and force NFR-pattern
test cases in `derive_test_cases.md` Step 1c regardless of SRS keywords.

---

*End of SAD — Phase 2 deliverable for `taskq-api`. Subsequent phases:
ADR.md (architecture decisions), TEST_SPEC.md (test design), SAB
generation.*
