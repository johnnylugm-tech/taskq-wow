"""[FR-01] repository layer — persistence boundary (SAD §2.7).

The repository layer depends only on `models` and `independence` per
SAB.json. In Phase-3 GREEN this is backed by an in-memory store; Phase-4
swaps in SQLAlchemy 2.x + Alembic per FR-06.
"""