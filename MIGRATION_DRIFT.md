# Known migration drift (pre-existing)

The migration chain now **runs** on SQLite from an empty database and all the way back
down again. It did not before: revision 1 used PostgreSQL-only DDL, so
`alembic upgrade head` failed on SQLite at the first table.

That fix is complete and tested (`backend/tests/test_migrations_sqlite.py`).

What is **not** complete: revision 1 predates most of the current models, so on a
database built through the chain, many tables still lack columns the ORM declares. This
is pre-existing, affects roughly thirty tables, and is unrelated to the Agent Inbox.

## What this means in practice

| Database was built with | Usable? |
|---|---|
| `Base.metadata.create_all` (`run.py`, `init_db`) — the normal path | Yes. Always matched the models. |
| `alembic upgrade head` | Partly. The chain runs, but do not assume the schema is complete. |

The project's real deployment path is `create_all`. Alembic remains the mechanism for
*applying* a change to an already-migrated database, which is what revision 5 does.

## Why the two disagree

Revision 1 was written once, early, against an earlier set of models. Only a handful of
tables were ever reconciled afterwards, by revisions 2 through 5. The `test_migrations.py`
static checks only cover `wallets`, `memory_records` and `knowledge_relationships`, which is
why the rest went unnoticed.

## What was fixed here

Drift that the Agent Inbox actually depends on, because the feature cannot function
without it:

- `users.telegram_user_id` — the auth layer writes it; no migration created it.
- `user_sessions.updated_at` — inherited from `Base`; without it no session can be
  inserted, so a chain-built database could not authenticate anyone.
- `postgresql.ARRAY(...)` columns → `JSONB`, which renders as `JSON` on SQLite and matches
  what the models declare.
- The `updated_at` trigger is now dialect-aware and is only attached to tables that
  actually have an `updated_at` column. Revision 1 listed `user_sessions`, which has none,
  so the trigger referenced a column that does not exist.

## Known remaining limitation

Rolling all the way back and forward again (`downgrade base`, then `upgrade head`) fails:
revision 2's `downgrade` does not drop `integration_accounts`, so revision 1 recreates it
and revision 2 then tries to create it a second time. This is pre-existing and only
reachable now that the chain runs on SQLite at all. Full rollback-then-forward is not a
supported workflow; fixing it means rewriting revision 2's downgrade.

## Reconciling the rest

Out of scope here, and worth doing deliberately rather than opportunistically. The safe
order is one table per revision, each guarded by an inspector check, each verified by
extending the scoped assertions in `test_migrations_sqlite.py`. Until then, treat
`create_all` as the source of truth for a local database.