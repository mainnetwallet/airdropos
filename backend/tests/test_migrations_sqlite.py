"""Migration integrity for the Agent Inbox.

Two things are checked:

1. The chain RUNS on SQLite from an empty database. SQLite is a documented backend
   (`run.py --sqlite`), and revision 1 was PostgreSQL-only, so `alembic upgrade head`
   failed immediately on it.

2. The tables the Agent Inbox depends on end up matching the ORM. Everything the feature
   reads or writes has to exist, or the Inbox 500s against a migrated database.

Scoped deliberately. Many other tables in this schema still differ from the models -- a
pre-existing, project-wide condition documented in MIGRATION_DRIFT.md. Reconciling all of
them is a separate piece of work, and silently widening this test would hide that rather
than fix it.

All databases used here are temporary. The developer's own database is never touched.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO / "backend" / "alembic.ini"

# Tables the Agent Inbox reads or writes, and therefore cannot function without.
INBOX_TABLES = (
    "agent_conversations",
    "conversation_messages",
    "intervention_requests",
)

# Columns added to existing tables by the inbox revisions.
INBOX_TASK_COLUMNS = ("owner_email", "operator_instructions")

# The persistent execution-context tables, and the columns that give memory a scope.
CONTEXT_TABLES = ("task_contexts", "task_observations", "task_requirements")
CONTEXT_MEMORY_COLUMNS = ("scope", "task_id", "campaign_id")

# The revision at the head of the chain. Pinned so a new revision that breaks the chain
# is caught here rather than at deploy time.
HEAD_REVISION = "6_task_context"


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    # A temporary state dir so nothing reaches the real emergency-stop file.
    env["EMERGENCY_STOP_STATE_FILE"] = str(db_path.parent / "estop.json")
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory) -> Path:
    """A temporary database migrated from empty to head."""
    db_path = tmp_path_factory.mktemp("migrations") / "chain.db"
    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, (
        "the migration chain does not run on SQLite from an empty database:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return db_path


class TestChainRunsOnSqlite:
    def test_upgrade_from_empty_reaches_head(self, migrated_db: Path):
        result = _run_alembic(migrated_db, "current")
        assert HEAD_REVISION in result.stdout, result.stdout

    def test_downgrade_to_base_is_supported(self, tmp_path_factory):
        db_path = tmp_path_factory.mktemp("downgrade") / "chain.db"
        assert _run_alembic(db_path, "upgrade", "head").returncode == 0
        result = _run_alembic(db_path, "downgrade", "base")
        assert result.returncode == 0, (
            f"downgrade to base failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_running_upgrade_again_is_a_no_op(self, tmp_path_factory):
        """Guarded DDL: re-running against an already-migrated database must not fail."""
        db_path = tmp_path_factory.mktemp("idem") / "chain.db"
        assert _run_alembic(db_path, "upgrade", "head").returncode == 0
        result = _run_alembic(db_path, "upgrade", "head")
        assert result.returncode == 0


class TestInboxSchemaMatchesTheModels:
    def _columns(self, db_path: Path, table: str) -> set:
        con = sqlite3.connect(db_path)
        try:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def test_inbox_tables_exist(self, migrated_db: Path):
        for table in INBOX_TABLES:
            assert self._columns(migrated_db, table), f"{table} was not created"

    def test_inbox_table_columns_match_the_orm(self, migrated_db: Path):
        sys.path.insert(0, str(REPO))
        try:
            from backend.app.models import Base
        finally:
            sys.path.pop(0)

        for table in Base.metadata.sorted_tables:
            if table.name not in INBOX_TABLES:
                continue
            actual = self._columns(migrated_db, table.name)
            declared = {c.name for c in table.columns}
            assert declared <= actual, (
                f"{table.name} is missing columns the model declares: "
                f"{sorted(declared - actual)}"
            )

    def test_task_columns_added_by_the_inbox_exist(self, migrated_db: Path):
        actual = self._columns(migrated_db, "tasks")
        for column in INBOX_TASK_COLUMNS:
            assert column in actual, f"tasks.{column} was never created by the chain"

    def test_authentication_tables_are_usable(self, migrated_db: Path):
        """The Inbox needs a login, so the session columns the auth layer writes must exist."""
        users = self._columns(migrated_db, "users")
        sessions = self._columns(migrated_db, "user_sessions")
        assert {"email", "hashed_password", "telegram_user_id"} <= users
        assert {"user_id", "token", "expires_at", "updated_at"} <= sessions

    def test_intervention_uniqueness_is_enforced(self, migrated_db: Path):
        con = sqlite3.connect(migrated_db)
        try:
            indexes = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='intervention_requests'"
            )}
        finally:
            con.close()
        assert "ix_intervention_requests_request_id" in indexes


class TestContextSchemaMatchesTheModels:
    def _columns(self, db_path: Path, table: str) -> set:
        con = sqlite3.connect(db_path)
        try:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def test_context_tables_exist(self, migrated_db: Path):
        for table in CONTEXT_TABLES:
            assert self._columns(migrated_db, table), f"{table} was not created"

    def test_context_table_columns_match_the_orm(self, migrated_db: Path):
        sys.path.insert(0, str(REPO))
        try:
            from backend.app.models import Base
        finally:
            sys.path.pop(0)

        for table in Base.metadata.sorted_tables:
            if table.name not in CONTEXT_TABLES:
                continue
            actual = self._columns(migrated_db, table.name)
            declared = {c.name for c in table.columns}
            assert declared <= actual, (
                f"{table.name} is missing columns the model declares: "
                f"{sorted(declared - actual)}"
            )

    def test_memory_scope_columns_exist(self, migrated_db: Path):
        """Without these every memory is global and one task reads another's history."""
        actual = self._columns(migrated_db, "memory_records")
        for column in CONTEXT_MEMORY_COLUMNS:
            assert column in actual, f"memory_records.{column} was never created by the chain"

    def test_task_context_is_unique_per_task(self, migrated_db: Path):
        """One isolated context per task. Two rows would mean two histories for one run."""
        con = sqlite3.connect(migrated_db)
        try:
            indexes = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_contexts'"
            )}
        finally:
            con.close()
        assert "ix_task_contexts_task_id" in indexes