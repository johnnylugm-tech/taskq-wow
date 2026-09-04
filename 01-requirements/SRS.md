# Software Requirements Specification (SRS) — taskq-api

> **Source of truth**: This SRS transcribes `/Users/johnny/projects/taskq-wow/SPEC.md` (v1.0.0, 2026-07-30). Per FR/NFR entry below cites the canonical line range in SPEC.md.
>
> **Document role**: Phase 1 deliverable for harness-methodology round-2 progressive verification testbed (Python backend + database). The spec adds HTTP layer, real database, real schema migration, and async to the round-1 surface.
>
> **Ingestion mode**: 100% transcription of `### FR-01..FR-10` and `### NFR-01..NFR-12` from SPEC.md; no invention, no silent omission. TBD/TODO/placeholders are emitted as `NFR-99` / `FR-XX-deferred` items in §7 Open Issues.

---

## 1. Introduction

### 1.1 Project Identity

| Field | Value |
|-------|-------|
| Project name | `taskq-api` |
| Spec version | v1.0.0 (2026-07-30) |
| Verification round | Round 2 / 3 |
| Predecessor testbed | `taskq-plus` (SPEC.md v1.0.0, 8 FR / 12 NFR) |
| Companion files | `PROJECT_BRIEF-2.md` (10 FR / 12 NFR / 12 env), `.env.example`, `.importlinter`, `requirements.txt`, `alembic.ini`, `Makefile` |
| Document role | Single Source of Truth for all implementation |

### 1.2 Purpose (canonical, SPEC.md §1)

The `taskq-api` project exists to expose a task queue over HTTP — submit, query, and execute tasks via a REST API; persist data in a relational database; evolve the schema across versions; and provide authentication, authorization, and rate-limiting.

### 1.3 Scope

- **Language**: Python 3.11.
- **Form**: ASGI service launched as `uvicorn taskq_api.app:app`. A management entry point `python -m taskq_api` exposes `migrate` / `seed` / `healthcheck`.
- **Out of round-2 scope** (carried from round 1, unchanged): distributed consensus, multi-region replication, fine-grained RBAC beyond `read` / `write` / `admin`, web UI.

### 1.4 Round-2 Design Intent (canonical, SPEC.md §0 本輪設計意圖)

Round 1 (`taskq-plus`) lit up `license_compliance` / `architecture_constraints` / `mutation_testing` / `test_assertion_quality` but remained a single-process CLI. The following surfaces produced no signal in the prior two testbeds and are introduced here:

| Uncovered surface | Round-2 mitigation | Clause |
|---|---|---|
| No HTTP layer → `security` only scans subprocess | REST API + API key auth + per-token scope + rate limit | FR-03/04/05, NFR-02 |
| No database → ORM, transactions, pool, N+1 all zero | SQLAlchemy ORM + explicit transaction boundaries + N+1 guard assertion | FR-06, NFR-01 |
| Self-built JSON `version` field for schema | **Real Alembic three-step evolution** + data migration + reversible downgrade | FR-07, NFR-03 |
| No async → `async def` paths never scanned | async endpoints + asyncio background runner | FR-08, NFR-03 |
| Shallow dependency tree (2 direct) | fastapi / sqlalchemy / alembic / uvicorn + transitive | NFR-07 |
| Integration tests only exercised CLI subprocess | `httpx.ASGITransport` end-to-end, covers auth & error contract | NFR-10 |

---

## 2. Constraints

Constraints in this section are normative inputs from SPEC.md §2 (技術架構) and §5.3 (專案側必備設定檔). They constrain every FR/NFR below.

| Component | Technology |
|---|---|
| HTTP framework | FastAPI (ASGI) |
| Validation | `pydantic` v2 request/response models |
| ORM | SQLAlchemy 2.x (declarative + explicit `Session` transactions) |
| Database | SQLite (dev/test), PostgreSQL (prod) — same ORM model |
| Migration | **Alembic** (v1 → v2 → v3, every step has a working `downgrade`) |
| Async | `async def` endpoints + `asyncio.TaskGroup` background runner |
| Authentication | `X-API-Key` header, key hashed (never plaintext) |
| Authorization | per-token scope: `read` / `write` / `admin` (hierarchical) |
| Rate limit | per-token token bucket |
| Error contract | RFC 7807 `application/problem+json` |
| Task execution | `asyncio.create_subprocess_exec` (`shell=True` forbidden) |
| Layering | `import-linter` layers contract (see NFR-06) |

**Project-side mandatory files** (non-optional, cited from SPEC.md §5.3):

| File | Purpose | Source NFR |
|---|---|---|
| `.importlinter` | Layer contract + `sqlalchemy` forbidden | NFR-06 |
| `requirements.txt` + `requirements.lock` | Pinning + transitive lock | NFR-07 |
| `requirements-dev.txt` | `import-linter` / `pip-licenses` / `mutmut` / `pytest-benchmark` / `httpx` | NFR-06/07/08/10 |
| `alembic.ini` + `migrations/versions/` | Three revisions (FR-07) | FR-07 |
| `.env.example` | All 12 `TASKQ_*` declared with annotation | §5.1 |
| `.methodology/harness_config.json` | `features.mutation_testing: true`; do not lower `crg_cohesion_healthy` | NFR-08 |
| `Makefile` | `verify-system` target (includes migration round-trip) | NFR-12 |

---

## 3. Functional Requirements

### FR-01: 任務資源 CRUD API

| Method | Path | Scope | Behavior |
|---|---|---|---|
| `POST` | `/v1/tasks` | `write` | Create a task; body validated by `TaskCreate` pydantic model |
| `GET` | `/v1/tasks/{id}` | `read` | Fetch a single task with all fields |
| `GET` | `/v1/tasks` | `read` | Paginated list, supports `?status=`, `?limit=`, `?cursor=` |
| `DELETE` | `/v1/tasks/{id}` | `admin` | Delete task (and result rows, in the same transaction) |

- Validation rules inherited from round-1 FR-01: non-empty / ≤1000 chars / injection-character denylist / unique name; violation → **HTTP 422** + problem+json
- Unknown id → **HTTP 404** + problem+json
- Pagination is **cursor-based** (offset is forbidden — large-table offset scans are N+1's cousin)
- The list endpoint's default `limit` is 50; upper bound 200; exceeding → 422

**Acceptance criteria**:

- **AC-1.1** **DERIVED: SPEC.md line 83 — chose '201 + task id' framing to make testable; canonical 'POST /v1/tasks (write) 建立任務; body 由 TaskCreate pydantic 模型驗證'** POST `/v1/tasks` with a valid `write`-scope key returns 201 and a task id — decided by integration test `test_post_tasks_201_with_write_key_returns_id`, per SPEC.md line 83.
- **AC-1.2** **DERIVED: SPEC.md line 86 — chose '401 + problem+json' framing; canonical '缺少或無效 API key -> 401'** POST `/v1/tasks` with missing `X-API-Key` returns **401** + problem+json — decided by `test_post_tasks_401_without_api_key`, per SPEC.md line 86.
- **AC-1.3** **DERIVED: SPEC.md line 88 — chose '422 + problem+json' framing; canonical lists the validation set** POST `/v1/tasks` with body violating validation (empty / >1000 chars / denylisted chars / duplicate name) returns **422** + problem+json — decided by `test_post_tasks_422_validation_failure`, per SPEC.md line 88.
- **AC-1.4** **DERIVED: SPEC.md line 88 — chose '409 duplicate name' framing; canonical 'name 唯一'** POST `/v1/tasks` with duplicate `name` returns **409** — decided by `test_post_tasks_409_duplicate_name`, per SPEC.md line 88 (validation set).
- **AC-1.5** **DERIVED: SPEC.md line 84 — chose 'full task record' framing; canonical '取得單一任務全欄位'** GET `/v1/tasks/{id}` with valid `read`-scope key returns the full task record — decided by `test_get_task_200_returns_full_record`, per SPEC.md line 84.
- **AC-1.6** **DERIVED: SPEC.md line 89 — chose 'no existence leak' framing; canonical '未知 id -> 404'** GET `/v1/tasks/{unknown_id}` returns **404** + problem+json without leaking existence to unauthorized callers — decided by `test_get_task_404_unknown_id`, per SPEC.md line 89.
- **AC-1.7** **DERIVED: SPEC.md lines 90–91 — chose 'cursor + limit cap' framing; canonical 'cursor-based ... 上限 200'** GET `/v1/tasks?limit=50` uses cursor-based pagination (not offset) and rejects `limit > 200` with 422 — decided by `test_list_tasks_cursor_pagination_and_limit_cap`, per SPEC.md lines 90–91.
- **AC-1.8** **DERIVED: SPEC.md line 86 — chose 'removes results in same transaction' framing; canonical '連同結果列, 同一交易'** DELETE `/v1/tasks/{id}` with `admin` scope succeeds (200/204) and removes both the task row and its `task_results` rows in the same transaction — decided by `test_delete_task_removes_results_in_single_transaction`, per SPEC.md line 86.
- **AC-1.9** **DERIVED: SPEC.md line 112 — chose 'no existence leak' framing; canonical 'body 不得洩漏該資源是否存在'** DELETE `/v1/tasks/{id}` with `write` (non-admin) scope returns **403** and the body does not disclose whether the id exists — decided by `test_delete_task_403_write_scope_no_existence_leak`, per SPEC.md line 112.

---

### FR-02: 任務執行端點

- `POST /v1/tasks/{id}/run` (scope `write`) → **HTTP 202 Accepted**, body contains `run_id`
- Execution uses `asyncio.create_subprocess_exec(*shlex.split(command))`; **`shell=True` is forbidden**; timeout is `TASKQ_TASK_TIMEOUT`
- State machine: `pending → running → done | failed | timeout`
- Execution results written to `task_results` table (FR-07 v3 schema), fields: `exit_code` / `stdout_tail` / `stderr_tail` / `duration_ms` / `finished_at`
- `GET /v1/tasks/{id}/runs` (scope `read`) → historical run records for that task, newest first

**Acceptance criteria**:

- **AC-2.1** **DERIVED: SPEC.md line 95 — chose '202 with run_id' framing; canonical 'HTTP 202 Accepted, body 含 run_id'** POST `/v1/tasks/{id}/run` with valid `write` scope returns **202** with a body that contains a `run_id` — decided by `test_run_task_202_returns_run_id`, per SPEC.md line 95.
- **AC-2.2** **DERIVED: SPEC.md line 96 + §8 #16 — chose 'grep 0 hits shell=True' framing; canonical '禁 shell=True'** The runner executes the task via `asyncio.create_subprocess_exec(*shlex.split(command))`; **`shell=True` does not appear** anywhere in `03-development/src/` (grep gate, 0 hits) — decided by `test_no_shell_true_in_source_tree`, per SPEC.md line 96 + §8 #16.
- **AC-2.3** **DERIVED: SPEC.md line 149 — chose 'no orphan subprocess' framing; canonical 'process.kill() 後 await process.wait(), 不得留下孤兒進程'** Task timeout uses `asyncio.wait_for`; on timeout the runner calls `process.kill()` and `await process.wait()` so no orphan subprocess remains — decided by `test_runner_timeout_kills_subprocess_no_orphan`, per SPEC.md lines 96 + 149.
- **AC-2.4** **DERIVED: SPEC.md line 98 — chose 'five fields populated' framing; canonical lists the five fields** Successful run produces a `task_results` row with `exit_code` / `stdout_tail` / `stderr_tail` / `duration_ms` / `finished_at` populated — decided by `test_run_result_persists_to_task_results_table`, per SPEC.md line 98.
- **AC-2.5** **DERIVED: SPEC.md line 99 — chose 'newest-first + read scope' framing; canonical '新到舊排序'** GET `/v1/tasks/{id}/runs` returns run history newest-first; requires `read` scope; returns 403 for `read`-missing keys — decided by `test_list_runs_ordered_newest_first`, per SPEC.md line 99.
- **AC-2.6** **DERIVED: SPEC.md line 97 — chose 'state machine transitions' framing; canonical 'pending -> running -> done | failed | timeout'** The task state machine transitions `pending → running → done | failed | timeout` and never enters an undefined state — decided by `test_task_state_machine_transitions`, per SPEC.md line 97.

---

### FR-03: API Key 認證

- All `/v1/*` endpoints require `X-API-Key` header; missing or invalid → **HTTP 401** + problem+json
- Keys are **stored as SHA-256 hashes** in the `api_keys` table; **plaintext is never stored**; comparison uses `hmac.compare_digest` (constant time)
- Keys are generated by `python -m taskq_api key create --scope <scope>`; the plaintext is **only printed once at creation time**
- Revoked keys: any key with non-null `revoked_at` is treated as invalid
- `/healthz`, `/readyz` do not require authentication (FR-09)

**Acceptance criteria**:

- **AC-3.1** **DERIVED: SPEC.md line 103 — chose 'every /v1/* endpoint' framing; canonical '全部 /v1/* 端點要求 X-API-Key'** All `/v1/*` endpoints return **401** + problem+json when `X-API-Key` is missing or invalid — decided by `test_v1_endpoints_401_without_api_key`, per SPEC.md line 103.
- **AC-3.2** **DERIVED: SPEC.md line 104 + §8 #18 — chose 'sha256 hex 64 chars' framing; canonical 'SHA-256 雜湊'** `api_keys.key_hash` is a 64-character hex string (SHA-256) for every row; no row contains a plaintext key — decided by `test_api_keys_table_stores_only_sha256_hashes`, per SPEC.md line 104 + §8 #18.
- **AC-3.3** **DERIVED: SPEC.md line 104 — chose 'constant-time comparison' framing; canonical 'hmac.compare_digest (常數時間)'** Key comparison uses `hmac.compare_digest` (constant-time) — decided by `test_key_compare_uses_constant_time`, per SPEC.md line 104.
- **AC-3.4** **DERIVED: SPEC.md line 105 — chose 'printed exactly once' framing; canonical '明文只在建立當下印出一次'** `python -m taskq_api key create --scope <scope>` prints the plaintext exactly once and never persists it — decided by `test_key_create_prints_plaintext_once`, per SPEC.md line 105.
- **AC-3.5** **DERIVED: SPEC.md line 106 — chose 'revoked key rejected' framing; canonical 'revoked_at 非空的金鑰一律視為無效'** A key with non-null `revoked_at` is rejected with 401 even if the presented plaintext matches — decided by `test_revoked_key_returns_401`, per SPEC.md line 106.
- **AC-3.6** **DERIVED: SPEC.md line 107 — chose 'no auth required' framing; canonical '不要求認證 (FR-09)'** `/healthz` and `/readyz` do not require authentication (return 200 with no header) — decided by `test_healthz_readyz_no_auth_required`, per SPEC.md line 107.

---

### FR-04: Scope 授權

- Each key carries a scope: `read` < `write` < `admin` (hierarchical inclusion)
- Endpoint scope requirements per FR-01/02 tables; insufficient → **HTTP 403** + problem+json; **the body must not disclose whether the resource exists**
- Authorization must be enforced by **a single dependency (middleware)**, not scattered across handlers — verified by a test asserting "every `/v1` route goes through the same dependency"

**Acceptance criteria**:

- **AC-4.1** **DERIVED: SPEC.md line 111 — chose 'hierarchy inclusion' framing; canonical 'read < write < admin (階層包含)'** Scope hierarchy `read ⊂ write ⊂ admin`: an `admin` key passes `write` and `read` checks; a `write` key passes `read` checks; a `read` key is rejected by `write`/`admin` endpoints — decided by `test_scope_hierarchy_inclusion`, per SPEC.md line 111.
- **AC-4.2** **DERIVED: SPEC.md line 112 — chose 'no existence leak in 403 body' framing; canonical 'body 不得洩漏該資源是否存在'** Insufficient scope returns **403** + problem+json with a body that does not reveal whether the targeted resource exists — decided by `test_insufficient_scope_403_no_existence_leak`, per SPEC.md line 112.
- **AC-4.3** **DERIVED: SPEC.md line 113 — chose 'single shared dependency' framing; canonical '單一中介層 (dependency)'** Authorization is enforced by a single dependency shared across all `/v1` routes; no handler performs its own scope check — decided by `test_all_v1_routes_share_single_auth_dependency`, per SPEC.md line 113.

---

### FR-05: 流量控制

- Per-token token bucket: capacity `TASKQ_RATE_BURST`, refill rate `TASKQ_RATE_PER_SEC`
- Exceeded → **HTTP 429** + problem+json + `Retry-After` header (seconds)
- Bucket state stored in the database (consistent across workers); updates must use row-level locking within a single transaction
- `/healthz`, `/readyz` are exempt

**Acceptance criteria**:

- **AC-5.1** **DERIVED: SPEC.md line 118 + §8 #9 — chose '429 + Retry-After' framing; canonical + verification row** A burst exceeding `TASKQ_RATE_BURST` within the configured window returns **429** + problem+json with a `Retry-After` header — decided by `test_rate_limit_429_with_retry_after`, per SPEC.md line 118 + §8 #9.
- **AC-5.2** **DERIVED: SPEC.md line 119 — chose 'persists + row lock' framing; canonical '狀態存於資料庫 (跨 worker 一致), 更新必須在單一交易內以 row-level lock 進行'** Bucket state is persisted to the database (visible across worker restarts); updates occur in a single transaction with row-level locking — decided by `test_rate_bucket_persists_across_restart_and_uses_row_lock`, per SPEC.md line 119.
- **AC-5.3** **DERIVED: SPEC.md line 120 — chose 'healthz/readyz exempt' framing; canonical '/healthz、/readyz 不受限'** `/healthz` and `/readyz` are not rate-limited — decided by `test_healthz_readyz_exempt_from_rate_limit`, per SPEC.md line 120.

---

### FR-06: 持久化層與交易邊界

- All data access goes through the `repository/` layer; **the business layer must not hold a `Session` directly**
- One `Session` per API request; transaction boundaries are explicit: success commits, exceptions roll back (guaranteed by context manager)
- **String-concatenated SQL is forbidden**; use ORM or parameterized queries only (NFR-02)
- Eager loading for associations must use `selectinload` / `joinedload` explicitly — **N+1 is an acceptance failure** (NFR-01)
- Connection pool: `pool_size=TASKQ_DB_POOL_SIZE`, `pool_pre_ping=True`

**Acceptance criteria**:

- **AC-6.1** **DERIVED: SPEC.md line 124 + NFR-06 — chose 'repository-only sqlalchemy' framing; canonical '業務層不得直接持有 Session' + NFR-06 forbidden contract** Business-layer code (`service/`, `api/`) does not import `sqlalchemy` directly; the `repository/` layer is the only importer (verified by `import-linter` forbidden contract in NFR-06) — decided by `test_repository_is_only_sqlalchemy_importer`, per SPEC.md line 124 + NFR-06.
- **AC-6.2** **DERIVED: SPEC.md line 125 — chose 'one Session per request' framing; canonical '每個 API 請求一個 Session'** Each request opens exactly one `Session`; success commits, exceptions roll back via context manager — decided by `test_request_session_scope_commit_on_success_rollback_on_exception`, per SPEC.md line 125.
- **AC-6.3** **DERIVED: SPEC.md line 126 + §8 #17 — chose '0 grep hits SQL concat' framing; canonical '禁止字串拼接 SQL'** `grep -rn` over `03-development/src/` for f-string / `%` / `+` SQL concatenation yields 0 hits — decided by `test_no_sql_string_concatenation_in_source`, per SPEC.md line 126 + §8 #17.
- **AC-6.4** **DERIVED: SPEC.md line 127 + §8 #14 — chose 'constant SQL count' framing; canonical 'selectinload / joinedload 顯式預載 — N+1 為驗收失敗條件'** List endpoints use `selectinload` / `joinedload` to eager-load associations; the number of SQL statements per list request is **constant** (independent of returned rows) — decided by `test_list_endpoint_constant_sql_count_no_n_plus_1`, per SPEC.md lines 127 + §8 #14.
- **AC-6.5** **DERIVED: SPEC.md line 128 — chose 'pool config' framing; canonical 'pool_size=TASKQ_DB_POOL_SIZE, pool_pre_ping=True'** SQLAlchemy engine uses `pool_size=TASKQ_DB_POOL_SIZE` and `pool_pre_ping=True` — decided by `test_engine_pool_size_and_pre_ping_configured`, per SPEC.md line 128.

---

### FR-07: Schema Migration (Alembic 三步演進)

Three revisions; every step must have a working `downgrade`:

| revision | upgrade content | downgrade requirement |
|---|---|---|
| **v1** | Create `tasks`, `api_keys` tables | drop both tables |
| **v2** | Add `tags`, `task_tags` (many-to-many) + unique index on `tasks.name` | drop new tables and index, do not touch v1 data |
| **v3** | **Data migration**: split `tasks.result_json` into a separate `task_results` table; migrate existing data; drop original column | reverse-migrate back to `tasks.result_json` then drop `task_results`; **no data loss** |

- `alembic upgrade head` and `alembic downgrade base` must both succeed
- **Round-trip reversibility**: `upgrade head` → write sample data → `downgrade -1` → `upgrade head`; every column of the sample data must be byte-identical (v3 data migration is the focus)
- Destructive shortcuts such as `op.execute("DROP TABLE ...")` are forbidden as a substitute for real downgrade
- Migration files themselves are covered by tests (offline SQL generation + assertions)

**Acceptance criteria**:

- **AC-7.1** **DERIVED: SPEC.md line 140 — chose 'fresh DB upgrade head' framing; canonical 'alembic upgrade head ... 必須都成功'** `alembic upgrade head` succeeds on a fresh database — decided by `test_alembic_upgrade_head_succeeds`, per SPEC.md line 140.
- **AC-7.2** **DERIVED: SPEC.md line 140 + §8 #13 — chose 'downgrade base no residual' framing; canonical + verification row** `alembic downgrade base` succeeds after `upgrade head`; no tables remain — decided by `test_alembic_downgrade_base_no_residual_tables`, per SPEC.md lines 140 + §8 #13.
- **AC-7.3** **DERIVED: SPEC.md line 137 — chose 'v2 adds tags + unique index' framing; canonical '新增 tags、task_tags (多對多) + tasks.name 唯一索引'** v2 migration adds `tags`, `task_tags`, and a unique index on `tasks.name` without altering v1 data — decided by `test_alembic_v2_adds_tags_and_unique_index`, per SPEC.md line 137.
- **AC-7.4** **DERIVED: SPEC.md line 138 — chose 'v3 data migration' framing; canonical '把 tasks.result_json 拆為獨立的 task_results 表'** v3 migration splits `tasks.result_json` into `task_results`, migrates existing rows, drops the original column — decided by `test_alembic_v3_splits_results_with_data_migration`, per SPEC.md line 138.
- **AC-7.5** **DERIVED: SPEC.md line 141 + §8 #12 — chose 'bytewise column equality' framing; canonical '樣本資料的欄位值必須逐欄相同'** Round-trip reversibility: write sample data → `downgrade -1` → `upgrade head`; every column of the sample is byte-identical to the pre-roundtrip state — decided by `test_migration_round_trip_preserves_sample_data_bytewise`, per SPEC.md lines 141 + §8 #12.
- **AC-7.6** **DERIVED: SPEC.md line 142 — chose 'no DROP TABLE shortcut' framing; canonical '禁止以 op.execute DROP TABLE 取代真正的 downgrade'** No `op.execute("DROP TABLE ...")` (or equivalent destructive shortcut) substitutes for a real downgrade — decided by `test_migration_files_have_real_downgrade_no_drop_table_shortcuts`, per SPEC.md line 142.
- **AC-7.7** **DERIVED: SPEC.md line 143 — chose 'offline SQL generation' framing; canonical '以 alembic 的 offline SQL 產生 + 斷言'** Migration files are covered by tests using offline SQL generation plus assertions on the SQL text — decided by `test_migration_offline_sql_generation_matches_expectations`, per SPEC.md line 143.

---

### FR-08: 非同步執行器

- Background execution is managed by `asyncio.TaskGroup`; on shutdown the service must perform **graceful drain** (wait for in-flight tasks up to `TASKQ_DRAIN_TIMEOUT`; mark remaining as `interrupted`)
- Concurrency cap `TASKQ_MAX_CONCURRENT`; excess tasks queue, no unbounded coroutine creation
- Task timeout is implemented with `asyncio.wait_for`; on timeout the **child process must actually be terminated** (`process.kill()` then `await process.wait()`); no orphan processes
- Cancellation semantics: `asyncio.CancelledError` must propagate; **`except Exception` must not swallow it** (NFR-03)

**Acceptance criteria**:

- **AC-8.1** **DERIVED: SPEC.md line 147 — chose 'TaskGroup + concurrency cap' framing; canonical 'asyncio.TaskGroup + TASKQ_MAX_CONCURRENT'** `asyncio.TaskGroup` manages background tasks; concurrency never exceeds `TASKQ_MAX_CONCURRENT` — decided by `test_runner_concurrency_capped_at_max_concurrent`, per SPEC.md line 147.
- **AC-8.2** **DERIVED: SPEC.md line 147 + §8 #25 — chose 'graceful drain + interrupted marker' framing; canonical + verification row** On shutdown, in-flight tasks have up to `TASKQ_DRAIN_TIMEOUT` to finish; tasks exceeding the deadline are marked `interrupted` — decided by `test_shutdown_graceful_drain_marks_overdue_interrupted`, per SPEC.md line 147 + §8 #25.
- **AC-8.3** **DERIVED: SPEC.md line 149 — chose 'kill + wait no orphans' framing; canonical 'process.kill() 後 await process.wait()'** Task timeout uses `asyncio.wait_for` and kills the child process via `process.kill()` then `await process.wait()`; no orphan subprocesses remain after timeout — decided by `test_timeout_kills_child_process_no_orphans`, per SPEC.md line 149.
- **AC-8.4** **DERIVED: SPEC.md line 150 + NFR-03 — chose 'CancelledError propagates' framing; canonical 'CancelledError 必須向上傳播, 不得被 except Exception 吞掉'** `asyncio.CancelledError` is not caught by a bare `except Exception:`; it propagates up — decided by `test_cancelled_error_propagates_through_exception_handlers`, per SPEC.md line 150 + NFR-03.

---

### FR-09: 健康檢查與可觀測性

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /healthz` | none | process alive → 200 `{"status":"ok"}` |
| `GET /readyz` | none | DB reachable **and** `alembic current` == head → 200; otherwise **503** with body explaining which check failed |
| `GET /v1/metrics` | `admin` | task counts (by status), execution latency percentiles, rate-limit rejection counts |

- `/readyz`'s "migration not at head" check is critical: a deployment that shipped new code but forgot to run migrations must **fail closed**

**Acceptance criteria**:

- **AC-9.1** **DERIVED: SPEC.md line 156 — chose 'healthz 200 no auth' framing; canonical 'GET /healthz | 無 | 進程存活 -> 200 {status:ok}'** GET `/healthz` returns 200 with `{"status":"ok"}` while the process is alive, with no authentication required — decided by `test_healthz_200_no_auth`, per SPEC.md line 156.
- **AC-9.2** **DERIVED: SPEC.md line 157 — chose 'readyz 200 when DB+migrations OK' framing; canonical 'DB 連線可用且 alembic current == head -> 200'** GET `/readyz` returns 200 when DB is reachable **and** `alembic current == head` — decided by `test_readyz_200_when_db_ok_and_migrations_at_head`, per SPEC.md line 157.
- **AC-9.3** **DERIVED: SPEC.md line 157 + §8 #10 — chose 'readyz 503 DB unreachable' framing; canonical '否則 503 並在 body 說明哪一項失敗'** After stopping the DB, GET `/readyz` returns **503** with `detail` identifying the DB failure — decided by `test_readyz_503_when_db_unreachable`, per SPEC.md line 157 + §8 #10.
- **AC-9.4** **DERIVED: SPEC.md line 157 + §8 #11 — chose 'readyz 503 migration not at head (fail-closed)' framing; canonical + verification row** After `alembic downgrade -1`, GET `/readyz` returns **503** with `detail` identifying the migration-not-at-head failure (fail-closed) — decided by `test_readyz_503_when_migration_not_at_head`, per SPEC.md line 157 + §8 #11.
- **AC-9.5** **DERIVED: SPEC.md line 158 — chose 'metrics admin scope no DB URL leak' framing; canonical 'GET /v1/metrics | admin | ... + NFR-04'** GET `/v1/metrics` requires `admin` scope; returns task counts by status, execution latency percentiles, rate-limit rejection counts; does not leak the DB connection string (NFR-04) — decided by `test_metrics_admin_scope_no_db_url_leak`, per SPEC.md line 158.

---

### FR-10: 錯誤契約 (RFC 7807)

- All non-2xx responses have `Content-Type: application/problem+json`
- Body fields: `type` (URI), `title`, `status`, `detail`, `instance`, `correlation_id`
- **`detail` must not leak internals**: no SQL statements, stack traces, file paths, or DB schema
- `correlation_id` appears in both the response header `X-Correlation-Id` and the server log for stitching
- Error code mapping: 422 validation / 401 unauthenticated / 403 insufficient scope / 404 unknown resource / 409 name conflict / 429 rate limited / 503 not ready / 500 other

**Acceptance criteria**:

- **AC-10.1** **DERIVED: SPEC.md line 164 — chose 'Content-Type problem+json' framing; canonical '全部非 2xx 回應的 Content-Type 為 application/problem+json'** Every non-2xx response sets `Content-Type: application/problem+json` — decided by `test_all_error_responses_use_problem_json_content_type`, per SPEC.md line 164.
- **AC-10.2** **DERIVED: SPEC.md line 165 — chose 'six fields present' framing; canonical 'body 欄位: type, title, status, detail, instance, correlation_id'** Problem+json bodies carry `type` / `title` / `status` / `detail` / `instance` / `correlation_id` — decided by `test_problem_json_body_has_required_fields`, per SPEC.md line 165.
- **AC-10.3** **DERIVED: SPEC.md line 166 + §8 #19 — chose '500 detail no internals' framing; canonical 'detail 不得洩漏內部細節'** Triggering a 500 (unhandled exception) produces a `detail` that contains no stack trace, no SQL, no file path, no DB schema — decided by `test_500_detail_omits_internals`, per SPEC.md line 166 + §8 #19.
- **AC-10.4** **DERIVED: SPEC.md line 167 — chose 'correlation_id consistency' framing; canonical 'correlation_id 同時出現在回應 header X-Correlation-Id 與伺服器日誌'** `correlation_id` from the problem+json body equals the `X-Correlation-Id` response header and equals the corresponding server log line — decided by `test_correlation_id_consistent_across_response_and_log`, per SPEC.md line 167.
- **AC-10.5** **DERIVED: SPEC.md line 168 — chose 'error code mapping observed' framing; canonical '錯誤碼對照 422/401/403/404/409/429/503/500'** Error code mapping table (SPEC.md §7) is observed end-to-end: 422 / 401 / 403 / 404 / 409 / 429 / 503 / 500 — decided by `test_error_code_mapping_observed_per_spec_table`, per SPEC.md line 168.

---

## 4. Non-Functional Requirements

> **Dimension mapping**: every NFR's `dimension` field is one of the headings currently listed in `/Users/johnny/projects/taskq-wow/harness/harness/ssi/prompts/evaluate_dimension.md` (`### <dimension>` headers). The mapping is verified below; no canonical dimension in this SRS is missing from the current roster.
>
> **Terminology harmonization (downstream dim-mapper cross-reference)**: the NFR category label appears under three names across P1 artifacts — SRS §4 body uses `dimension` (e.g., `performance`, `error_handling`); the machine FR Block at §8 uses `type` (e.g., `reliability`, `layering`); `SPEC_TRACKING.md` uses `Intent Class` (e.g., `error_handling / reliability`, `architecture_constraints`). All three label-sets are reasonable; eight of twelve NFRs (NFR-03, NFR-06, NFR-07, NFR-08, NFR-09, NFR-10, NFR-11, NFR-12) carry divergent but synonymous labels. The §8 FR Block `type` field is the canonical machine-readable form; the §4 `dimension` body and `SPEC_TRACKING.md` `Intent Class` are human-readable aliases that downstream dim-mappers should treat as equivalent for routing to `evaluate_dimension.md` headings.

### NFR-01: 效能與查詢效率

- **dimension**: `performance`
- `GET /v1/tasks/{id}` under 10,000 records: **p95 < 30ms** (excluding network, measured via ASGI transport)
- `GET /v1/tasks?limit=50` under 10,000 records: **p95 < 80ms**
- **N+1 is a failure condition**: list endpoints must issue a **constant** number of SQL statements per request (independent of returned rows), asserted via a SQLAlchemy event listener counter
- Measurement: `pytest-benchmark`

**Acceptance criteria**:

- **AC-N1.1** **DERIVED: SPEC.md line 180 + §8 #15 — chose 'p95 < 30ms benchmark' framing; canonical + verification row** `GET /v1/tasks/{id}` benchmark on a 10,000-record dataset reports `p95 < 30ms` — decided by `test_perf_get_task_p95_under_30ms`, per SPEC.md line 180 + §8 #15.
- **AC-N1.2** **DERIVED: SPEC.md line 181 + §11 — chose 'list p95 < 80ms' framing; canonical + monitoring threshold** `GET /v1/tasks?limit=50` benchmark on a 10,000-record dataset reports `p95 < 80ms` — decided by `test_perf_list_tasks_p95_under_80ms`, per SPEC.md line 181 + §11.
- **AC-N1.3** **DERIVED: SPEC.md line 182 + §8 #14 — chose 'SQLAlchemy event listener constant count' framing; canonical '列表端點回應一次請求所發出的 SQL 陳述數必須是常數 (與回傳筆數無關)'** A SQLAlchemy event listener asserts that `GET /v1/tasks?limit=50` issues a constant number of SQL statements regardless of returned-row count (N+1 guard) — decided by `test_perf_no_n_plus_1_constant_sql_count`, per SPEC.md line 182 + §8 #14.

**Coverage note (AC-N1.1, AC-N1.2)**: The harness `performance` dimension scores only `mean` latency from `pytest-benchmark`. p95 thresholds from this NFR are not directly enforced by the dimension score; the project must add a test that asserts p95, since the dimension only penalizes `mean > 1000 ms` or `mean > 3000 ms`. The p95 ACs above are not redundant with the dimension score and require their own dedicated test.

---

### NFR-02: HTTP 與資料層安全

- **dimension**: `security`
- Codebase-wide ban on `shell=True`, `eval(`, `exec(` (grep yields 0 hits)
- **String-concatenated SQL is forbidden**: no f-string / `%` / `+` SQL composition; ORM or parameterized only (grep + code review double-check)
- API keys **stored hashed**; compared via `hmac.compare_digest` (FR-03)
- 403 responses must not leak resource existence (FR-04)
- Error bodies must not contain stack/SQL/path (FR-10)
- CORS **denies all origins by default**; allow-list supplied by `TASKQ_CORS_ORIGINS`
- `bandit -r 03-development/src/`: **0 HIGH, 0 MEDIUM**

**Acceptance criteria**:

- **AC-N2.1** **DERIVED: SPEC.md line 188 + §8 #16 — chose '0 grep hits' framing; canonical '全 codebase 禁用 shell=True、eval(、exec('** `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` yields 0 hits — decided by `test_no_shell_eval_exec_in_source`, per SPEC.md line 188 + §8 #16.
- **AC-N2.2** **DERIVED: SPEC.md line 189 + §8 #17 — chose '0 grep hits SQL concat' framing; canonical '禁止字串拼接 SQL'** Grep + code-review CI gate over `03-development/src/` for f-string / `%` / `+` SQL composition yields 0 hits — decided by `test_no_sql_string_concatenation_grep_gate`, per SPEC.md line 189 + §8 #17.
- **AC-N2.3** **DERIVED: SPEC.md line 194 + §8 #23 — chose 'bandit 0 HIGH 0 MEDIUM' framing; canonical + verification row** `bandit -r 03-development/src/` reports 0 HIGH and 0 MEDIUM issues — decided by `test_bandit_zero_high_medium`, per SPEC.md line 194 + §8 #23.
- **AC-N2.4** **DERIVED: SPEC.md line 193 — chose 'CORS default-deny + allowlist' framing; canonical 'CORS 預設拒絕所有來源; 允許清單由 TASKQ_CORS_ORIGINS 明示'** CORS middleware denies all origins when `TASKQ_CORS_ORIGINS` is empty; allows only the configured list when set — decided by `test_cors_default_deny_and_allowlist_only`, per SPEC.md line 193.
- **AC-N2.5** **DERIVED: SPEC.md line 192 + §8 #6 — chose '403 no existence disclosure' framing; canonical + verification row** 403 response body for `DELETE /v1/tasks/{unknown_id}` with insufficient scope does not disclose whether the id exists — decided by `test_403_no_existence_disclosure`, per SPEC.md line 192 + §8 #6.
- **AC-N2.6** **DERIVED: SPEC.md line 192 + §8 #19 — chose '500 body no internals' framing; canonical + verification row** Triggering a 500 confirms the response body has no stack trace, no SQL fragment, no file path — decided by `test_500_body_no_stack_sql_path`, per SPEC.md line 192 + §8 #19.

**Coverage note (AC-N2.1, AC-N2.2)**: `bandit` only flags high-level Python AST patterns (e.g. `shell=True` in `subprocess.Popen`). The grep ACs above are not duplicates of the bandit AC: bandit may miss dynamically-built substrings or imports via `importlib`, and the grep gate covers those. Treat AC-N2.1/AC-N2.2 as separate test functions, not subsumed by the bandit score.

---

### NFR-03: 錯誤處理、交易與非同步正確性

- **dimension**: `error_handling`
- Each request has an explicit transaction boundary: success commits, exceptions roll back via context manager (FR-06)
- Bare `except:` and `except Exception: pass` are **forbidden**
- **`asyncio.CancelledError` must not be swallowed** — must be re-raised (async-specific swallowing trap)
- DB connection failure → `/readyz` 503 with explicit `detail`; no infinite silent retry
- Task timeout must actually terminate the child process; no orphans (FR-08)
- Migration failure → transaction rollback; DB remains at the prior revision (FR-07)

**Acceptance criteria**:

- **AC-N3.1** **DERIVED: SPEC.md line 200 — chose 'no bare except / no swallow pass' framing; canonical '不得出現裸 except:、except Exception: pass'** Static scan of `03-development/src/` finds no bare `except:` and no `except Exception: pass` — decided by `test_no_bare_except_or_swallow_exception`, per SPEC.md line 200.
- **AC-N3.2** **DERIVED: SPEC.md line 201 + FR-08 — chose 'CancelledError propagates' framing; canonical 'asyncio.CancelledError 不得被吞掉 — 必須重新拋出'** Static scan finds no `except Exception:` that swallows `asyncio.CancelledError`; a unit test triggers `CancelledError` mid-handler and asserts it propagates — decided by `test_cancelled_error_not_swallowed`, per SPEC.md line 201 + FR-08.
- **AC-N3.3** **DERIVED: SPEC.md line 202 + §8 #10 — chose 'readyz 503 no infinite retry' framing; canonical + verification row** With the DB stopped, `GET /readyz` returns 503 with `detail` identifying the DB failure; the readiness checker does not retry indefinitely — decided by `test_readyz_503_db_down_no_infinite_retry`, per SPEC.md line 202 + §8 #10.
- **AC-N3.4** **DERIVED: SPEC.md line 204 + FR-07 — chose 'failed migration rolls back' framing; canonical 'migration 失敗 -> 交易 rollback, 資料庫維持在前一個 revision'** A failing migration rolls back its transaction; subsequent queries show the DB at the prior revision (no partial state) — decided by `test_failed_migration_rolls_back_to_prior_revision`, per SPEC.md line 204 + FR-07.

**Coverage note (AC-N3.2)**: The `ast-error-handling` dimension scores file-level try/except coverage and penalizes `broad_swallow` / `except_base_exception` / `bare_except` anti-patterns. It does **not** specifically detect `except Exception:` that swallows `asyncio.CancelledError` — that anti-pattern needs an explicit runtime test. AC-N3.2 is not subsumed by the dimension score.

---

### NFR-04: 敏感資料遮蔽

- **dimension**: `security`
- `stdout_tail` / `stderr_tail` / logs / error bodies — before persisting or sending, lines matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)` are replaced entirely with `[REDACTED]`
- **Database connection strings** (including passwords) must not appear in any log, error message, or `/v1/metrics` response
- API key plaintext is only printed once at `key create`; never written to any persistent location

**Acceptance criteria**:

- **AC-N4.1** **DERIVED: SPEC.md line 210 — chose 'all four patterns redacted' framing; canonical regex '(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)'** A fixture logs an `sk-...` key (≥8 chars), a `token=...` fragment, a `Bearer ...` header, and a `postgres://user:pass@host/db` URL; each is replaced with `[REDACTED]` in `stdout_tail` / `stderr_tail` / log lines — decided by `test_redact_sk_token_bearer_pg_url`, per SPEC.md line 210.
- **AC-N4.2** **DERIVED: SPEC.md line 211 + §8 #20 — chose 'DB URL password not leaked' framing; canonical + verification row** `TASKQ_DB_URL` containing a password does not appear in any log line, error message, or `/v1/metrics` response — decided by `test_db_url_password_not_leaked_to_log_or_metrics`, per SPEC.md line 211 + §8 #20.

**Coverage note (AC-N4.1)**: bandit/semgrep cannot detect runtime redaction; AC-N4.1 is a runtime-only check.

---

### NFR-05: 文件覆蓋

- **dimension**: `documentation`
- Every public function/class has a docstring containing a `[FR-XX]` or `[NFR-XX]` reference; coverage **100%**
- Each API endpoint has a `summary` and `description` in the OpenAPI schema (FastAPI's auto-generated `/openapi.json` asserted by test)

**Acceptance criteria**:

- **AC-N5.1** **DERIVED: SPEC.md line 217 — chose 'all public symbols carry [FR-XX]/[NFR-XX]' framing; canonical '全部公開函式/類別有 docstring 且含 [FR-XX] 或 [NFR-XX] 引用, 覆蓋率 100%'** Static scan of `03-development/src/` reports 100% of public `def`/`class` symbols carry a docstring that mentions at least one `[FR-XX]` or `[NFR-XX]` tag — decided by `test_docstrings_cover_all_public_symbols_with_fr_nfr_tags`, per SPEC.md line 217.
- **AC-N5.2** **DERIVED: SPEC.md line 218 — chose 'openapi.json summary + description' framing; canonical '每個 API 端點在 OpenAPI schema 中有 summary 與 description'** `/openapi.json` lists every `/v1/*` endpoint with a non-empty `summary` and `description` — decided by `test_openapi_schema_summary_description_present`, per SPEC.md line 218.

**Coverage note (AC-N5.1)**: The `ast-docstrings` dimension counts public docstring coverage but does not enforce that the docstring references a `[FR-XX]` / `[NFR-XX]` tag. AC-N5.1 is not subsumed by the dimension score.

---

### NFR-06: 架構分層契約

- **dimension**: `architecture_constraints`
- Project root **must contain `.importlinter`** declaring the layers contract:
  ```
  api > service > repository > models
  ```
  Upper layers may import lower; lower layers must not import upper; `config` and `errors` are independent
- **Additional forbidden contract**: any layer other than `repository` **must not import `sqlalchemy`** — ORM leakage into business code is the specific anti-pattern this round guards
- `lint-imports` must **exit 0**
- Removing `.importlinter`, blanket `ignore_imports`, or downgrading the contract to gain a pass is forbidden

**Acceptance criteria**:

- **AC-N6.1** **DERIVED: SPEC.md line 224 — chose 'importlinter config + layers' framing; canonical 'api > service > repository > models'** `.importlinter` exists at the project root and declares `api > service > repository > models` — decided by `test_importlinter_config_present_with_layers`, per SPEC.md line 224.
- **AC-N6.2** **DERIVED: SPEC.md line 231 + §8 #21 — chose 'lint-imports exit 0' framing; canonical + verification row** `lint-imports` exits 0 — decided by `test_lint_imports_exit_zero`, per SPEC.md line 231 + §8 #21.
- **AC-N6.3** **DERIVED: SPEC.md line 230 + §8 #21 — chose 'sqlalchemy import blocked in service/api' framing; canonical 'repository 以外的任何層不得 import sqlalchemy'** A `service` or `api` module that imports `sqlalchemy` is rejected by `lint-imports` (forbidden contract) — decided by `test_sqlalchemy_import_in_service_or_api_blocked`, per SPEC.md line 230 + §8 #21.
- **AC-N6.4** **DERIVED: SPEC.md line 232 — chose 'no bypass via delete/ignore/downgrade' framing; canonical '禁止以刪除 .importlinter、萬用字元 ignore_imports、或降級 contract 的方式取得通過'** `.importlinter` is not deleted, does not use blanket `ignore_imports`, and is not downgraded to bypass the contract — decided by `test_no_bypass_of_importlinter_contract`, per SPEC.md line 232.

---

### NFR-07: 依賴與授權合規

- **dimension**: `license_compliance`
- All runtime dependencies in `requirements.txt` are pinned with `==`; transitive dependencies are fully locked in `requirements.lock`
- Allowed licenses: MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0 / PSF; any other → dependency must not be used
- **Scan scope must include the full dependency tree** (direct + transitive); evidence command: `pip-licenses --format=json --with-system`
- SBOM produced at `08-config/SBOM.json` with each dependency's `name` / `version` / `license` / `direct|transitive`

**Acceptance criteria**:

- **AC-N7.1** **DERIVED: SPEC.md line 237 — chose 'requirements.txt == pinning' framing; canonical '全部 runtime 依賴在 requirements.txt 以 == 釘版'** Every entry in `requirements.txt` uses `==` pinning — decided by `test_requirements_txt_all_pinned_with_equals_equals`, per SPEC.md line 237.
- **AC-N7.2** **DERIVED: SPEC.md lines 238–239 + §8 #22 — chose 'pip-licenses full-tree allowlist' framing; canonical + verification row** `pip-licenses --format=json --with-system` lists every dependency (direct + transitive); every license is in the allowlist {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF} — decided by `test_pip_licenses_full_tree_allows_only_allowlist`, per SPEC.md lines 238–239 + §8 #22.
- **AC-N7.3** **DERIVED: SPEC.md line 237 — chose 'requirements.lock transitive' framing; canonical 'transitive 依賴以 lock 檔完整鎖定'** `requirements.lock` exists and locks every transitive dependency — decided by `test_requirements_lock_present_and_complete`, per SPEC.md line 237.
- **AC-N7.4** **DERIVED: SPEC.md line 240 — chose 'SBOM fields per dependency' framing; canonical '產出 SBOM ... 含 name / version / license / direct|transitive'** `08-config/SBOM.json` exists with `name` / `version` / `license` / `direct|transitive` for every dependency — decided by `test_sbom_json_has_required_fields_per_dependency`, per SPEC.md line 240.

**Coverage note (AC-N7.2, AC-N7.4)**: The `license_compliance` dimension uses scancode over `src/`, not pip-licenses over the dependency tree. AC-N7.2/AC-N7.4 require their own dedicated tests (pip-licenses run + SBOM schema assertion) and are not subsumed by the scancode dimension score.

---

### NFR-08: 變異測試

- **dimension**: `mutation_testing`
- `.methodology/harness_config.json` sets `features.mutation_testing: true`
- **mutation score ≥ 70**
- Scope limited to `service/` and `repository/` layers, with the rationale recorded in `harness_config.json` (execution-time budget)

**Acceptance criteria**:

- **AC-N8.1** **DERIVED: SPEC.md line 245 — chose 'harness_config mutation_testing enabled' framing; canonical '.methodology/harness_config.json 設 features.mutation_testing: true'** `.methodology/harness_config.json` contains `features.mutation_testing: true` — decided by `test_harness_config_mutation_testing_enabled`, per SPEC.md line 245.
- **AC-N8.2** **DERIVED: SPEC.md line 246 + §8 #24 — chose 'mutmut score >= 70' framing; canonical + verification row** `mutmut run` followed by `mutmut results` reports a mutation score **≥ 70** for the `service/` + `repository/` layers — decided by `test_mutation_score_at_least_70`, per SPEC.md line 246 + §8 #24.
- **AC-N8.3** **DERIVED: SPEC.md line 326 + §10 — chose 'crg_cohesion_healthy not lowered' framing; canonical '不得調降 crg_cohesion_healthy'** `.methodology/harness_config.json` does not lower `crg_cohesion_healthy` below the framework default — decided by `test_crg_cohesion_healthy_not_lowered`, per SPEC.md line 326 + §10.

---

### NFR-09: 驗證真實性 (零 skip 鐵律)

- **dimension**: `test_assertion_quality`
- **No FR/NFR verification test may be `pytest.skip` / `skipif` / `xfail` / an unasserted stub**
- `pytest 03-development/tests -q` **skipped count must be 0**
- Every test function has at least one `assert` (`zero_assert == 0`)
- **Anti-fake clause**: must not exclude tests via `--ignore` / `-k` / `--deselect` / `collect_ignore` / removing directories from `testpaths`
- **Round-2 special clause**: FR-07's three-step migration must be tested against a **real database** (SQLite file, not in-memory mock); round-trip reversibility verified by actual data comparison. **Must not** be downgraded to skip on the grounds that "migration logic is hard to test" — this is precisely the failure mode of the prior two rounds
- `TRACEABILITY_MATRIX.md`'s `VERIFIED` is only set after a test actually runs and passes

**Acceptance criteria**:

- **AC-N9.1** **DERIVED: SPEC.md line 253 + §8 #1 — chose 'pytest 0 skipped' framing; canonical + verification row** `pytest 03-development/tests -q` exits 0 with `0 skipped` in its summary — decided by `test_pytest_zero_skipped`, per SPEC.md line 253 + §8 #1.
- **AC-N9.2** **DERIVED: SPEC.md line 254 — chose 'zero test functions with zero asserts' framing; canonical '每個測試函式至少一個 assert (zero_assert == 0)'** Static AST scan reports zero test functions with zero assertions (`zero_assert == 0`) — decided by `test_zero_assert_count_is_zero`, per SPEC.md line 254.
- **AC-N9.3** **DERIVED: SPEC.md line 255 — chose 'no test exclusion via config' framing; canonical '不得以 --ignore / -k / --deselect / collect_ignore 排除測試'** Project pytest configuration does not exclude tests via `--ignore` / `-k` / `--deselect` / `collect_ignore` / removing directories from `testpaths` — decided by `test_no_test_exclusion_via_config`, per SPEC.md line 255.
- **AC-N9.4** **DERIVED: SPEC.md line 256 + FR-07 — chose 'real SQLite file migration test' framing; canonical '三步 migration 必須以真實資料庫測試 (SQLite 檔案)'** FR-07's three-step migration round-trip is tested against a real SQLite file (not in-memory mock); the test would fail if the data migration were skipped — decided by `test_migration_round_trip_uses_real_sqlite_file`, per SPEC.md line 256 + FR-07.
- **AC-N9.5** **DERIVED: SPEC.md line 257 — chose 'TRACEABILITY VERIFIED only after test passes' framing; canonical 'TRACEABILITY_MATRIX.md 的 VERIFIED 只能在測試實際執行並通過時給出'** `TRACEABILITY_MATRIX.md` rows marked `VERIFIED` only after the corresponding test ran and passed; audit log present — decided by `test_traceability_verified_only_after_test_passes`, per SPEC.md line 257.

---

### NFR-10: 整合覆蓋

- **dimension**: `integration_coverage`
- `03-development/tests/integration/` line coverage **≥ 80%**
- Integration tests use `httpx.AsyncClient(transport=ASGITransport(app))`; **must not call handler functions directly**
- Coverage at minimum: full CRUD chain, one example each of 401/403/404/409/422/429/503, migration round-trip, rate-limit trigger and recovery, graceful drain

**Acceptance criteria**:

- **AC-N10.1** **DERIVED: SPEC.md line 262 + §8 #3 — chose 'integration coverage >= 80%' framing; canonical + verification row** `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` reports TOTAL **≥ 80%** — decided by `test_integration_coverage_at_least_80_percent`, per SPEC.md line 262 + §8 #3.
- **AC-N10.2** **DERIVED: SPEC.md line 263 — chose 'ASGITransport-only' framing; canonical '整合測試以 httpx.AsyncClient(transport=ASGITransport(app)) 驅動, 不得直接呼叫 handler 函式'** Integration tests drive the API exclusively via `httpx.AsyncClient(transport=ASGITransport(app))`; no integration test imports a handler function directly — decided by `test_integration_uses_asgi_transport_only`, per SPEC.md line 263.
- **AC-N10.3** **DERIVED: SPEC.md line 264 — chose 'required scenarios enumerated' framing; canonical '至少涵蓋: CRUD 全鏈、401/403/404/409/422/429/503 各一例、migration 往返、rate limit 觸發與恢復、graceful drain'** Integration suite includes at least one example of each status code 401 / 403 / 404 / 409 / 422 / 429 / 503, plus a migration round-trip test, a rate-limit trigger and recovery test, and a graceful-drain test — decided by `test_integration_covers_required_scenarios`, per SPEC.md line 264.

---

### NFR-11: 可讀性

- **dimension**: `readability`
- Project MI (LLOC-weighted) **≥ 80**; per-function CC **≤ 10**
- Single file ≤ 400 lines; single directory ≤ 15 files
- Each API handler ≤ 40 lines (business logic must descend to `service/`)

**Acceptance criteria**:

- **AC-N11.1** **DERIVED: SPEC.md line 269 + §11 — chose 'MI >= 80' framing; canonical + monitoring threshold** `radon mi` (LLOC-weighted) over `03-development/src/` reports average MI **≥ 80** — decided by `test_mi_average_at_least_80`, per SPEC.md line 269 + §11.
- **AC-N11.2** **DERIVED: SPEC.md line 269 — chose 'CC <= 10' framing; canonical '單一函式 CC <= 10'** Per-function cyclomatic complexity is **≤ 10** across the source tree — decided by `test_cyclomatic_complexity_per_function_at_most_10`, per SPEC.md line 269.
- **AC-N11.3** **DERIVED: SPEC.md line 270 — chose 'file <= 400 LOC, dir <= 15 files' framing; canonical + monitoring threshold** No file in `03-development/src/` exceeds 400 lines; no directory exceeds 15 files — decided by `test_file_and_directory_size_limits`, per SPEC.md line 270.
- **AC-N11.4** **DERIVED: SPEC.md line 271 — chose 'handler <= 40 lines' framing; canonical '每個 API handler <= 40 行 (業務邏輯必須下沉到 service/)'** No API handler under `api/` exceeds 40 lines (business logic must descend to `service/`) — decided by `test_api_handlers_under_40_lines`, per SPEC.md line 271.

---

### NFR-12: 系統驗證目標

- **dimension**: `execute_verification_target`
- The `Makefile` `verify-system` target chains:
  1. `alembic upgrade head`
  2. full test suite
  3. service startup + `/healthz`, `/readyz` smoke
  4. `alembic downgrade base` then `upgrade head` (round-trip)
- `make verify-system` must **exit 0** and print `verify-system: PASS` on stdout

**Acceptance criteria**:

- **AC-N12.1** **DERIVED: SPEC.md lines 277–280 — chose 'verify-system chains four steps' framing; canonical 4-step list** `Makefile` contains a `verify-system` target chaining `alembic upgrade head` → full test suite → service startup + `/healthz`/`/readyz` smoke → `alembic downgrade base` → `alembic upgrade head` — decided by `test_makefile_verify_system_target_chains_steps`, per SPEC.md lines 277–280.
- **AC-N12.2** **DERIVED: SPEC.md line 281 + §8 #27 — chose 'exit 0 + PASS message' framing; canonical + verification row** `make verify-system` exits 0 and prints `verify-system: PASS` on stdout — decided by `test_make_verify_system_exit_zero_and_passes_message`, per SPEC.md line 281 + §8 #27.

---

## 5. Acceptance Criteria Summary

The full AC table is enumerated across §3 and §4. Mapping of SPEC.md §8 verification rows to AC identifiers:

| SPEC §8 # | Command | Expected | Mapped AC |
|---|---|---|---|
| 1 | `pytest 03-development/tests -q` | all green, skipped == 0 | AC-N9.1 |
| 2 | `pytest --cov=03-development/src --cov-report=term` | TOTAL 100% | AC-N1.1, AC-N1.2, AC-N11.x (overall coverage target) |
| 3 | `pytest tests/integration --cov=...` | TOTAL ≥ 80% | AC-N10.1 |
| 4 | `POST /v1/tasks` (write key) | 201 + id | AC-1.1 |
| 5 | `POST /v1/tasks` (no X-API-Key) | 401 + problem+json | AC-1.2, AC-3.1 |
| 6 | `DELETE /v1/tasks/{id}` (write, non-admin) | 403, no existence leak | AC-1.9, AC-4.2, AC-N2.5 |
| 7 | `GET /v1/tasks/{unknown}` | 404 + problem+json | AC-1.6 |
| 8 | `POST /v1/tasks` duplicate name | 409 | AC-1.4 |
| 9 | Burst > `TASKQ_RATE_BURST` | 429 + Retry-After | AC-5.1 |
| 10 | DB down → `/readyz` | 503, DB-failure detail | AC-9.3, AC-N3.3 |
| 11 | `alembic downgrade -1` → `/readyz` | 503, migration detail | AC-9.4 |
| 12 | round-trip migration | sample bytewise identical | AC-7.5 |
| 13 | `alembic downgrade base` | exit 0, no residual tables | AC-7.2 |
| 14 | SQL statement count | constant | AC-N1.3 |
| 15 | `GET /v1/tasks/{id}` p95 (10k) | < 30ms | AC-N1.1 |
| 16 | grep shell/eval/exec | 0 hits | AC-N2.1 |
| 17 | SQL string concat scan | 0 hits | AC-N2.2, AC-6.3 |
| 18 | `api_keys` table | no plaintext | AC-3.2 |
| 19 | 500 detail check | no internals | AC-10.3, AC-N2.6 |
| 20 | DB URL leak check | none | AC-N4.2 |
| 21 | `lint-imports` | exit 0, sqlalchemy blocked | AC-N6.2, AC-N6.3 |
| 22 | `pip-licenses` | allowlist only | AC-N7.2 |
| 23 | `bandit -r ...` | 0 HIGH, 0 MEDIUM | AC-N2.3 |
| 24 | `mutmut run` results | mutation ≥ 70 | AC-N8.2 |
| 25 | shutdown with in-flight | graceful drain | AC-8.2 |
| 26 | `grep -c "^TASKQ_" .env.example` | 12 | (env parity, §5.1) |
| 27 | `make verify-system` | exit 0 + `verify-system: PASS` | AC-N12.2 |

§8 #26 (`.env.example` line count == 12) is verified by static file inspection rather than a runtime test; see §5.1 below.

---

## 6. Out-of-Scope

The following are explicitly out of round-2 scope (carried from `taskq-plus`, not added by SPEC.md):

- Distributed consensus / leader election
- Multi-region replication
- Fine-grained RBAC beyond `read` / `write` / `admin` (e.g. per-resource ACLs)
- Web UI / dashboard
- Webhooks / push notifications
- gRPC / non-HTTP transports

If any of these are introduced, they must be added as new FRs/NFRs in a future spec revision, not slipped in via implementation.

---

## 7. Open Issues

Items deferred from SPEC.md or surfaced as known ambiguities to be resolved by downstream phases:

### Deferred canonical items (FR/NFR with no implementation yet)

- **NFR-99: Resolve SPEC.md §5.1 `.env.example` parity check (line 326 + §8 #26).** The canonical spec states `grep -c "^TASKQ_" .env.example == 12`. This SRS treats it as a static file-existence check, not as a runtime AC; Phase 3+ should add a CI assertion if dynamic verification is required.
- **NFR-99: Resolve SPEC.md §10 "framework 對齊" — async coverage by ast-error-handling / ast-assertions.** SPEC.md line 429 notes: *"async 為本輪新變數... 框架的 `ast-error-handling` / `ast-assertions` 掃描器過去只面對過同步程式碼。若它們在 async 語法上出現誤判或漏判,那本身就是本輪測床要交付的發現 —— 應記入 Phase 4 的 bug hunt,不得靜默繞過。"* Phase 4 bug hunt must surface any false negatives or false positives in these scanners against async code; do not paper over them.

### Prompt-injection / suspicious pattern scan outcome (one-line summary)

The SPEC.md scan for prompt-injection patterns / suspicious clauses (Round-3 anti-over-spec tooling) found no actionable injection patterns in the canonical text; all 10 FRs and 12 NFRs were transcribed verbatim. Detail is omitted here per R-NO-PRESCRIPTION-001 (SRS §8 may reference scan outcome as a one-line summary only).

---

## 8. Risks

Risks transcribed from SPEC.md §9:

| ID | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | **v3 data migration loses data** | High | Medium | Round-trip reversibility tested with real DB, column-by-column (FR-07 / §8 #12) |
| R2 | SQL injection | High | Low | String concat ban + ORM/parameterized + grep gate (NFR-02) |
| R3 | API key leak | High | Medium | Hashed storage + constant-time compare + plaintext printed once (FR-03) |
| R4 | 403 leaks resource existence | Medium | Medium | Authorization decision before resource lookup (FR-04 / §8 #6) |
| R5 | N+1 query crashes on large tables | High | High | Explicit eager load + SQL count assertion (NFR-01 / §8 #14) |
| R6 | Error body leaks internal structure | Medium | High | RFC 7807 fixed fields + `detail` whitelist (FR-10) |
| R7 | **`CancelledError` swallowed → shutdown deadlock** | Medium | Medium | Plain-language ban + test assertion (NFR-03) |
| R8 | Task timeout leaves orphan processes | Medium | Medium | `kill()` + `await wait()` (FR-08 / §8 #25) |
| R9 | Deploy forgot to run migration | High | Medium | `/readyz` fail-closed (FR-09 / §8 #11) |
| R10 | Connection pool exhaustion | Medium | Medium | `pool_pre_ping` + concurrency cap (FR-06/08) |
| R11 | Transitive dependency introduces incompatible license | Medium | Medium | Lock file + full-tree scan (NFR-07) |
| R12 | Rate-bucket race over-allowance | Low | Medium | Single transaction + row-level lock (FR-05) |

---

## 9. Glossary

| Term | Definition |
|---|---|
| ASGI | Asynchronous Server Gateway Interface — Python async web server protocol (used by FastAPI / uvicorn). |
| Alembic | SQLAlchemy's database schema migration tool. The round-2 project uses three Alembic revisions (v1/v2/v3). |
| `application/problem+json` | RFC 7807 content type used for non-2xx HTTP error bodies. |
| `asyncio.TaskGroup` | Python 3.11+ structured concurrency primitive for grouping and awaiting background tasks. |
| `crg_cohesion_healthy` | Framework calibration threshold for the `architecture` dimension. NFR-08 forbids lowering it below the framework default. |
| Cursor-based pagination | Pagination via a stable pointer rather than offset; avoids large-table scans. Required by FR-01. |
| `hmac.compare_digest` | Constant-time string comparison function; required by FR-03 for API-key comparison. |
| `import-linter` | Python static-layer-contract tool. NFR-06 enforces the `api > service > repository > models` layers and the `sqlalchemy`-forbidden contract. |
| N+1 | Anti-pattern where a query triggers a number of follow-up queries proportional to returned rows. NFR-01 makes it a hard failure. |
| `problem+json` | Short for `application/problem+json` (RFC 7807). |
| Round-2 testbed | This project's role in harness-methodology's progressive verification, the second of three rounds. |
| RFC 7807 | "Problem Details for HTTP APIs" — the error-body schema this project adopts (FR-10). |
| SBOM | Software Bill of Materials; NFR-07 requires `08-config/SBOM.json`. |
| Single Source of Truth | SPEC.md is the only authoritative spec; SRS.md is a transcription, not a reinterpretation. |
| Token bucket | Rate-limiting algorithm with capacity + refill rate; FR-05 implements this per API key. |

---

## FR Block (machine-readable)

The block below is the machine-readable form of every FR and NFR heading above. Downstream artifacts (`check-spec-alignment`, `scripts/plangen/artifact_parsers.srs_machine_block`, P2 SAB generator) reject any SRS missing this block.

### NFR `type` ↔ §4 `dimension` ↔ `SPEC_TRACKING.md` Intent Class synonym map

> **Downstream dim-mapper routing contract.** The NFR `type` field in the FR Block (this section, machine-readable) is the canonical label for routing to `evaluate_dimension.md` headings. The §4 `dimension` field and `SPEC_TRACKING.md` `Intent Class` are human-readable aliases that downstream consumers MUST treat as equivalent to the canonical `type`. The map below is the explicit synonym table; if a future NFR diverges from this map, add it here and update all three label-sets in lockstep.

| FR Block `type` (canonical) | §4 `dimension` body | `SPEC_TRACKING.md` Intent Class | evaluate_dimension.md heading | NFRs |
|---|---|---|---|---|
| `performance` | `performance` | `performance` | `performance` | NFR-01 |
| `security` | `security` | `security` / `security / redaction` | `security` | NFR-02, NFR-04 |
| `documentation` | `documentation` | `documentation` | `documentation` | NFR-05 |
| `reliability` | `error_handling` | `error_handling / reliability` | `error_handling` | NFR-03 |
| `layering` | `architecture_constraints` | `architecture_constraints` | `architecture_constraints` | NFR-06 |
| `licensing` | `license_compliance` | `license_compliance` | `license_compliance` | NFR-07 |
| `mutation` | `mutation_testing` | `mutation_testing` | `mutation_testing` | NFR-08 |
| `testability` | `test_assertion_quality` | `test_assertion_quality` | `test_assertion_quality` | NFR-09 |
| `integration` | `integration_coverage` | `integration_coverage` | `integration_coverage` | NFR-10 |
| `maintainability` | `readability` | `readability` | `readability` | NFR-11 |
| `verifiability` | `execute_verification_target` | `execute_verification_target` | `execute_verification_target` | NFR-12 |

<!-- FR:START -->
```json
{
  "version": "1.0",
  "created_at": "2026-09-05",
  "phase": 1,
  "project": "taskq-api",
  "functional_requirements": [
    {
      "id": "FR-01",
      "description": "Task resource CRUD API: POST /v1/tasks (write), GET /v1/tasks/{id} (read), GET /v1/tasks (read, cursor-paginated), DELETE /v1/tasks/{id} (admin). Validation non-empty/<=1000 chars/injection denylist/unique name -> 422; unknown id -> 404; list limit default 50, cap 200.",
      "implementation_functions": ["taskq_api.api.v1.tasks.create_task", "taskq_api.api.v1.tasks.get_task", "taskq_api.api.v1.tasks.list_tasks", "taskq_api.api.v1.tasks.delete_task"],
      "verification_method": "integration test via httpx.AsyncClient(ASGITransport(app)) asserting 2xx/4xx/5xx per AC-1.1..AC-1.9"
    },
    {
      "id": "FR-02",
      "description": "Task execution endpoint: POST /v1/tasks/{id}/run (write) -> 202 with run_id; runs asyncio.create_subprocess_exec(*shlex.split(command)) with shell=True forbidden; timeout = TASKQ_TASK_TIMEOUT; state machine pending -> running -> done|failed|timeout; results in task_results; GET /v1/tasks/{id}/runs (read) returns history newest-first.",
      "implementation_functions": ["taskq_api.api.v1.tasks.run_task", "taskq_api.api.v1.tasks.list_runs", "taskq_api.service.runner.run_subprocess", "taskq_api.repository.task_results.insert"],
      "verification_method": "integration test asserting AC-2.1..AC-2.6; subprocess isolation via tmp_path; SQLAlchemy event listener for no-shell-true"
    },
    {
      "id": "FR-03",
      "description": "API Key authentication: all /v1/* require X-API-Key (401 otherwise); keys stored as SHA-256 hashes; comparison via hmac.compare_digest; keys created via python -m taskq_api key create --scope <scope>, plaintext printed once; revoked_at non-null keys rejected; /healthz and /readyz exempt.",
      "implementation_functions": ["taskq_api.service.auth.authenticate_api_key", "taskq_api.repository.api_keys.lookup_by_hash", "taskq_api.cli.key_create"],
      "verification_method": "unit + integration tests asserting AC-3.1..AC-3.6; constant-time compare verified by call-graph inspection"
    },
    {
      "id": "FR-04",
      "description": "Scope authorization: per-key scope read<write<admin (hierarchical); insufficient -> 403 + problem+json with body not disclosing resource existence; single dependency enforces authorization for every /v1 route.",
      "implementation_functions": ["taskq_api.service.auth.require_scope", "taskq_api.dependencies.scope_guard"],
      "verification_method": "integration tests asserting AC-4.1..AC-4.3; dependency shared across all /v1 routes asserted via route introspection"
    },
    {
      "id": "FR-05",
      "description": "Rate limit: per-token token bucket (capacity TASKQ_RATE_BURST, refill TASKQ_RATE_PER_SEC); exceeded -> 429 + problem+json + Retry-After; bucket state in DB with row-level lock in single transaction; /healthz and /readyz exempt.",
      "implementation_functions": ["taskq_api.service.rate_limit.consume_token", "taskq_api.repository.rate_buckets.consume"],
      "verification_method": "integration tests asserting AC-5.1..AC-5.3; burst recovery asserted via Retry-After header"
    },
    {
      "id": "FR-06",
      "description": "Persistence layer + transaction boundaries: all data access via repository/; business layer does not hold Session; one Session per request with explicit commit/rollback via context manager; SQL string concat forbidden; associations eager-loaded via selectinload/joinedload (no N+1); pool_size=TASKQ_DB_POOL_SIZE with pool_pre_ping=True.",
      "implementation_functions": ["taskq_api.repository.session.request_session", "taskq_api.repository.base.BaseRepository"],
      "verification_method": "integration tests + grep gate asserting AC-6.1..AC-6.5; SQLAlchemy event listener asserting constant statement count"
    },
    {
      "id": "FR-07",
      "description": "Alembic three-step evolution: v1 creates tasks+api_keys; v2 adds tags+task_tags + tasks.name unique index; v3 splits tasks.result_json into task_results with data migration; every step has working downgrade; upgrade head / downgrade base both succeed; round-trip reversibility preserves sample data bytewise; no op.execute DROP TABLE shortcuts.",
      "implementation_functions": ["migrations.versions.v1_initial", "migrations.versions.v2_tags", "migrations.versions.v3_split_results"],
      "verification_method": "real SQLite-file migration round-trip test asserting AC-7.1..AC-7.7; offline SQL generation asserted"
    },
    {
      "id": "FR-08",
      "description": "Async runner: asyncio.TaskGroup manages background tasks; graceful drain on shutdown up to TASKQ_DRAIN_TIMEOUT (overflow -> interrupted); concurrency cap TASKQ_MAX_CONCURRENT with queueing; timeout via asyncio.wait_for killing subprocess via process.kill() + await process.wait(); asyncio.CancelledError propagates (not swallowed by except Exception).",
      "implementation_functions": ["taskq_api.service.runner.Runner", "taskq_api.service.runner.TaskGroupPool"],
      "verification_method": "integration tests asserting AC-8.1..AC-8.4; subprocess orphan detection via psutil"
    },
    {
      "id": "FR-09",
      "description": "Health checks + observability: GET /healthz (no auth) -> 200 {status:ok}; GET /readyz (no auth) -> 200 iff DB reachable AND alembic current == head else 503 with detail naming the failed check (fail-closed on migration lag); GET /v1/metrics (admin) -> task counts by status, latency percentiles, rate-limit rejection counts.",
      "implementation_functions": ["taskq_api.api.health.healthz", "taskq_api.api.health.readyz", "taskq_api.api.v1.metrics.metrics_view"],
      "verification_method": "integration tests asserting AC-9.1..AC-9.5; readiness tested with DB-down and downgrade scenarios"
    },
    {
      "id": "FR-10",
      "description": "RFC 7807 error contract: all non-2xx Content-Type = application/problem+json; body fields type/title/status/detail/instance/correlation_id; detail must not leak stack/SQL/path/schema; correlation_id echoed in X-Correlation-Id header and server log; mapping 422/401/403/404/409/429/503/500.",
      "implementation_functions": ["taskq_api.errors.problem_json", "taskq_api.middleware.correlation_id"],
      "verification_method": "integration tests asserting AC-10.1..AC-10.5; 500-detail internals scan by string contains"
    }
  ],
  "non_functional_requirements": [
    {
      "id": "NFR-01",
      "type": "performance",
      "description": "GET /v1/tasks/{id} p95 < 30ms (10k records, ASGI); GET /v1/tasks?limit=50 p95 < 80ms; list endpoints issue constant SQL count (no N+1, asserted via SQLAlchemy event listener); measured by pytest-benchmark.",
      "test_method": "pytest-benchmark on seeded 10k dataset; event-listener statement counter asserts constant SQL count (AC-N1.1..AC-N1.3)"
    },
    {
      "id": "NFR-02",
      "type": "security",
      "description": "No shell=True / eval( / exec( in source (grep gate); no f-string / % / + SQL composition (grep + review); API keys hashed + hmac.compare_digest (FR-03); 403 responses do not leak existence (FR-04); error bodies contain no stack/SQL/path (FR-10); CORS default-deny with TASKQ_CORS_ORIGINS allowlist; bandit -r src/ 0 HIGH 0 MEDIUM.",
      "test_method": "grep + bandit CI gate; CORS allowlist unit test; 403 body content test; 500-detail internals test (AC-N2.1..AC-N2.6)"
    },
    {
      "id": "NFR-03",
      "type": "reliability",
      "description": "Explicit transaction boundary per request (commit/rollback via context manager, FR-06); no bare except: and no except Exception: pass; asyncio.CancelledError must re-raise; DB connection failure -> /readyz 503 with explicit detail (no infinite silent retry); task timeout terminates child process (FR-08); migration failure rolls back (FR-07).",
      "test_method": "ast scan + integration tests for cancelled-error propagation; readiness test with DB stopped; migration rollback test (AC-N3.1..AC-N3.4)"
    },
    {
      "id": "NFR-04",
      "type": "security",
      "description": "Lines matching (sk-[A-Za-z0-9_-]{8,}|token=\\S+|Bearer\\s+\\S+|postgres(ql)?://[^\\s]+) replaced with [REDACTED] in stdout_tail / stderr_tail / logs / error bodies; DB connection string (with password) never appears in logs / error messages / /v1/metrics; API key plaintext only printed once at key create.",
      "test_method": "fixture logs all four patterns and asserts each replaced with [REDACTED]; log/metrics scan asserts no password fragment (AC-N4.1..AC-N4.2)"
    },
    {
      "id": "NFR-05",
      "type": "documentation",
      "description": "100% docstring coverage on public def/class with [FR-XX] or [NFR-XX] tag; each /v1 endpoint has summary + description in OpenAPI (/openapi.json).",
      "test_method": "ast-docstrings scan with regex for FR/NFR tags; OpenAPI schema assertion via httpx GET /openapi.json (AC-N5.1..AC-N5.2)"
    },
    {
      "id": "NFR-06",
      "type": "layering",
      "description": ".importlinter declares api > service > repository > models; config + errors independent; additional forbidden contract: any layer other than repository must not import sqlalchemy; lint-imports exits 0; no bypassing via delete / ignore_imports / contract downgrade.",
      "test_method": "lint-imports CLI exit code; sqlalchemy-import-in-service-or-api unit test (AC-N6.1..AC-N6.4)"
    },
    {
      "id": "NFR-07",
      "type": "licensing",
      "description": "All runtime deps pinned with == in requirements.txt; transitive deps fully locked in requirements.lock; allowed licenses {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF}; scan full dependency tree (direct + transitive) via pip-licenses --format=json --with-system; SBOM at 08-config/SBOM.json with name/version/license/direct|transitive.",
      "test_method": "pip-licenses --with-system exit + allowlist assertion; SBOM schema test; lock-file completeness test (AC-N7.1..AC-N7.4)"
    },
    {
      "id": "NFR-08",
      "type": "mutation",
      "description": "harness_config.json sets features.mutation_testing: true; mutation score >= 70; scope limited to service/ + repository/ with rationale recorded; crg_cohesion_healthy must not be lowered.",
      "test_method": "harness_cli.py mutation-test-score exit + score >= 70; harness_config.json field assertions (AC-N8.1..AC-N8.3)"
    },
    {
      "id": "NFR-09",
      "type": "testability",
      "description": "No pytest.skip / skipif / xfail / unasserted stub; pytest -q skipped == 0; zero test functions with zero asserts; no test exclusion via --ignore / -k / --deselect / collect_ignore / removed testpaths; FR-07 migration tested on real SQLite file (not in-memory); TRACEABILITY_MATRIX VERIFIED only after test passes.",
      "test_method": "pytest -q summary line; ast-assertions scan; pytest config scan for forbidden exclusions; real-file migration test (AC-N9.1..AC-N9.5)"
    },
    {
      "id": "NFR-10",
      "type": "integration",
      "description": "tests/integration/ line coverage >= 80%; integration tests drive API exclusively via httpx.AsyncClient(ASGITransport(app)); covers full CRUD chain + each error code (401/403/404/409/422/429/503) + migration round-trip + rate-limit trigger/recovery + graceful drain.",
      "test_method": "pytest tests/integration --cov=src --cov-report=term; ASGITransport-only scan; scenario-coverage checklist test (AC-N10.1..AC-N10.3)"
    },
    {
      "id": "NFR-11",
      "type": "maintainability",
      "description": "MI (LLOC-weighted) >= 80; per-function CC <= 10; single file <= 400 lines; single directory <= 15 files; API handler <= 40 lines (business logic in service/).",
      "test_method": "radon mi aggregate; radon cc per function; line-count assertions on src tree (AC-N11.1..AC-N11.4)"
    },
    {
      "id": "NFR-12",
      "type": "verifiability",
      "description": "Makefile verify-system chains: alembic upgrade head -> full test suite -> service startup + /healthz+/readyz smoke -> alembic downgrade base -> alembic upgrade head; exit 0 + stdout 'verify-system: PASS'.",
      "test_method": "make verify-system exit code + stdout regex match (AC-N12.1..AC-N12.2)"
    }
  ]
}
```
<!-- FR:END -->
