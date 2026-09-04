# Traceability Matrix — taskq-api

> Requirements Traceability Matrix
> Framework: harness-methodology
> Version: v1.0
> Phase: P1 (01-requirements) — Round 1, derived from SPEC_TRACKING.md + SRS.md + TEST_INVENTORY.yaml

---

## Overview

Provides complete **FR ↔ SRS ↔ Design ↔ Test** bidirectional traceability supporting ASPICE SWE.3/SYS.4 compliance.

Authoritative SSOT for the matrix is `01-requirements/SPEC_TRACKING.md` (status column
machine-refreshed from `build_traceability`) and `TEST_INVENTORY.yaml` (test_function/test_file
naming authority consumed by `derive_test_cases.md`). Rows below cross-reference both.

Phase-1 snapshot: design elements (`02-architecture/`) and code/test artefacts
(`03-development/`, `04-testing/`) are PENDING until P2/P3/P4. P1 produces the
forward-mapping skeleton; P2 fills the design rows; P3/P4 populate code and test
rows and turn statuses from PENDING to IMPLEMENTED → VERIFIED.

---

## 1. FR ↔ Spec Mapping

| FR ID | Functional Requirement (short) | SRS Section | Priority | Status | Test Inventory Ref |
|-------|---------------------------------|-------------|----------|--------|---------------------|
| FR-01 | Task resource CRUD (POST/GET/GET-list/DELETE /v1/tasks, per-method scope, cursor pagination, problem+json) | SRS §3 FR-01 | HIGH | DRAFT | TC-FR01-01 (integration), TC-FR01-02 (unit) + AC-1.1..AC-1.9 |
| FR-02 | Task execution (POST /v1/tasks/{id}/run → 202; subprocess via shlex.split; state machine pending→running→{done,failed,timeout}; GET runs history) | SRS §3 FR-02 | HIGH | DRAFT | AC-2.1..AC-2.6 |
| FR-03 | API Key auth (X-API-Key on /v1/*; SHA-256 hash; hmac.compare_digest; plaintext-once at key create; revoked → 401; /healthz /readyz exempt) | SRS §3 FR-03 | HIGH | DRAFT | AC-3.1..AC-3.6 (cross-cuts NFR-02, NFR-04) |
| FR-04 | Scope authorization (read<write<admin hierarchy; insufficient → 403 with no existence leak; single shared dependency) | SRS §3 FR-04 | HIGH | DRAFT | AC-4.1..AC-4.3 (cross-cuts NFR-02) |
| FR-05 | Rate limiting (per-token token bucket; capacity TASKQ_RATE_BURST; refill TASKQ_RATE_PER_SEC; 429 + Retry-After; DB-persisted via row-locking; /healthz /readyz exempt) | SRS §3 FR-05 | HIGH | DRAFT | AC-5.1..AC-5.3 |
| FR-06 | Persistence + transaction boundaries (`repository/` only; one Session per request; no string-concat SQL; eager-load; pool_size/pool_pre_ping) | SRS §3 FR-06 | HIGH | DRAFT | AC-6.1..AC-6.5 (cross-cuts NFR-01, NFR-06) |
| FR-07 | Alembic three-step migration (v1 tasks+api_keys; v2 tags+task_tags + tasks.name unique; v3 split tasks.result_json → task_results; downgrade preserved; round-trip bytewise; no DROP shortcuts) | SRS §3 FR-07 | HIGH | DRAFT | AC-7.1..AC-7.7 |
| FR-08 | Async executor (asyncio.TaskGroup; TASKQ_DRAIN_TIMEOUT drain; TASKQ_MAX_CONCURRENT cap; wait_for + process.kill + wait; CancelledError propagates) | SRS §3 FR-08 | HIGH | DRAFT | AC-8.1..AC-8.4 (cross-cuts NFR-03, FR-02) |
| FR-09 | Health checks + observability (/healthz → 200 no-auth; /readyz → 200 iff DB+alembic head else 503 with detail; /v1/metrics admin scope, no DB URL leak) | SRS §3 FR-09 | HIGH | DRAFT | AC-9.1..AC-9.5 |
| FR-10 | RFC 7807 error contract (Content-Type application/problem+json; 6-field body; no stack/SQL/path leak; correlation_id echoed header + log; full status mapping 422/401/403/404/409/429/503/500) | SRS §3 FR-10 | HIGH | DRAFT | AC-10.1..AC-10.5 (cross-cuts NFR-02, NFR-04) |
| NFR-01 | Performance (GET /v1/tasks/{id} p95 < 30ms @ 10k records; list p95 < 80ms; constant SQL count; pytest-benchmark) | SRS §4 NFR-01 | HIGH | DRAFT | AC-N1.1..AC-N1.3 |
| NFR-02 | Security (ban shell=True/eval/exec; no f-string SQL; API keys hashed; 403 no existence leak; 500 no internals; CORS default-deny + allowlist; bandit 0 HIGH/MED) | SRS §4 NFR-02 | HIGH | DRAFT | TC-N02-01 (static) + AC-N2.1..AC-N2.6 |
| NFR-03 | Error handling & transaction & async correctness (explicit tx boundary; no bare except / swallow pass; CancelledError re-raise; /readyz 503 on DB fail no silent retry; task timeout kills child; migration fail rolls back) | SRS §4 NFR-03 | HIGH | DRAFT | AC-N3.1..AC-N3.4 |
| NFR-04 | Sensitive-data redaction (regex redaction in stdout_tail/stderr_tail/logs/error bodies; no DB password fragment; API key plaintext only at key create) | SRS §4 NFR-04 | HIGH | DRAFT | AC-N4.1..AC-N4.2 |
| NFR-05 | Documentation coverage (100% docstring on public def/class with [FR-XX]/[NFR-XX] tag; OpenAPI summary+description per /v1 endpoint) | SRS §4 NFR-05 | MEDIUM | DRAFT | AC-N5.1..AC-N5.2 |
| NFR-06 | Architecture layering (importlinter api > service > repository > models; config/errors independent; non-repository MUST NOT import sqlalchemy; lint-imports exits 0) | SRS §4 NFR-06 | HIGH | DRAFT | AC-N6.1..AC-N6.4 |
| NFR-07 | Dependency + license compliance (pinned == in requirements.txt; lock complete; allowlist MIT/BSD-2/BSD-3/Apache-2.0/PSF; SBOM at 08-config/SBOM.json) | SRS §4 NFR-07 | HIGH | DRAFT | AC-N7.1..AC-N7.4 |
| NFR-08 | Mutation testing (features.mutation_testing: true; score ≥ 70; scope service/+repository/; crg_cohesion_healthy not lowered) | SRS §4 NFR-08 | HIGH | DRAFT | AC-N8.1..AC-N8.3 |
| NFR-09 | Verification authenticity (zero-skip iron rule; pytest skipped == 0; zero-assertion tests forbidden; no --ignore/-k/--deselect/collect_ignore bypass; FR-07 real-file SQLite; matrix VERIFIED only after test passes) | SRS §4 NFR-09 | HIGH | DRAFT | TC-N09-01 (integration) + AC-N9.1..AC-N9.5 |
| NFR-10 | Integration coverage (03-development/tests/integration/ ≥ 80%; httpx.AsyncClient(ASGITransport) only; covers full CRUD + 401/403/404/409/422/429/503 + migration round-trip + rate-limit + graceful drain) | SRS §4 NFR-10 | HIGH | DRAFT | AC-N10.1..AC-N10.3 |
| NFR-11 | Readability (project MI ≥ 80; per-function CC ≤ 10; file ≤ 400 lines; dir ≤ 15 files; API handler ≤ 40 lines) | SRS §4 NFR-11 | MEDIUM | DRAFT | AC-N11.1..AC-N11.4 |
| NFR-12 | System verification target (Makefile `verify-system` chains alembic upgrade head → full test suite → service + /healthz+/readyz smoke → alembic downgrade base → alembic upgrade head; exit 0 + `verify-system: PASS` on stdout) | SRS §4 NFR-12 | HIGH | DRAFT | AC-N12.1..AC-N12.2 |

> Row count: 10 FR + 12 NFR = **22 requirement rows**. P1 floor: 1:1 row per FR/NFR.

---

## 2. Spec ↔ Design Mapping (P2 skeleton; design elements resolved in ADR/SAD)

| SRS Section | Design Element (P2) | Layer | ADR Ref | Status |
|-------------|----------------------|-------|---------|--------|
| SRS §3 FR-01 | `api/tasks.py` router + `service/task_service.py` + `repository/task_repository.py` + `models/task.py` | api / service / repository / models | PENDING (P2 ADR-001) | PENDING |
| SRS §3 FR-02 | `service/runner.py` (subprocess via shlex.split) + `service/state_machine.py` | service | PENDING (P2 ADR-002) | PENDING |
| SRS §3 FR-03 | `service/auth.py` (X-API-Key) + `repository/api_key_repository.py` + CLI `taskq_api key create` | service + repository + CLI | PENDING (P2 ADR-003) | PENDING |
| SRS §3 FR-04 | `service/auth.py::require_scope` shared dependency | service | PENDING (P2 ADR-004) | PENDING |
| SRS §3 FR-05 | `service/rate_limit.py` (token bucket) + `repository/rate_limit_repository.py` (row-locked) | service + repository | PENDING (P2 ADR-005) | PENDING |
| SRS §3 FR-06 | `repository/` boundary + `db/session.py` (context manager) + `db/engine.py` (pool_size, pool_pre_ping) + importlinter contract | repository + infra | PENDING (P2 ADR-006, cross-cuts NFR-06) | PENDING |
| SRS §3 FR-07 | `alembic/versions/` v1, v2, v3 + `db/migrations` CLI surface | infra | PENDING (P2 ADR-007) | PENDING |
| SRS §3 FR-08 | `service/executor.py` (TaskGroup + drain + kill+wait) | service | PENDING (P2 ADR-008, cross-cuts FR-02/NFR-03) | PENDING |
| SRS §3 FR-09 | `api/health.py` (healthz/readyz) + `api/metrics.py` + `service/observability.py` | api + service | PENDING (P2 ADR-009) | PENDING |
| SRS §3 FR-10 | `errors/problem.py` (RFC 7807 builder) + middleware correlation_id | infra | PENDING (P2 ADR-010, cross-cuts NFR-02/NFR-04) | PENDING |
| SRS §4 NFR-01 | eager-load in `repository/task_repository.py` + SQLAlchemy event-listener statement counter | repository | PENDING (P2 ADR-006) | PENDING |
| SRS §4 NFR-02 | grep gates (shell/eval/exec + SQL concat) + bandit CI + CORS middleware default-deny | infra + toolchain | PENDING (P2 ADR-011) | PENDING |
| SRS §4 NFR-03 | context-manager tx + AST `no bare except / no swallow pass` + CancelledError-propagation tests + /readyz fail-closed | service + infra + tests | PENDING (P2 ADR-012) | PENDING |
| SRS §4 NFR-04 | `service/redaction.py` (regex redaction) + log filter + metrics filter | service + infra | PENDING (P2 ADR-013) | PENDING |
| SRS §4 NFR-05 | docstring + FR/NFR tag scan + OpenAPI summary/description | repo-wide + OpenAPI | PENDING (P2 ADR-014) | PENDING |
| SRS §4 NFR-06 | `.importlinter` contracts (api>service>repository>models; sqlalchemy-in-repository-only) | repo-wide | PENDING (P2 ADR-015) | PENDING |
| SRS §4 NFR-07 | `requirements.txt` (pinned ==) + `requirements.lock` + `pip-licenses` + `08-config/SBOM.json` | repo-wide + config | PENDING (P2 ADR-016) | PENDING |
| SRS §4 NFR-08 | `.methodology/harness_config.json` `features.mutation_testing: true` + scope `service/+repository/` | toolchain | PENDING (P2 ADR-017) | PENDING |
| SRS §4 NFR-09 | pytest summary gate + ast-assertions scan + pytest-config scan + real-file migration test + traceability-audit-log | toolchain + tests | PENDING (P2 ADR-018) | PENDING |
| SRS §4 NFR-10 | pytest-cov integration ≥ 80% + ASGITransport-only scan + scenario checklist test | tests | PENDING (P2 ADR-019) | PENDING |
| SRS §4 NFR-11 | `radon mi` aggregate ≥ 80 + `radon cc` ≤ 10 + line/file/dir limits + handler ≤ 40 LOC | repo-wide | PENDING (P2 ADR-020) | PENDING |
| SRS §4 NFR-12 | `Makefile` `verify-system` chain | config + toolchain | PENDING (P2 ADR-021) | PENDING |

> P2 produces ADR-001..ADR-021 and SAD.md; rows above are reconciled by P2 phase
> completion. No row may be left PENDING at P2 close.

---

## 3. Design ↔ Test Mapping (P3/P4 — populated as 03-development/ and 04-testing/ artefacts land)

| Design Element (P2) | Test File (planned) | Test Functions (planned, from TEST_INVENTORY.yaml + derive_test_cases.md) | Layer | Status |
|----------------------|----------------------|---------------------------------------------------------------------------|-------|--------|
| `api/tasks.py` | `03-development/tests/integration/test_fr01.py` | `test_fr01_example_integration` (TC-FR01-01) + AC-1.1..AC-1.9 | integration | PENDING |
| `service/task_service.py` | `03-development/tests/unit/test_fr01.py` | `test_fr01_example_unit` (TC-FR01-02) | unit | PENDING |
| `service/runner.py` | `03-development/tests/integration/test_fr02.py` | TBD by derive_test_cases.md (6 ACs) | integration | PENDING |
| `service/auth.py` | `03-development/tests/integration/test_fr03.py` | TBD (6 ACs) | integration | PENDING |
| `service/auth.py::require_scope` | `03-development/tests/integration/test_fr04.py` | TBD (3 ACs) | integration | PENDING |
| `service/rate_limit.py` | `03-development/tests/integration/test_fr05.py` | TBD (3 ACs) | integration | PENDING |
| `repository/` + `db/session.py` | `03-development/tests/integration/test_fr06.py` + unit | TBD (5 ACs) | unit + integration | PENDING |
| `alembic/versions/` v1/v2/v3 | `03-development/tests/integration/test_fr07.py` (real SQLite file) | TBD (7 ACs) | integration | PENDING |
| `service/executor.py` | `03-development/tests/integration/test_fr08.py` | TBD (4 ACs) | integration | PENDING |
| `api/health.py` + `api/metrics.py` | `03-development/tests/integration/test_fr09.py` | TBD (5 ACs) | integration | PENDING |
| `errors/problem.py` | `03-development/tests/integration/test_fr10.py` | TBD (5 ACs) | integration | PENDING |
| (perf instrumented repo) | `03-development/tests/performance/test_nfr01.py` | TBD (3 ACs; pytest-benchmark) | performance | PENDING |
| (security toolchain) | `03-development/tests/static/test_nfr02.py` | `test_security_example` (TC-N02-01) + 6 ACs | static | PENDING |
| (async/tx correctness) | `03-development/tests/integration/test_nfr03.py` | TBD (4 ACs) | integration | PENDING |
| `service/redaction.py` | `03-development/tests/integration/test_nfr04.py` | TBD (2 ACs) | integration | PENDING |
| (doc + OpenAPI) | `03-development/tests/static/test_nfr05.py` | TBD (2 ACs) | static | PENDING |
| `.importlinter` | `03-development/tests/static/test_nfr06.py` | TBD (4 ACs) | static | PENDING |
| `requirements.txt` + SBOM | `03-development/tests/static/test_nfr07.py` | TBD (4 ACs) | static | PENDING |
| mutation scope | `03-development/tests/mutation/` (mutmut) | TBD (3 ACs) | mutation | PENDING |
| (zero-skip gates) | `03-development/tests/integration/test_nfr09.py` | `test_deployment_example` (TC-N09-01) + 5 ACs | integration | PENDING |
| integration suite | `03-development/tests/integration/` | TBD (3 ACs) | integration | PENDING |
| readability gates | `03-development/tests/static/test_nfr11.py` | TBD (4 ACs) | static | PENDING |
| `Makefile` `verify-system` | `03-development/tests/integration/test_nfr12.py` | TBD (2 ACs) | integration | PENDING |

> All rows are PENDING at P1 close; P3 development populates code columns,
> P4 testing populates test columns, and `build_traceability` transitions
> status to IN_PROGRESS → VERIFIED. TEST_INVENTORY.yaml is the naming authority.

---

## 4. Cross-Reference Index (FR ↔ NFR cut matrix)

Forward refs to downstream phase docs (canonical names, no invented files):
- 01-requirements → {SPEC_TRACKING.md, SRS.md, TEST_INVENTORY.yaml, TRACEABILITY_MATRIX.md}
- 02-architecture → {ADR.md, SAD.md, TEST_SPEC.md}
- 04-testing → {TEST_PLAN.md, TEST_RESULTS.md}
- 05-verification → {BASELINE.md, VERIFICATION_REPORT.md}
- 06-quality → {FINAL_SIGN_OFF.md, QUALITY_REPORT.md, RELEASE_NOTES.md}
- 07-risk → {RISK_MITIGATION_PLANS.md, RISK_REGISTER.md, RISK_STATUS_REPORT.md}
- 08-config → {CONFIG_RECORDS.md, RELEASE_CHECKLIST.md}

| NFR / FR | FR-01 | FR-02 | FR-03 | FR-04 | FR-05 | FR-06 | FR-07 | FR-08 | FR-09 | FR-10 |
|----------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|
| NFR-01   |       |       |       |       |       | X     |       |       |       |       |
| NFR-02   |       | X     | X     | X     |       |       |       |       |       | X     |
| NFR-03   |       |       |       |       |       | X     | X     | X     |       |       |
| NFR-04   |       |       | X     |       |       |       |       |       | X     | X     |
| NFR-05   | (repo-wide, not FR-specific) |
| NFR-06   |       |       |       |       |       | X     |       |       |       |       |
| NFR-07   | (repo-wide dependency tree) |
| NFR-08   | (scope: service/+repository/) |
| NFR-09   | (zero-skip applies to every FR test) |
| NFR-10   | (integration coverage applies to every FR) |
| NFR-11   | (readability applies to every FR) |
| NFR-12   | (verify-system chains full chain including all FRs) |

---

## 5. Completeness Verification

| Check | Target | Actual (P1) | Status |
|-------|--------|-------------|--------|
| FR → SRS mapping | 100% (10/10) | 10/10 | PASS |
| NFR → SRS mapping | 100% (12/12) | 12/12 | PASS |
| Spec → Design mapping | 100% (P2 close) | 0/22 (P2 pending) | PENDING (P2) |
| Design → Test mapping | 100% (P3/P4 close) | 0/22 (P3/P4 pending) | PENDING (P3/P4) |
| Test coverage | ≥ 80% line (P3) / ≥ 70% branch floor (P3 early) | n/a | PENDING (P3/P4) |
| FR-07 real-file migration test | 1 | 0 (planned) | PENDING (P3) |
| Integration tests using `httpx.AsyncClient(ASGITransport(app))` only | 100% | n/a | PENDING (P4) |
| TEST_INVENTORY.yaml ↔ TRACEABILITY_MATRIX §3 naming parity | 100% | matches TC-FR01-01/02, TC-N02-01, TC-N09-01 in §3 | PASS (P1 floor) |

---

## 6. ASPICE Compliance

| ASPICE Capability | Evidence (this matrix) | Status |
|-------------------|------------------------|--------|
| SWE.3.B.SP1 Task-to-work-product traceability | §1 (FR ↔ Spec) + §3 (Design ↔ Test) | IN PROGRESS (P3/P4 rows PENDING) |
| SWE.3.B.SP2 Bidirectional traceability | §4 cross-reference index (FR ↔ NFR) + §2 (Spec → Design) reverse-implied | IN PROGRESS (P2 design rows PENDING) |
| SWE.3.B.SP3 Traceability consistency | §5 completeness table + machine-refresh from build_traceability | IN PROGRESS (P1 floor PASS; full PASS at P4 close) |

---

## 7. Update Log

| Date | Change | By |
|------|--------|----|
| 2026-09-05 | Initial creation (10 FR + 12 NFR transcribed from SPEC_TRACKING.md + SRS.md §3/§4; TEST_INVENTORY.yaml test IDs mirrored in §3; ASPICE rows IN PROGRESS pending P2/P3/P4) | Agent A (Round 1, P1) |