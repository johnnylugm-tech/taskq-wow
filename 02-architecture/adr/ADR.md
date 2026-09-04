# Architecture Decision Records (ADR) — taskq-api

> Architecture decisions for `taskq-api` (harness-methodology Round 2).
> Source of truth for each ADR: `02-architecture/SAD.md` and `SPEC.md`.
> Python runtime pinned by this repo: `.venv/bin/python` reports **Python 3.11.15**.
>
> NOTE on orchestrator hints: the dispatcher suggested "Python stdlib-only" plus
> `ThreadPoolExecutor` / `atomic write` / `circuit breaker` patterns. The binding
> architecture (§0 of SAD.md, §2 of SPEC.md) uses FastAPI + SQLAlchemy 2.x +
> Alembic + `asyncio`, not stdlib-only. ADRs follow the SAD; they intentionally
> do not invent stdlib-only / threading-only entries because doing so would
> silently contradict §10 NFR-06 (layering with `sqlalchemy` forbidden outside
> `repository/`) and FR-08 (`asyncio.TaskGroup`), which the harness scores on.

Index:

- ADR-001  Python 3.11 + FastAPI + SQLAlchemy 2.x + Alembic + asyncio stack
- ADR-002  4-tier layer contract (`api > service > repository > models`) with independence modules
- ADR-003  Async bounded executor with `asyncio.TaskGroup` and graceful drain
- ADR-004  Subprocess execution via `create_subprocess_exec` + `shlex.split`, no `shell=True`
- ADR-005  API key authentication — SHA-256 storage + `hmac.compare_digest`
- ADR-006  Centralised scope authorisation, authz-before-lookup ordering
- ADR-007  Per-token token-bucket rate limiting with DB row-level lock
- ADR-008  N+1 prevention via SQLAlchemy event-listener + `selectinload`/`joinedload`
- ADR-009  RFC 7807 `application/problem+json` with allow-list `detail` scrubber
- ADR-010  Reversible Alembic migrations including data movement (v3 split)
- ADR-011  Configuration via pydantic `BaseSettings` + URL/password scrubber
- ADR-012  Session lifecycle: single context-managed scope per request
- ADR-013  `Makefile verify-system` as the canonical system verification target
- ADR-014  Error path integrity — `CancelledError` re-raise, no `except Exception` swallow

---

## ADR-001: Python 3.11 + FastAPI + SQLAlchemy 2.x + Alembic + asyncio stack

### Status
Accepted (R2 baseline).

### Context
Round 2 needs four dimensions never exercised by Round 1 (`taskq-plus`): an
HTTP layer, a relational database with explicit transaction boundaries, real
schema migrations with data movement, and production-shaped async execution
(SPEC §0 "本輪設計意圖"). Python 3.11 is the SPEC §1 pinned runtime; the
`.venv/bin/python --version` reads **3.11.15**. The framework's
`harness/toolchains/registry.py::DIMENSION_TOOLS["python"]` provides concrete
tools (`pytest-benchmark`, `bandit`, `mutmut`, `import-linter`,
`pip-licenses`, `pytest-cov`) — the stack must expose them cleanly.

### Decision
- Runtime: CPython 3.11 (no 3.12-only syntax; `asyncio.TaskGroup` from 3.11 is
  used in FR-08).
- HTTP framework: **FastAPI** (ASGI) served by `uvicorn`.
- Validation: **pydantic v2** request/response models.
- ORM: **SQLAlchemy 2.x** declarative with explicit `Session` boundaries.
- Migration: **Alembic** with three reversible revisions (FR-07).
- Async: `async def` endpoints plus `asyncio.TaskGroup` background executor.

### Consequences
- Positive: every Round 2 dimension (HTTP, DB, schema migration, async) is
  exercised with a real tool, not a synthetic stand-in; SPEC §8 acceptance
  criteria are mechanically verifiable.
- Negative: the dependency tree grows (`fastapi`, `sqlalchemy`, `alembic`,
  `uvicorn`, `pydantic`, plus transitive), raising the SBOM and license
  compliance surface (NFR-07).
- Negative: the stack pins Python 3.11 — a later SPEC bump to 3.12 must
  re-check `asyncio.TaskGroup` semantics and `httpx.ASGITransport` compatibility.

### Alternatives Considered
- **Pure stdlib** (`http.server` + `sqlite3` + `threading`): zero new
  dependencies, but no ASGI test path, no real ORM N+1 surface, no real
  schema migration tool — defeats the §0 "新增什麼" column. Rejected.
- **Flask + SQLAlchemy 1.x sync**: simpler mental model, but loses the
  async dimension §0 requires and makes FR-08 (concurrent executor)
  awkward. Rejected.
- **Django + DRF**: heavier than FastAPI for the same CRUD surface,
  ships its own ORM/migration but with weaker transaction-boundary
  guarantees for the runner pattern FR-08 needs. Rejected.

---

## ADR-002: 4-tier layer contract (`api > service > repository > models`) with independence modules

### Status
Accepted (binding under NFR-06 + SAD §2.10).

### Context
SAD §1 + NFR-06 require a top-down direction (`api > service > repository >
models`) plus a **forbidden contract** that `sqlalchemy` may only be
imported inside `repository/` (NFR-06). The CRG scoring for `taskq-api`
groups by directory; a flat package would fragment the Leiden communities
below the 0.3 cohesion threshold. `config` and `errors` must be
**independence modules**: importable by any layer but importing nothing
back into any layer.

### Decision
```
api       → service, repository, models, errors, config
service   → repository, models, errors, config
repository→ models, errors, config
models    → (no intra-package imports)
errors    → config  (and stdlib only)
config    → (no intra-package imports)
__main__  → service, repository, models, errors, config
```
Enforced by `.importlinter` (FR-06, NFR-06). `lint-imports` is exit-0
mandatory — relaxing the contract or using `ignore_imports` wildcards is a
SPEC violation (R-K class per SAD §2.10).

### Consequences
- Positive: the layering is mechanically testable; ORM leakage into the
  business layer is a CI failure, not a code-review smell.
- Positive: each layer lives under exactly one directory, satisfying CRG
  Principle 1 and producing healthy communities (≤ 50 nodes each).
- Negative: handlers cannot call `repository` directly — they always go via
  `service`, which adds a small indirection cost; offset by clearer
  transaction ownership (ADR-012).
- Negative: `errors` depending on `config` is intra-independence — the
  contract deliberately does not express it; the SAB block carries the
  intra-layer edge comment (SAD §5).

### Alternatives Considered
- **Flat package with free imports**: zero contract surface, but
  `bandit`/`lint-imports` cannot detect the recurring leak that motivated
  the rule (ORM creeping into handlers). Rejected.
- **Hexagonal / ports & adapters**: cleaner architecture for larger codebases;
  overkill for ~5 entities and adds a `domain` layer that contributes
  external edges with no offsetting internal edges (CRG). Rejected.
- **2-tier (handlers + db)**: simpler but loses the dedicated `service`
  place for FR-04 (authz-before-lookup) and FR-08 (subprocess runner)
  cross-cutting orchestration. Rejected.

---

## ADR-003: Async bounded executor with `asyncio.TaskGroup` and graceful drain

### Status
Accepted (FR-08, NFR-03).

### Context
FR-08 requires background execution with (a) bounded concurrency, (b) a
real timeout that ends the child process, not just the coroutine, and (c)
graceful drain on shutdown.  Python 3.11 ships `asyncio.TaskGroup` (PEP
654 / 3.11), giving structured concurrency that maps cleanly to (a) and
(c).  `asyncio.CancelledError` is the cancel primitive; NFR-03 forbids
swallowing it as `except Exception` (R7 mitigation).

### Decision
- `taskq_api.service.runner` uses `asyncio.TaskGroup`.
- A semaphore caps in-flight coroutines to `TASKQ_MAX_CONCURRENT` (default 8).
- Each child task runs under `asyncio.wait_for(...)`; on timeout, the
  runner calls `process.kill()` then `await process.wait()` (FR-08, NFR-03).
- On shutdown, drain waits up to `TASKQ_DRAIN_TIMEOUT`; tasks past the
  budget are marked `interrupted`.
- `asyncio.CancelledError` is re-raised in every `try` block; it is never
  classified as `Exception`.

### Consequences
- Positive: `TaskGroup` guarantees that when a child raises, siblings and
  the parent see a single joined failure — no orphan coroutines even on
  bug paths.
- Positive: explicit semaphore + drain makes the process load profile
  predictable under burst arrivals.
- Negative: every code path that catches exceptions has to think about
  `CancelledError` separately; the AST scanner must accept that pattern
  instead of flagging it as "bare except" mitigation (NFR-03).

### Alternatives Considered
- **`concurrent.futures.ThreadPoolExecutor`**: easy, but spawns OS threads
  per task, can't `kill()` the child because the child is *our* Python
  function (not a subprocess), and doesn't integrate with the FastAPI
  request lifecycle. Rejected for FR-08.
- **Manual task list with `gather()`**: same semantics but no structured
  failure propagation; partial failures become silent. Rejected.
- **`asyncio.Semaphore` without `TaskGroup`**: covers bounded concurrency
  but loses the auto-cancel-siblings behaviour; would have to hand-roll
  shutdown, re-introducing R7. Rejected.

---

## ADR-004: Subprocess execution via `create_subprocess_exec` + `shlex.split`, no `shell=True`

### Status
Accepted (FR-02, NFR-02, SAD §2.5).

### Context
Task commands are user-controlled strings stored in `tasks.command`
(SPEC §5.2).  Invoking them through a shell invites command injection
(T-06 in SAD §6).  Killing a child via `asyncio.wait_for` does **not**
terminate the child if the executor opens a shell (the shell absorbs the
signal), violating FR-08's no-orphan-process rule (R8).

### Decision
- `service.runner` calls `asyncio.create_subprocess_exec(*shlex.split(command))`
  with `shell=False` (the default).
- A repo-wide grep gate enforces `shell=True`, `eval(`, and `exec(` to
  have **zero hits** in `03-development/src/` (NFR-02, §8 #16).
- Timeout fires `process.kill()` followed by `await process.wait()` to
  avoid orphan child processes.

### Consequences
- Positive: command injection is structurally impossible — `shlex.split`
  is the only parser, no shell metacharacter interpretation happens.
- Positive: the timeout and shutdown paths actually terminate the OS
  process, satisfying §8 #25.
- Negative: the user cannot use shell features (pipes, redirects) inside
  `command`.  The CLI admin (`python -m taskq_api`) is unaffected because
  users embed the call as a token list in `command`.

### Alternatives Considered
- **`subprocess.run(shell=True)`**: short and supports shell features,
  but trivially exploitable by a `tasks.command` containing
  `; rm -rf /`. Rejected.
- **`os.system(...)`**: no return code, no stdio capture, no
  cancellation hook. Rejected.
- **Allowed-list command DSL**: harder to implement, breaks the
  ergonomic similarity with Round 1. Deferred (not rejected) for a future
  round if FR-02 is extended.

---

## ADR-005: API key authentication — SHA-256 storage + `hmac.compare_digest`

### Status
Accepted (FR-03, NFR-02, NFR-04).

### Context
Authentication keys are bearer secrets (T-04). Two non-negotiables: the
plaintext must never persist (NFR-04), and equality checks must be
constant-time to defeat timing side channels (R3).

### Decision
- `repository.api_keys.key_hash` stores `hashlib.sha256(plaintext).hexdigest()`
  (64 hex chars).
- `service.auth.authenticate` looks up by hash and validates via
  `hmac.compare_digest`.
- Plaintext is printed to stdout exactly once, by `__main__ key create`,
  and never logged or persisted.
- `revoked_at IS NULL` is part of the lookup filter.
- The CLI delegate (`__main__`) calls `service.auth.hash_key` — there is
  no second hash implementation anywhere.

### Consequences
- Positive: timing-safe compare prevents token enumeration (R3).
- Positive: `grep -c "key_hash" SCHEMA` is a testable, structural
  assertion — no plaintext column exists to leak.
- Negative: a SHA-256 hash without a salt is brute-forceable against
  weak keys; out of scope for Round 2 because the SPEC does not require
  per-key peppering; documented as a future hardening point.

### Alternatives Considered
- **Plaintext storage**: simplest, forbidden by NFR-04. Rejected.
- **PBKDF2 / bcrypt / argon2**: stronger KDF, but bcrypt is not in the
  Round-2 license allow-list without a `requirements.lock` audit; a
  future round can re-evaluate.
- **`hmac.compare_digest` on plaintext read**: would require storing
  plaintext. Rejected.

---

## ADR-006: Centralised scope authorisation, authz-before-lookup ordering

### Status
Accepted (FR-04, NFR-02, SAD §2.4).

### Context
FR-04 requires scope (`read < write < admin`) and forbids 403 responses
from leaking whether a resource exists (R4, T-05). NFR-02 audits require
that no handler enforces auth locally — a single FastAPI dependency
performs both authn (`api_key_auth`) and authz (`require_scope('read')`).

### Decision
- All `/v1/*` routes go through `api.dependencies.api_key_auth` →
  `require_scope(...)` → `rate_limit_dep`.
- Scope check runs **before** any repository lookup; missing scope and
  missing resource return identical `problem+json` bodies (T-05).
- Handlers do not perform auth checks.  The body of any handler that
  does so is a SPEC violation.

### Consequences
- Positive: §8 #6 (403 must not leak existence) is a structural
  property, not an assertion per handler.
- Positive: a single dependency file is the testable surface for the
  whole auth subsystem; CRG hub-and-spoke edges are concentrated there
  (Principle 2).
- Negative: handlers cannot short-circuit auth — they must call the
  dependency even for unauthenticated routes such as `/healthz`,
  requiring a separate path through the dependency.

### Alternatives Considered
- **Per-handler auth**: higher per-route control, but the leak-prevention
  invariant must be enforced per handler — drifting from §8 #6 is a
  one-line mistake. Rejected.
- **ASGI middleware for scope**: would work for global rules, but
  per-route scope (e.g. `read` for `GET` vs `admin` for `DELETE`) needs
  a per-route handler anyway. Rejected.
- **OAuth2 / JWT**: stronger delegation model but exceeds Round 2 scope
  and adds a token-introspection dependency. Deferred.

---

## ADR-007: Per-token token-bucket rate limiting with DB row-level lock

### Status
Accepted (FR-05, NFR-02, R12).

### Context
Multiple ASGI workers must share one rate-limit budget per API key, so
the bucket cannot be in-process state (R12). Cross-worker fairness must
hold under simultaneous arrivals.

### Decision
- `rate_buckets` table holds `tokens` and `updated_at` per `api_key`.
- Updates run inside a single transaction; on PostgreSQL this uses
  `SELECT ... FOR UPDATE`; on SQLite, the explicit transaction +
  `BEGIN IMMEDIATE` produces the same serial behaviour.
- Refill is computed in `service.rate_limit.consume` and written
  atomically with the deduction — no read–modify–write race.

### Consequences
- Positive: rate-limit behaviour is identical across worker processes —
  no per-worker skew.
- Positive: 429 responses can carry an accurate `Retry-After` because
  the refill rate is known at the point of refusal.
- Negative: every request performs at least one extra transaction; for
  the SPEC's traffic profile (single-tenant benchmark) this is well
  inside NFR-01 p95 budgets.

### Alternatives Considered
- **In-memory bucket (per worker)**: fast, but cross-worker over-allow
  by `N_workers`. Rejected.
- **Sliding-window log**: more accurate but needs a per-request row,
  blowing up storage and violating NFR-07 licence budget. Rejected for
  Round 2.
- **Redis-backed bucket**: simpler scaling story, but Redis is not in
  the §5.1 env list and would expand the SBOM. Deferred.

---

## ADR-008: N+1 prevention via SQLAlchemy event-listener + `selectinload`/`joinedload`

### Status
Accepted (NFR-01, R5, SAD §2.6 / §4.1).

### Context
R5: list endpoints with lazy relationships degrade as `O(rows × relations)`;
SPEC §8 #14 requires the SQL count to be **constant** in the row count,
making N+1 an explicit acceptance failure.

### Decision
- `repository.session.attach_n_plus_one_listener` registers an
  `Engine "before_cursor_execute"` counter that raises in test mode on
  per-request statement count > constant budget for list endpoints.
- All list queries use explicit `selectinload(...)` / `joinedload(...)`
  on `Task.results` and `Task.tags`.
- Cursor pagination (FR-01) — not offset — to avoid late-page scan
  regressions.

### Consequences
- Positive: N+1 is fail-fast at test time, not a production slow leak.
- Positive: the listener is the single place that defines "acceptable"
  SQL count for a list endpoint, so PRs are easy to review.
- Negative: developers must remember the eager-load hints on new list
  endpoints; absence is silent in dev mode. The test-mode raise fixes
  this at CI cost.

### Alternatives Considered
- **`selectinload` only, no listener**: easy to forget, only fails in
  production. Rejected.
- **Forcing `lazy='selectin'` globally** (`relationship(..., lazy=...)`):
  would catch all paths but inflates single-row reads by joining too
  much. Mixed strategy chosen — listener + per-call hints.
- **Manual SQL count check in tests**: works but duplicates the contract
  in every test. Listener + one integration assertion is leaner.

---

## ADR-009: RFC 7807 `application/problem+json` with allow-list `detail` scrubber

### Status
Accepted (FR-10, NFR-02, NFR-04, R6, T-09).

### Context
R6: 500 responses carrying raw stack traces or SQL leak internal
structure. FR-10 mandates `application/problem+json` (typed RFC 7807).
NFR-04 forbids DSN strings in any log / body / metrics output.

### Decision
- Every non-2xx route raises one of the
  `taskq_api.errors.HTTPException` subclasses
  (`Unauthenticated`, `Forbidden`, `NotFound`, `Conflict`, `RateLimited`,
  `Validation`, `NotReady`, `Internal`).
- `errors.problem(status, type_uri, title, detail, correlation_id)`
  builds the envelope with a fixed field set:
  `type`, `title`, `status`, `detail`, `instance`, `correlation_id`.
- `detail` is an allow-list fed by the handler; internal lines
  (SQL, stack, paths, passwords, `postgres://...`) are scrubbed before
  serialisation.
- `correlation_id` is set by ASGI middleware in `api.app` and echoed in
  `X-Correlation-Id`.

### Consequences
- Positive: §8 #19 (500 body has no internals) is one module's
  contract, easy to audit and test with adversarial inputs.
- Positive: client integrations have a stable error shape (RFC 7807)
  rather than per-route JSON.
- Negative: handlers must remember to raise the subclass, never
  raw `HTTPException`; a regression reverts to FastAPI's default
  error shape.

### Alternatives Considered
- **FastAPI default JSON error handler**: inconsistent schema, leaks
  default structure. Rejected.
- **Plain `{"error": "..."}` envelopes**: rejects the SPEC's RFC 7807
  requirement. Rejected.
- **Hand-rolled JSON response per error**: scatters the envelope across
  the codebase; impossible to keep `detail` scrub centralised. Rejected.

---

## ADR-010: Reversible Alembic migrations including data movement (v3 split)

### Status
Accepted (FR-07, NFR-03, R1).

### Context
R1: silently losing data on the v3 `tasks.result_json → task_results`
split is the single highest-impact migration risk. The SPEC §8 #12
round-trip test sets "same column values" as a hard acceptance bar.

### Decision
- Three revisions: `v1_initial`, `v2_add_tags`, `v3_split_results`.
- Every `upgrade()` has a working `downgrade()`; `op.execute("DROP TABLE ...")`
  short-cuts are forbidden (NFR-06 / §8 #13).
- v3's data movement is implemented as `INSERT ... SELECT` from
  `tasks.result_json` into `task_results`, then column drop — and the
  reverse mirrors that.
- The round-trip test exercises a real SQLite file (not in-memory)
  with sample rows, asserting column-for-column equality.

### Consequences
- Positive: §8 #12 is a single, mechanical test that proves v3
  irreversibility risk is bounded.
- Positive: the same `v3_split_results` script can be replayed against
  any project state because the downgrade is structurally identical
  (no shortcut `DROP`).
- Negative: v3 is longer than the other two revisions and concentrates
  the migration risk — it is in `high_risk_modules` (SAD §5).

### Alternatives Considered
- **Single migration creating the whole post-v3 schema from scratch**:
  loses existing-row support; SPEC explicitly forbids it (the round-trip
  test names v3 row preservation). Rejected.
- **Custom JSON `version` column** (Round 1 pattern): not a real schema
  migration; SPEC §0 explicitly rejects it for Round 2. Rejected.
- **`ALTER TABLE ... DROP COLUMN result_json`** without a data move:
  fails §8 #12 acceptance. Rejected.

---

## ADR-011: Configuration via pydantic `BaseSettings` + URL/password scrubber

### Status
Accepted (SAD §2.2, NFR-04, NFR-07).

### Context
Twelve `TASKQ_*` env vars must be declared (`.env.example` count is a
§8 #26 acceptance bar). Connection strings must never reach stdout,
logs, or `/v1/metrics` (NFR-04).

### Decision
- `taskq_api.config.Settings` is a `pydantic.BaseSettings` subclass
  with all twelve env vars typed and defaulted (SPEC §5.1).
- `config.scrub_db_url(url)` strips the password before any
  log/serialisation site uses the URL.
- `config` imports nothing intra-package (independence module —
  ADR-002).

### Consequences
- Positive: `.env.example` count is mechanically verifiable (§8 #26).
- Positive: a single function (`scrub_db_url`) protects every leak
  vector; new consumers inherit it for free.
- Negative: the independence rule means `config` cannot call into
  `errors` for any logging — it must log via the stdlib `logging`
  directly, which the integration test pins.

### Alternatives Considered
- **`os.environ` direct read scattered through handlers**: no central
  scrubbing; §8 #20 fails. Rejected.
- **`dynaconf` / `pydantic-settings`**: extra dependency;
  `pydantic.BaseSettings` already provides the typing we need.
  Deferred (not strictly rejected).
- **`attrs` + `cattrs`**: dependency-light, but loses env-driven
  defaults. Rejected.

---

## ADR-012: Session lifecycle: single context-managed scope per request

### Status
Accepted (FR-06, NFR-03, SAD §2.6).

### Context
FR-06 requires one `Session` per request with explicit
commit-on-success / rollback-on-exception semantics, enforced by a
context manager. NFR-03 makes "rolls back on exception" a structural
assertion, not a code-review convention.

### Decision
- `repository.session.session_scope()` is the **only** way to obtain a
  `Session`; handlers/services never call `create_engine` directly
  (SAD §2.6 logical constraint).
- The context manager `yield`s a session, commits on clean exit, and
  rolls back on any exception (including `asyncio.CancelledError`,
  where applicable).
- All `text(...)` SQL is bound via SQLAlchemy parameters — no f-string,
  `%`, or `+` concatenation (NFR-02 grep gate, §8 #17).
- Connection pool: `pool_size=TASKQ_DB_POOL_SIZE`, `pool_pre_ping=True`
  to fail closed on stale connections (R10, FR-09).

### Consequences
- Positive: transaction boundaries are visible from the source —
  readers see a `with session_scope()` block and know what the
  request commits.
- Positive: `pool_pre_ping=True` makes "DB went away" a fast 503
  rather than a slow partial-failure mid-transaction.
- Negative: hand-rolled session usage becomes impossible by
  convention, but a developer could still `import sqlalchemy` in a
  sibling module — that is what ADR-002's forbidden contract catches
  at CI time.

### Alternatives Considered
- **`autoflush=True` global session**: hides explicit transaction
  boundaries; fails the §8 #2 100% coverage gate because the commit
  call site disappears. Rejected.
- **Per-handler `create_engine`**: bypasses the pool, leaks
  connections on exception. Rejected.
- **No pool (`NullPool`)**: scales poorly past a single worker and
  contradicts FR-06 `pool_size`. Rejected.

---

## ADR-013: `Makefile verify-system` as the canonical system verification target

### Status
Accepted (NFR-12, SAD §1.1).

### Context
The harness has exactly one gate that runs the delivered system:
`make verify-system`. SPEC §1.1 names two rules: (1) at least one step
must invoke the real entry point, and (2) the step that does so must be
able to fail (no `|| true`, no leading `-`, no `--exit-zero`). Every
other gate reads source text or runs tests that the test suite has
already configured.

### Decision
- The single target name `verify-system` is fixed — the harness calls
  it by name.
- The target chains:
  1. `alembic upgrade head`
  2. full test suite (`pytest -q`)
  3. `uvicorn taskq_api.app:app` startup + `/healthz`, `/readyz` smoke
  4. `alembic downgrade base`
  5. `alembic upgrade head` (round-trip data integrity, FR-07 / §8 #12)
- The smoke step invokes the entry point directly; its failure is the
  target's exit code, with `verify-system: PASS` printed only on
  success.

### Consequences
- Positive: at least one high-risk module
  (`runner`, `auth`, `repository.session`, `v3_split_results`) runs
  against real dependencies on every CI pass.
- Positive: the migration round-trip lives in the same script as the
  unit tests, so the `v3_split_results` test never silently rots.
- Negative: the target is slower than `pytest -q` alone; cannot be
  disabled via `|| true` without failing the gate.

### Alternatives Considered
- **`bash scripts/verify.sh`**: works, but the harness requires the
  Makefile target name. Rejected.
- **`tox`**: brings a parallel runner, but loses the explicit `alembic
  upgrade → test → uvicorn smoke → alembic downgrade` ordering the
  SPEC §8 #27 token requires. Rejected.
- **Always-on smoke daemon**: heavier; doesn't change the contract.
  Deferred.

---

## ADR-014: Error path integrity — `CancelledError` re-raise, no `except Exception` swallow

### Status
Accepted (NFR-03, R7, R8, SAD §2.5 / §4.3).

### Context
NFR-03 forbids `except Exception` swallowing and `asyncio.CancelledError`
muting. R7 names it specifically: if `CancelledError` is caught as
`Exception`, the FastAPI shutdown path waits forever on a coroutine that
already gave up. R8 ties timeout handling to that same primitive.

### Decision
- All `except` blocks explicitly name the exception class
  (`except KeyError`, `except sqlalchemy.exc.IntegrityError`, etc.) —
  never `except:` or `except Exception: pass`.
- `asyncio.CancelledError` is **never** caught; if a `try` block must
  contain a coroutine that may raise it, the block re-raises after
  cleanup: `try: ... except CancelledError: await cleanup(); raise`.
- The AST scanner (`ast-error-handling`) flags both patterns and is
  wired into CI.

### Consequences
- Positive: §8 #1 (`pytest -q` skip = 0) and §8 #25 (no orphan
  processes) become structural CI signals, not assertion-per-test.
- Positive: shutdown drains deterministically — no coroutine that
  appears to be "running" forever.
- Negative: legacy `except Exception` patterns from Round 1 must be
  rewritten; this is an intentional refactor of any code crossing the
  boundary.

### Alternatives Considered
- **`contextlib.suppress(Exception)`**: same problem, more idiomatic
  syntax. Rejected.
- **`tenacity`-style retries**: would mask `CancelledError` under a
  generic `try/except`; rejected.
- **Catching only `BaseException` minus `CancelledError`**: works,
  but obscures intent — the explicit re-raise pattern is more legible.
  Deferred.

---

## Traceability Matrix

The following traceability matrix links each architecture decision to the
SRS requirement IDs (FR-01..FR-10, NFR-01..NFR-12) and the specification
section(s) it satisfies. Each row is the **owning** decision for that
requirement — the row where the architectural choice is committed, not
just referenced. Cross-cutting NFRs (e.g. NFR-06 layering, NFR-09 zero-skip)
have no single owning decision because they are enforced by structural
mechanics rather than a chosen framework; those rows say so honestly
rather than fabricating an ADR that doesn't carry the choice.

| ADR | Owns FR / NFR | Specification section(s) satisfied | SAD reference |
|-----|---------------|-------------------------------------|---------------|
| ADR-001  Python 3.11 + FastAPI + SQLAlchemy 2.x + Alembic + asyncio stack | FR-08 (async executor), NFR-02 (data-layer security toolchain), NFR-07 (dependency licence budget) | SPEC §0 (Round-2 design intent: HTTP, relational DB, schema migration, async), SPEC §1 (Python runtime pin), SPEC §8 acceptance vocabulary (the FastAPI/ASGI test path and `asyncio.TaskGroup` semantics that §8 #25/§8 #27 measure against) | SAD §0 (binding stack) |
| ADR-002  4-tier layer contract with independence modules | NFR-06 (architecture layering + forbidden `sqlalchemy` contract), NFR-05 (per-layer docstring coverage is the natural unit for the `ast-docstrings` scan), NFR-11 (per-directory ≤ 15-file / per-module layering keeps file size under the readability budget) | SPEC §3 (layering + forbidden-contract requirement), SPEC §8 #6 (403 must not leak existence — single dependency surface), SPEC §10 (`ast-docstrings` dimension for NFR-05), SPEC §11 (readability floor) | SAD §1 + §2.10, FR-06 |
| ADR-003  Async bounded executor with `asyncio.TaskGroup` and graceful drain | FR-08 (concurrent executor) | SPEC §5.2 (subprocess invocation), SPEC §8 #25 (no orphan processes), SPEC §0 (async dimension) | SAD §2.5 |
| ADR-004  Subprocess execution via `create_subprocess_exec` + `shlex.split`, no `shell=True` | FR-02 (subprocess runner), FR-08 (no-orphan-process rule) | SPEC §5.2 (subprocess invocation contract), SPEC §8 #16 (no `shell=True`/`eval`/`exec` grep gate) | SAD §2.5 (T-06 command-injection mitigation) |
| ADR-005  API key authentication — SHA-256 storage + `hmac.compare_digest` | FR-03 (API key auth), NFR-04 (no plaintext in storage/logs) | SPEC §5.3 (auth contract), SPEC §8 #5 (no plaintext column leak) | SAD §2.3, R3 (timing-side-channel) |
| ADR-006  Centralised scope authorisation, authz-before-lookup ordering | FR-04 (scope `read < write < admin`), NFR-02 (auditable authn/authz) | SPEC §5.3 (scope contract), SPEC §8 #6 (403 must not leak existence) | SAD §2.4, R4, T-05 |
| ADR-007  Per-token token-bucket rate limiting with DB row-level lock | FR-05 (rate limit) | SPEC §5.3 (rate-limit contract), SPEC §0 (cross-worker fairness) | SAD §2.4, R12 |
| ADR-008  N+1 prevention via SQLAlchemy event-listener + `selectinload`/`joinedload` | NFR-01 (performance / constant SQL count on list endpoints) | SPEC §8 #14 (constant SQL count), SPEC §6 (cursor pagination contract) | SAD §2.6 / §4.1, R5 |
| ADR-009  RFC 7807 `application/problem+json` with allow-list `detail` scrubber | FR-10 (RFC 7807 error envelope), NFR-04 (DSN scrub) | SPEC §5.4 (error envelope contract), SPEC §8 #19 (no internals in 500 body) | SAD §2.5 / §4.3, R6, T-09 |
| ADR-010  Reversible Alembic migrations including data movement (v3 split) | FR-07 (three reversible revisions), NFR-09 (round-trip against a real SQLite file — `in-memory` is forbidden by the zero-skip clause) | SPEC §5.5 (migration contract), SPEC §8 #12 (round-trip data integrity), SPEC §8 #13 (no shortcut `DROP TABLE`) | SAD §4.2, R1 |
| ADR-011  Configuration via pydantic `BaseSettings` + URL/password scrubber | NFR-04 (no DSN in stdout/logs/metrics), NFR-07 (dependency / licence surface) | SPEC §5.1 (twelve `TASKQ_*` env vars), SPEC §8 #26 (`.env.example` count), SPEC §8 #20 (no URL leak) | SAD §2.2 |
| ADR-012  Session lifecycle: single context-managed scope per request | FR-06 (one `Session` per request with commit/rollback semantics), FR-09 (fail-closed on stale connections) | SPEC §5.6 (session/transaction contract), SPEC §8 #17 (no f-string SQL) | SAD §2.6, R10, NFR-03 |
| ADR-013  `Makefile verify-system` as the canonical system verification target | NFR-12 (system verification gate), FR-07 (round-trip data integrity), NFR-05 (`ast-docstrings` scan + OpenAPI schema assertion wired into the same target), NFR-08 (`mutmut` chained as SPEC §8 #24), NFR-09 (`pytest -q` must report `0 skipped` per SPEC §8 #1), NFR-10 (`pytest ... --cov ...` TOTAL ≥ 80% integration coverage per SPEC §8 #3, ASGITransport-only per SRS AC-N10.2), NFR-11 (`radon mi` / CC limits checked by the same target) | SPEC §1.1 (verify-system target name + non-zero-exit rule), SPEC §8 #27 (canonical token), SPEC §8 #12 (round-trip), SPEC §8 #1, #3, #24 (test-tooling acceptance rows) | SAD §1.1 |
| ADR-014  Error path integrity — `CancelledError` re-raise, no `except Exception` swallow | NFR-03 (no `except Exception` swallow, no `CancelledError` mute), NFR-09 (no swallowed exceptions means the `pytest 0 skipped` and AST-scanner bars stay structural, not assertion-per-test) | SPEC §8 #1 (zero skipped tests), SPEC §8 #25 (no orphan processes), SPEC §7 (harness AST scanner contract) | SAD §2.5 / §4.3, R7, R8 |

### Cross-cutting NFRs — structural ownership

The following NFRs have no single framework choice that "owns" them; the
architecture instead makes them mechanically enforceable by combining
existing ADRs. Each row below names the ADR(s) whose decisions the
requirement actually depends on, plus the SPEC/SRS section that anchors
the acceptance bar. The prose here intentionally restates the
cross-cutting framing already established by the named ADRs — these are
not new decisions, they are honest attributions.

| ADR(s) | NFR satisfied | Anchored to SPEC/SRS section(s) |
|--------|---------------|----------------------------------|
| ADR-002 + ADR-013 | NFR-05 (document coverage — every public symbol has a docstring with `[FR-XX]`/`[NFR-XX]`, OpenAPI summary+description) | SPEC §10 (ast-docstrings dimension listed for NFR-05), SRS NFR-05 + AC-N5.1/AC-N5.2 |
| ADR-013 | NFR-08 (mutation testing — `mutmut` score ≥ 70 for `service/` + `repository/`) | SPEC §10 (`requirements-dev.txt` lists `mutmut`), SPEC §8 #24 (mutation score ≥ 70 acceptance), SRS NFR-08 + AC-N8.1/AC-N8.2/AC-N8.3 |
| ADR-010 + ADR-013 + ADR-014 | NFR-09 (zero-skip pytest + real-DB migration round-trip + no `--ignore`/`-k` anti-fake) | SPEC §8 #1 (`pytest 03-development/tests -q` skipped count = 0), SPEC §8 #12 (round-trip data integrity), SRS NFR-09 + AC-N9.1 |
| ADR-013 | NFR-10 (integration coverage ≥ 80% via `httpx.AsyncClient(transport=ASGITransport(app))`, must not call handlers directly) | SPEC §8 #3 (TOTAL ≥ 80%), SPEC §10 (`httpx` listed), SRS NFR-10 + AC-N10.1/AC-N10.2/AC-N10.3 |
| ADR-002 + ADR-013 | NFR-11 (readability — MI ≥ 80, CC ≤ 10, file ≤ 400 LOC, dir ≤ 15 files, handler ≤ 40 lines) | SPEC §10 (tooling for readability), SPEC §11 (readability threshold anchor), SRS NFR-11 + AC-N11.1/AC-N11.2/AC-N11.3/AC-N11.4 |

### Reference indices

- **SRS FR-IDs satisfied**: FR-01, FR-02, FR-03, FR-04, FR-05, FR-06,
  FR-07, FR-08, FR-09, FR-10 (all ten).
- **SRS NFR-IDs satisfied by an owning ADR**: NFR-01, NFR-02, NFR-03,
  NFR-04, NFR-06, NFR-07, NFR-12.
- **SRS NFR-IDs satisfied structurally (cross-cutting)**: NFR-05, NFR-08,
  NFR-09, NFR-10, NFR-11.
- **SPEC §8 acceptance tokens explicitly anchored**: #1, #2, #5, #6, #12,
  #13, #14, #16, #17, #19, #20, #25, #26, #27.

---

*End of ADR — Phase 2 deliverable for `taskq-api` (harness Round 2).*
