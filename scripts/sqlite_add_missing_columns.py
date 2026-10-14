"""Manual check/repair for "no such column" on SQLite.

`py run.py` already does this automatically at startup (init_db). Use this script only to see
what is missing without starting the app:

    python scripts/sqlite_add_missing_columns.py            # dry run: only lists
    python scripts/sqlite_add_missing_columns.py --apply    # backs up, then adds

Only ADD COLUMN is ever issued. Nothing is dropped, renamed or rewritten.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend.app.models  # noqa: E402,F401  (registers every table on Base)
from backend.app.database import schema_sync  # noqa: E402
from backend.app.database.session import engine  # noqa: E402


async def main(apply: bool) -> None:
    if not schema_sync.is_sqlite():
        sys.exit("DATABASE_URL is not SQLite; this tool is SQLite only (use Alembic for PostgreSQL).")
    print(f"Database: {schema_sync.sqlite_file()}")
    async with engine.connect() as conn:
        missing = await conn.run_sync(schema_sync.find_missing)
    if not missing:
        print("Schema already has every column. Nothing to do.")
        return
    print(f"\n{len(missing)} missing column(s):")
    for t, c, _ in missing:
        print(f"  {t}.{c}")
    if not apply:
        print("\nDry run. Re-run with --apply to add them (a backup is made first).")
        return
    print(f"\nBackup: {schema_sync.backup_sqlite_file()}")
    async with engine.begin() as conn:
        await conn.run_sync(schema_sync.apply_missing, missing)
    print("Done. Restart the app.")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
