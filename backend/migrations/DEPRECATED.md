# DEPRECATED: do not use this directory

This is a stale, unused duplicate of `backend/app/migrations/`. The active Alembic
configuration is `backend/alembic.ini`, which points at `backend/app/migrations/`.

Why it must not be used:

- Its only revision (`1`) calls `Base.metadata.create_all()` from the *current* models, so what
  it creates changes whenever the models change and it never matches the real migration chain.
- Its revision id (`1`) differs from the active chain (`1_initial_schema`,
  `2_reconcile_schema_with_models`). A database stamped here cannot be upgraded by the real chain.

It is kept only so no migration history is deleted. Remove it in a reviewed change once every
deployment is confirmed to be on the `backend/app/migrations/` chain.
