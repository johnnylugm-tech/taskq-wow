"""[FR-01] api layer — HTTP routers and dependencies (SAD §2.7).

The api layer depends only on `service`, `repository`, `models`, and
`independence` per SAB.json. Routers stay ≤40 lines each per NFR-11 and
delegate business logic to the service layer.
"""