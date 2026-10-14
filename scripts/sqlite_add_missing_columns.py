"""Add columns the models declare but an older SQLite database lacks.

Why: `create_all` only creates missing TABLES. It never alters an existing one, so a
database made before a model gained a column (e.g. memory_records.scope) fails with
"no such column". This adds those columns in place and keeps every existing row.

Usage (project root, venv active, backend and worker stopped):
    python scripts/sqlite_add_missing_columns.py            # dry run: only lists
    python scripts/sqlite_add_missing_columns.py --apply    # backs up, then adds

Only ADD COLUMN is ever issued. Nothing is dropped, renamed or rewritten.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402
from sqlalchemy.schema import CreateColumn  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.database.session import Base  # noqa: E402
import backend.app.models  # noqa: E402,F401  (registers every table on Base)


def _db_path() -> Path:
    url = settings.DATABASE_URL
    if not url.startswith("sqlite"):
        sys.exit(f"DATABASE_URL is not SQLite ({url.split(':')[0]}); this script is SQLite only.")
    raw = url.split("///", 1)[1]
    return Path(raw).resolve()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = _db_path()
    if not path.exists():
        sys.exit(f"Database file not found: {path}")
    print(f"Database: {path}")

    dialect = sqlite_dialect.dialect()
    conn = sqlite3.connect(path)
    existing_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    todo: list[tuple[str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # whole table missing: init_db / create_all handles that
        have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table.name}")')}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = str(CreateColumn(col).compile(dialect=dialect))
            # SQLite cannot ADD a NOT NULL column without a default, so relax it. The ORM
            # still supplies the value for new rows; existing rows get the default/NULL.
            if col.nullable is False and col.server_default is None:
                ddl = ddl.replace(" NOT NULL", "")
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    lit = col.default.arg
                    lit = f"'{lit}'" if isinstance(lit, str) else int(lit) if isinstance(lit, bool) else lit
                    ddl += f" DEFAULT {lit}"
            todo.append((table.name, col.name, ddl))

    if not todo:
        print("Schema already has every column. Nothing to do.")
        return

    print(f"\n{len(todo)} missing column(s):")
    for t, c, ddl in todo:
        print(f"  {t}.{c}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to add them (a backup is made first).")
        return

    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    print(f"\nBackup: {backup}")

    for t, c, ddl in todo:
        conn.execute(f'ALTER TABLE "{t}" ADD COLUMN {ddl}')
        print(f"  added {t}.{c}")
    conn.commit()
    conn.close()
    print("\nDone. Restart backend and worker.")


if __name__ == "__main__":
    main()
