"""[FR-01] service layer — business logic (SAD §2.7).

The service layer depends only on `repository`, `models`, and
`independence` per SAB.json. Service modules own validation, uniqueness
checks (NP-05), and authz ordering (NP-08).
"""