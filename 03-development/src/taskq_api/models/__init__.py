"""[FR-01] models layer — pydantic schemas and ORM rows (SAD §2.7).

The models layer is the dependency sink of the package per SAB.json
(`allowed_dependencies: []`). Pydantic v2 schemas live here; Phase-4
adds SQLAlchemy 2.x ORM rows under `models.orm` per FR-06.
"""