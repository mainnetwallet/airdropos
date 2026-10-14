"""Add columns the models declare but an older SQLite database lacks.

`Base.metadata.create_all` only creates missing TABLES; it never alters an existing one. A
database made before a model gained a column (e.g. memory_records.scope) therefore fails with
"no such column". This module closes that gap for SQLite only, and only ever issues
`ALTER TABLE ... ADD COLUMN`: nothing is dropped, renamed or rewritten, and every existing row
is kept. PostgreSQL deployments use Alembic instead.

It is used by `init_db` (so `git pull` + `py run.py` is enough) and by
`scripts/sqlite_add_missing_columns.py` (manual dry run).
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import inspect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateColumn

from backend.app.core.config import settings
from backend.app.database.session import Base

logger = logging.getLogger("airdropos.schema_sync")

# (table, column, ALTER-ready column DDL)
Missing = Tuple[str, str, str]


def is_sqlite() -> bool:
    return settings.DATABASE_URL.startswith("sqlite")


def sqlite_file() -> Optional[Path]:
    if not is_sqlite() or "///" not in settings.DATABASE_URL:
        return None
    raw = settings.DATABASE_URL.split("///", 1)[1].split("?", 1)[0]
    return None if raw in ("", ":memory:") else Path(raw).resolve()


def find_missing(conn: Connection) -> List[Missing]:
    """Columns present on a model but absent from the live table (existing tables only)."""
    dialect = sqlite_dialect.dialect()
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    missing: List[Missing] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # a whole missing table is create_all's job
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = str(CreateColumn(col).compile(dialect=dialect))
            # SQLite cannot ADD a NOT NULL column that has no default. Relax it: the ORM still
            # supplies the value for new rows, existing rows get the default (or NULL).
            if col.nullable is False and col.server_default is None:
                ddl = ddl.replace(" NOT NULL", "")
                default = getattr(col.default, "arg", None) if col.default is not None else None
                if getattr(col.default, "is_scalar", False):
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {int(default)}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    elif isinstance(default, str):
                        ddl += " DEFAULT '" + default.replace("'", "''") + "'"
            missing.append((table.name, col.name, ddl))
    return missing


def apply_missing(conn: Connection, missing: List[Missing]) -> None:
    for table, column, ddl in missing:
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        logger.info("Added missing column %s.%s", table, column)


def backup_sqlite_file() -> Optional[Path]:
    path = sqlite_file()
    if path is None or not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, target)
    return target
