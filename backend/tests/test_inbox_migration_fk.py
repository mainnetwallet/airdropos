"""Foreign-key validation in migration 7 (`7_inbox_lifecycle`).

Revision 7 has to leave each Inbox relationship with exactly one *correct* foreign key:

    agent_conversations.task_id          -> tasks.id                 ON DELETE CASCADE
    conversation_messages.conversation_id -> agent_conversations.id   ON DELETE CASCADE
    conversation_messages.task_id        -> tasks.id                 ON DELETE CASCADE
    intervention_requests.task_id        -> tasks.id                 ON DELETE CASCADE
    intervention_requests.conversation_id -> agent_conversations.id   ON DELETE CASCADE

An earlier version only asked "is there ANY foreign key on this column?", so a constraint that
pointed at the wrong table, or that did not cascade, was accepted and the real cascade was never
installed. Task deletion then failed (or left Inbox rows behind) on exactly the databases the
migration exists to repair.

Two layers are tested.

1. The planner, against real SQLite schemas. `plan_foreign_keys` only reads the schema, so the
   decision of what to drop and create is checked with SQLAlchemy's own inspector output on
   SQLite, which always runs. SQLite cannot ALTER a constraint, so nothing is *executed* there;
   the migration leaves SQLite's constraints alone and the application deletes children itself.

2. The migration itself, against a real PostgreSQL. Skipped unless AIRDROPOS_TEST_POSTGRES_URL
   points at a server whose user may CREATE DATABASE, for example

       AIRDROPOS_TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:5432/postgres

   Every test works in its own throwaway database, so nothing else on the server is touched.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

REPO = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO / "backend" / "alembic.ini"
MIGRATION_FILE = REPO / "backend" / "app" / "migrations" / "versions" / "7_inbox_lifecycle.py"


def _load_migration():
    """Import the revision by path: its file name starts with a digit, so it is not importable."""
    spec = importlib.util.spec_from_file_location("inbox_lifecycle_rev7", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()

# (key, table, column, referred table, standard constraint name)
RELATIONSHIPS = [
    ("conv_task", *migration.CASCADES[0]),
    ("msg_conv", *migration.CASCADES[1]),
    ("msg_task", *migration.CASCADES[2]),
    ("req_task", *migration.CASCADES[3]),
    ("req_conv", *migration.CASCADES[4]),
]
BY_KEY = {key: (table, column, referred, name) for key, table, column, referred, name in RELATIONSHIPS}


# ======================================================================
# 1. The planner, on real SQLite schemas
# ======================================================================
def _fk_sql(key: str, *, name: Optional[str] = "standard", target: Optional[str] = None,
            target_column: str = "id", ondelete: str = "ON DELETE CASCADE") -> str:
    """One FOREIGN KEY clause for relationship `key`, ready to splice into CREATE TABLE."""
    _table, column, referred, standard = BY_KEY[key]
    constraint = ""
    if name:
        constraint = f"CONSTRAINT {standard if name == 'standard' else name} "
    return (
        f", {constraint}FOREIGN KEY ({column}) "
        f"REFERENCES {target or referred}({target_column}) {ondelete}".rstrip()
    )


def _sqlite_schema(overrides: Optional[Dict[str, Optional[str]]] = None, extra_columns: str = ""):
    """An in-memory SQLite database with the five Inbox relationships.

    Every relationship gets its correct, named cascade foreign key unless `overrides` replaces
    the clause (a string) or removes it (None).
    """
    clauses = {key: _fk_sql(key) for key in BY_KEY}
    clauses.update(overrides or {})
    for key in clauses:
        clauses[key] = clauses[key] or ""

    engine = sa.create_engine("sqlite://")
    with engine.begin() as con:
        con.exec_driver_sql("CREATE TABLE tasks (id TEXT PRIMARY KEY, task_id TEXT)")
        con.exec_driver_sql("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        con.exec_driver_sql(
            f"CREATE TABLE agent_conversations (id TEXT PRIMARY KEY, task_id TEXT{extra_columns}"
            f"{clauses['conv_task']})"
        )
        con.exec_driver_sql(
            "CREATE TABLE conversation_messages (id TEXT PRIMARY KEY, conversation_id TEXT, "
            f"task_id TEXT{clauses['msg_conv']}{clauses['msg_task']})"
        )
        con.exec_driver_sql(
            "CREATE TABLE intervention_requests (id TEXT PRIMARY KEY, task_id TEXT, "
            f"conversation_id TEXT{clauses['req_task']}{clauses['req_conv']})"
        )
    return engine


def _plan(engine):
    with engine.connect() as con:
        return migration.plan_foreign_keys(sa.inspect(con))


class TestPlannerOnSqliteSchemas:
    def test_a_schema_with_every_correct_cascade_needs_no_change(self):
        assert _plan(_sqlite_schema()) == []

    def test_a_correct_cascade_with_another_name_is_left_alone(self):
        engine = _sqlite_schema({key: _fk_sql(key, name="someone_elses_name") for key in BY_KEY})
        assert _plan(engine) == [], "a valid constraint must not be rebuilt just for its name"

    def test_a_correct_unnamed_cascade_is_left_alone(self):
        engine = _sqlite_schema({key: _fk_sql(key, name=None) for key in BY_KEY})
        assert _plan(engine) == []

    def test_a_correct_cascade_with_lowercase_action_is_recognised(self):
        engine = _sqlite_schema({"conv_task": _fk_sql("conv_task", ondelete="on delete cascade")})
        assert _plan(engine) == []

    @pytest.mark.parametrize("key", list(BY_KEY))
    def test_a_missing_foreign_key_is_created(self, key):
        table, column, referred, name = BY_KEY[key]
        engine = _sqlite_schema({key: None})
        assert _plan(engine) == [("create", table, name, column, referred)]

    def test_every_foreign_key_missing_creates_all_five(self):
        engine = _sqlite_schema({key: None for key in BY_KEY})
        creates = [a for a in _plan(engine) if a[0] == "create"]
        assert [(a[1], a[3]) for a in creates] == [(t, c) for _k, t, c, _r, _n in RELATIONSHIPS]
        assert len(_plan(engine)) == 5, "nothing but creates when nothing exists to replace"

    @pytest.mark.parametrize(
        "ondelete",
        ["", "ON DELETE NO ACTION", "ON DELETE RESTRICT", "ON DELETE SET NULL", "ON DELETE SET DEFAULT"],
    )
    def test_a_foreign_key_without_cascade_is_replaced(self, ondelete):
        table, column, referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema({"conv_task": _fk_sql("conv_task", name="no_cascade_fk", ondelete=ondelete)})
        assert _plan(engine) == [
            ("drop", table, "no_cascade_fk"),
            ("create", table, name, column, referred),
        ]

    def test_a_foreign_key_to_the_wrong_table_is_replaced(self):
        table, column, referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema({"conv_task": _fk_sql("conv_task", name="wrong_table", target="projects")})
        assert _plan(engine) == [
            ("drop", table, "wrong_table"),
            ("create", table, name, column, referred),
        ]

    def test_a_cascading_foreign_key_to_the_wrong_column_is_replaced(self):
        table, column, referred, name = BY_KEY["req_task"]
        engine = _sqlite_schema(
            {"req_task": _fk_sql("req_task", name="wrong_column", target_column="task_id")}
        )
        assert _plan(engine) == [
            ("drop", table, "wrong_column"),
            ("create", table, name, column, referred),
        ]

    def test_the_cascade_to_the_wrong_parent_is_caught_on_a_child_table_too(self):
        """conversation_messages.conversation_id must reference conversations, not tasks."""
        table, column, referred, name = BY_KEY["msg_conv"]
        engine = _sqlite_schema({"msg_conv": _fk_sql("msg_conv", name="msg_to_task", target="tasks")})
        assert _plan(engine) == [
            ("drop", table, "msg_to_task"),
            ("create", table, name, column, referred),
        ]

    def test_a_correct_foreign_key_next_to_a_wrong_one_only_drops_the_wrong_one(self):
        """No duplicate is created, and the leftover wrong constraint is removed.

        The stale one points at a different table because SQLAlchemy's SQLite inspector cannot
        tell apart two foreign keys with the same column and target in one table; the
        same-target duplicate is covered on PostgreSQL below.
        """
        table = BY_KEY["conv_task"][0]
        both = _fk_sql("conv_task") + _fk_sql("conv_task", name="stale_wrong_table", target="projects")
        assert _plan(_sqlite_schema({"conv_task": both})) == [("drop", table, "stale_wrong_table")]

    def test_a_wrong_foreign_key_without_a_name_is_reported_not_acted_on(self):
        table, column, _referred, _name = BY_KEY["conv_task"]
        engine = _sqlite_schema({"conv_task": _fk_sql("conv_task", name=None, ondelete="")})
        plan = _plan(engine)
        assert [a[:3] for a in plan] == [("unresolved", table, column)]
        assert not [a for a in plan if a[0] in ("drop", "create")], (
            "a constraint that cannot be dropped by name must not get a second one beside it"
        )

    def test_a_constraint_name_already_used_on_another_column_is_reported(self):
        table, column, _referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema(
            {"conv_task": None},
            extra_columns=", other_task_id TEXT, "
            f"CONSTRAINT {name} FOREIGN KEY (other_task_id) REFERENCES tasks(id)",
        )
        plan = _plan(engine)
        assert [a[:3] for a in plan] == [("unresolved", table, column)]

    def test_a_wrong_foreign_key_is_not_dropped_when_the_replacement_name_is_taken(self):
        """The unsafe case: dropping first would leave the table with no constraint at all."""
        table, column, _referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema(
            {"conv_task": _fk_sql("conv_task", name="wrong_table", target="projects")},
            extra_columns=", other_task_id TEXT, "
            f"CONSTRAINT {name} FOREIGN KEY (other_task_id) REFERENCES tasks(id)",
        )
        plan = _plan(engine)
        assert [a[:3] for a in plan] == [("unresolved", table, column)]
        assert not [a for a in plan if a[0] == "drop"], "no destructive action without a replacement"

    def test_a_non_foreign_key_constraint_holding_the_name_also_blocks_the_drop(self):
        table, column, _referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema(
            {"conv_task": _fk_sql("conv_task", name="no_cascade", ondelete="")},
            extra_columns=f", CONSTRAINT {name} UNIQUE (id, task_id)",
        )
        plan = _plan(engine)
        assert [a[:3] for a in plan] == [("unresolved", table, column)]

    def test_a_wrong_foreign_key_that_itself_holds_the_name_is_replaced_in_place(self):
        """The name is free once its own holder is dropped, so drop then create is safe."""
        table, column, referred, name = BY_KEY["conv_task"]
        engine = _sqlite_schema({"conv_task": _fk_sql("conv_task", ondelete="")})  # standard name, no cascade
        assert _plan(engine) == [
            ("drop", table, name),
            ("create", table, name, column, referred),
        ]

    def test_a_stale_duplicate_is_still_dropped_beside_a_correct_cascade_with_the_name_taken(self):
        table = BY_KEY["conv_task"][0]
        both = _fk_sql("conv_task") + _fk_sql("conv_task", name="stale_wrong_table", target="projects")
        assert _plan(_sqlite_schema({"conv_task": both})) == [("drop", table, "stale_wrong_table")]

    def test_tables_and_columns_that_do_not_exist_are_skipped(self):
        engine = sa.create_engine("sqlite://")
        with engine.begin() as con:
            con.exec_driver_sql("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
            # A conversations table that predates task scoping has no task_id column at all.
            con.exec_driver_sql("CREATE TABLE agent_conversations (id TEXT PRIMARY KEY)")
        assert _plan(engine) == []

    def test_planning_never_changes_the_schema(self):
        engine = _sqlite_schema({"conv_task": None, "req_task": _fk_sql("req_task", ondelete="")})
        before = _snapshot(engine)
        _plan(engine)
        assert _snapshot(engine) == before


def _snapshot(engine):
    with engine.connect() as con:
        return con.exec_driver_sql("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()


class TestSqliteMigrationLeavesConstraintsAlone:
    """On SQLite the migration cleans orphans but never rebuilds a table or alters a constraint."""

    def test_upgrade_does_not_change_the_table_definitions(self, tmp_path):
        from backend.tests.test_migrations_sqlite import _run_alembic

        db_path = tmp_path / "inbox.db"
        assert _run_alembic(db_path, "upgrade", "6_task_context").returncode == 0
        before = _sqlite_file_schema(db_path)
        result = _run_alembic(db_path, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert _sqlite_file_schema(db_path) == before, (
            "revision 7 must not rebuild or alter a SQLite table; that is the operation "
            "that could lose the operator's messages"
        )

    def test_upgrade_downgrade_upgrade_round_trip(self, tmp_path):
        from backend.tests.test_migrations_sqlite import _run_alembic

        db_path = tmp_path / "inbox.db"
        for args in (
            ("upgrade", "head"),
            ("upgrade", "head"),
            ("downgrade", "6_task_context"),
            ("upgrade", "head"),
        ):
            result = _run_alembic(db_path, *args)
            assert result.returncode == 0, f"{args}: {result.stdout}\n{result.stderr}"


def _sqlite_file_schema(db_path: Path):
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN "
            "('agent_conversations', 'conversation_messages', 'intervention_requests') ORDER BY name"
        ).fetchall()
    finally:
        con.close()


# ======================================================================
# 2. The migration itself, on a real PostgreSQL
# ======================================================================
POSTGRES_URL = os.environ.get("AIRDROPOS_TEST_POSTGRES_URL", "")

needs_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="set AIRDROPOS_TEST_POSTGRES_URL to run the PostgreSQL migration tests"
)

MESSAGES_TABLE = "conversation_messages"


def _admin_engine():
    return sa.create_engine(
        sa.engine.make_url(POSTGRES_URL).set(drivername="postgresql+psycopg2"),
        isolation_level="AUTOCOMMIT",
    )


def _database_url(name: str, driver: str):
    return sa.engine.make_url(POSTGRES_URL).set(drivername=driver, database=name)


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = _database_url(database, "postgresql+asyncpg").render_as_string(hide_password=False)
    env["EMERGENCY_STOP_STATE_FILE"] = str(Path(os.environ.get("TMPDIR", "/tmp")) / f"estop-{database}.json")
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=600,
    )


@pytest.fixture(scope="module")
def pg_template():
    """One database migrated to revision 6, cloned for every test instead of being rebuilt."""
    if not POSTGRES_URL:
        pytest.skip("AIRDROPOS_TEST_POSTGRES_URL is not set")
    pytest.importorskip("psycopg2")
    pytest.importorskip("asyncpg")
    name = f"airdropos_fk_tpl_{uuid.uuid4().hex[:10]}"
    admin = _admin_engine()
    with admin.connect() as con:
        con.exec_driver_sql(f'CREATE DATABASE "{name}"')
    try:
        result = _alembic(name, "upgrade", "6_task_context")
        assert result.returncode == 0, f"the chain must reach revision 6 on PostgreSQL:\n{result.stdout}\n{result.stderr}"
        yield name
    finally:
        with admin.connect() as con:
            con.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def pg(pg_template):
    """A fresh engine on a private copy of the revision-6 database."""
    name = f"airdropos_fk_{uuid.uuid4().hex[:10]}"
    admin = _admin_engine()
    with admin.connect() as con:
        con.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"')
    engine = sa.create_engine(_database_url(name, "postgresql+psycopg2"))
    engine.database_name = name
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as con:
            con.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def _neutral(type_):
    if isinstance(type_, sa.Enum):
        return type_.enums[0]
    if isinstance(type_, sa.Boolean):
        return False
    if isinstance(type_, (sa.Integer, sa.Float, sa.Numeric)):
        return 0
    if isinstance(type_, (sa.DateTime, sa.Date)):
        return datetime.now(timezone.utc)
    if isinstance(type_, sa.JSON):
        return {}
    if isinstance(type_, (sa.Uuid, postgresql.UUID)):
        return uuid.uuid4()
    return "x"


def _insert(con, table: str, **values) -> uuid.UUID:
    """Insert one row, giving every other NOT NULL column a neutral value."""
    reflected = sa.Table(table, sa.MetaData(), autoload_with=con)
    row = dict(values)
    row.setdefault("id", uuid.uuid4())
    for column in reflected.columns:
        if column.name in row or column.nullable or column.server_default is not None:
            continue
        row[column.name] = _neutral(column.type)
    con.execute(reflected.insert().values(**row))
    return row["id"]


def _seed_chain(con, label: str):
    """A task with a conversation, a message and an intervention request."""
    task = _insert(con, "tasks", task_id=f"AIR-{label}", title=label, owner_email="o@x")
    conversation = _insert(
        con, "agent_conversations", channel="TASK_INBOX", channel_conversation_id=f"task:{label}",
        task_id=task, owner_email="o@x",
    )
    message = _insert(con, MESSAGES_TABLE, conversation_id=conversation, task_id=task,
                      role="assistant", content=label)
    request = _insert(con, "intervention_requests", request_id=f"IR-{label}", task_id=task,
                      conversation_id=conversation, owner_email="o@x", reason_code="UNKNOWN",
                      problem_summary=label)
    return {"task": task, "conversation": conversation, "message": message, "request": request}


def _count(con, table: str, where: str = "TRUE") -> int:
    return int(con.exec_driver_sql(f"SELECT COUNT(*) FROM {table} WHERE {where}").scalar())


def _foreign_keys(engine) -> Dict[str, dict]:
    """Single-column foreign keys per relationship, read straight from pg_catalog.

    Deliberately independent of the migration's own classifier, so the assertions below do not
    grade the code against itself. `confdeltype` 'c' is ON DELETE CASCADE.
    """
    query = """
        SELECT c.conname, c.confrelid::regclass::text, ra.attname, c.confdeltype
        FROM pg_constraint c
        JOIN pg_attribute a  ON a.attrelid  = c.conrelid  AND a.attnum  = c.conkey[1]
        JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1]
        WHERE c.contype = 'f' AND c.conrelid = %s::regclass
          AND array_length(c.conkey, 1) = 1 AND a.attname = %s
    """
    found = {}
    with engine.connect() as con:
        for key, (table, column, referred, _name) in BY_KEY.items():
            correct, incorrect = [], []
            for name, target, target_column, action in con.exec_driver_sql(query, (table, column)):
                row = {"name": name, "references": f"{target}.{target_column}", "on_delete": action}
                is_correct = target == referred and target_column == "id" and action == "c"
                (correct if is_correct else incorrect).append(row)
            found[key] = {"correct": correct, "incorrect": incorrect}
    return found


def _constraint_oids(engine) -> Dict[str, int]:
    with engine.connect() as con:
        rows = con.exec_driver_sql(
            "SELECT conname, oid FROM pg_constraint WHERE contype = 'f'"
        ).fetchall()
    return {name: oid for name, oid in rows}


def _assert_one_correct_foreign_key_each(engine):
    for key, state in _foreign_keys(engine).items():
        assert len(state["correct"]) == 1, f"{key}: expected exactly one correct cascade FK, got {state}"
        assert not state["incorrect"], f"{key}: an incorrect FK is still present: {state['incorrect']}"


def _upgrade_head(engine):
    result = _alembic(engine.database_name, "upgrade", "head")
    assert result.returncode == 0, f"upgrade head failed:\n{result.stdout}\n{result.stderr}"


def _prove_the_database_now_cascades(engine):
    """The behaviour the constraints exist for, checked by actually deleting."""
    with engine.begin() as con:
        gone = _seed_chain(con, "GONE")
        kept = _seed_chain(con, "KEPT")
        # Deleting a task takes its whole Inbox with it ...
        con.execute(sa.text("DELETE FROM tasks WHERE id = :id"), {"id": gone["task"]})
        assert _count(con, "agent_conversations", f"id = '{gone['conversation']}'") == 0
        assert _count(con, MESSAGES_TABLE, f"id = '{gone['message']}'") == 0
        assert _count(con, "intervention_requests", f"id = '{gone['request']}'") == 0
        # ... and deleting a conversation takes its messages and requests, never its task.
        con.execute(sa.text("DELETE FROM agent_conversations WHERE id = :id"), {"id": kept["conversation"]})
        assert _count(con, MESSAGES_TABLE, f"id = '{kept['message']}'") == 0
        assert _count(con, "intervention_requests", f"id = '{kept['request']}'") == 0
        assert _count(con, "tasks", f"id = '{kept['task']}'") == 1


@needs_postgres
class TestMigrationOnPostgres:
    def test_missing_foreign_keys_are_added_to_a_chain_built_database(self, pg):
        _upgrade_head(pg)
        _assert_one_correct_foreign_key_each(pg)
        _prove_the_database_now_cascades(pg)

    def test_a_correct_cascade_foreign_key_is_left_exactly_as_it_is(self, pg):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT my_own_name "
                "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
            )
        before = _constraint_oids(pg)["my_own_name"]

        _upgrade_head(pg)

        assert _constraint_oids(pg).get("my_own_name") == before, "a valid constraint must not be rebuilt"
        assert migration.CASCADES[0][3] not in _constraint_oids(pg), "no duplicate may be added beside it"
        _assert_one_correct_foreign_key_each(pg)

    @pytest.mark.parametrize("action", ["", "ON DELETE RESTRICT", "ON DELETE SET NULL", "ON DELETE NO ACTION"])
    def test_a_foreign_key_without_cascade_is_replaced(self, pg, action):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT stale_no_cascade "
                f"FOREIGN KEY (task_id) REFERENCES tasks(id) {action}"
            )

        _upgrade_head(pg)

        assert "stale_no_cascade" not in _constraint_oids(pg)
        _assert_one_correct_foreign_key_each(pg)
        _prove_the_database_now_cascades(pg)

    def test_a_foreign_key_to_the_wrong_table_is_replaced(self, pg):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT points_at_projects "
                "FOREIGN KEY (task_id) REFERENCES projects(id) ON DELETE CASCADE"
            )
            con.exec_driver_sql(
                f"ALTER TABLE {MESSAGES_TABLE} ADD CONSTRAINT message_points_at_tasks "
                "FOREIGN KEY (conversation_id) REFERENCES tasks(id) ON DELETE CASCADE"
            )

        _upgrade_head(pg)

        oids = _constraint_oids(pg)
        assert "points_at_projects" not in oids and "message_points_at_tasks" not in oids
        _assert_one_correct_foreign_key_each(pg)
        _prove_the_database_now_cascades(pg)

    def test_a_correct_foreign_key_beside_a_stale_one_keeps_the_good_and_drops_the_stale(self, pg):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE intervention_requests ADD CONSTRAINT stale_duplicate "
                "FOREIGN KEY (task_id) REFERENCES tasks(id)"
            )
        good_before = {n: o for n, o in _constraint_oids(pg).items() if n != "stale_duplicate"}

        _upgrade_head(pg)

        after = _constraint_oids(pg)
        assert "stale_duplicate" not in after
        for name, oid in good_before.items():
            if name.startswith("intervention_requests_task_id"):
                assert after.get(name) == oid, "the original correct constraint must survive untouched"
        _assert_one_correct_foreign_key_each(pg)

    def test_orphans_are_cleaned_before_the_constraints_are_added(self, pg):
        """The new constraints could not validate over orphans, so cleanup has to come first."""
        with pg.begin() as con:
            live = _seed_chain(con, "LIVE")
            gone_task = uuid.uuid4()
            orphan_conversation = _insert(
                con, "agent_conversations", channel="TASK_INBOX", channel_conversation_id="task:GONE",
                task_id=gone_task, owner_email="o@x",
            )
            _insert(con, MESSAGES_TABLE, conversation_id=orphan_conversation, task_id=gone_task,
                    role="assistant", content="orphan")
            _insert(con, MESSAGES_TABLE, conversation_id=uuid.uuid4(), task_id=live["task"],
                    role="assistant", content="dangling")
            console = _insert(
                con, "agent_conversations", channel="DASHBOARD", channel_conversation_id="console",
                task_id=None, owner_email=None,
            )
            _insert(con, MESSAGES_TABLE, conversation_id=console, task_id=None,
                    role="user", content="console message")

        _upgrade_head(pg)

        with pg.connect() as con:
            keys = {r[0] for r in con.exec_driver_sql("SELECT channel_conversation_id FROM agent_conversations")}
            contents = {r[0] for r in con.exec_driver_sql(f"SELECT content FROM {MESSAGES_TABLE}")}
            assert keys == {"task:LIVE", "console"}, "only the orphaned conversation goes"
            assert contents == {"LIVE", "console message"}
            assert _count(con, "tasks") == 1, "tasks are never touched"
            assert _count(con, "agent_conversations", "task_id IS NULL") == 1, "the console conversation survives"
        _assert_one_correct_foreign_key_each(pg)

    def test_upgrading_again_changes_nothing(self, pg):
        _upgrade_head(pg)
        before = _constraint_oids(pg)
        result = _alembic(pg.database_name, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert _constraint_oids(pg) == before, "a second upgrade must not drop, rebuild or add anything"

    def test_replacing_constraints_is_stable_across_a_full_round_trip(self, pg):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT stale_no_cascade "
                "FOREIGN KEY (task_id) REFERENCES tasks(id)"
            )
        _upgrade_head(pg)
        _assert_one_correct_foreign_key_each(pg)

        result = _alembic(pg.database_name, "downgrade", "6_task_context")
        assert result.returncode == 0, f"downgrade failed:\n{result.stdout}\n{result.stderr}"
        removed = {n for _k, _t, _c, _r, n in RELATIONSHIPS} & set(_constraint_oids(pg))
        assert not removed, f"the downgrade must drop what the upgrade created: {removed}"

        _upgrade_head(pg)
        _assert_one_correct_foreign_key_each(pg)
        _prove_the_database_now_cascades(pg)

    def test_the_downgrade_leaves_a_pre_existing_valid_constraint_in_place(self, pg):
        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT mine_not_the_migrations "
                "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
            )
        _upgrade_head(pg)
        assert _alembic(pg.database_name, "downgrade", "6_task_context").returncode == 0
        assert "mine_not_the_migrations" in _constraint_oids(pg)

    def test_the_downgrade_does_not_drop_an_unrelated_constraint_sharing_the_name(self, pg):
        """Matched by what it constrains as well as by name."""
        table, column, _referred, name = BY_KEY["conv_task"]
        with pg.begin() as con:
            con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN other_ref uuid")
            con.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (other_ref) REFERENCES tasks(id)"
            )
        # `name` is taken by another column, so task_id is reported and left as it was; the
        # upgrade must still succeed and must not disturb the constraint that holds the name.
        _upgrade_head(pg)
        assert name in _constraint_oids(pg)
        assert _alembic(pg.database_name, "downgrade", "6_task_context").returncode == 0
        assert name in _constraint_oids(pg), "the unrelated constraint must still be there"

    def test_a_wrong_foreign_key_survives_when_the_replacement_name_is_taken(self, pg):
        """Nothing may be dropped if its replacement cannot be created."""
        table, _column, _referred, name = BY_KEY["conv_task"]
        with pg.begin() as con:
            con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN other_ref uuid")
            con.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (other_ref) REFERENCES tasks(id)"
            )
            con.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT stale_no_cascade "
                "FOREIGN KEY (task_id) REFERENCES tasks(id)"
            )
        before = _constraint_oids(pg)

        _upgrade_head(pg)

        after = _constraint_oids(pg)
        # Other relationships may gain their missing constraints, but nothing that existed
        # may be dropped or rebuilt: the wrong constraint stays because it cannot be replaced.
        assert {n: after.get(n) for n in before} == before, "nothing that existed may be dropped"
        assert _alembic(pg.database_name, "downgrade", "6_task_context").returncode == 0
        assert {"stale_no_cascade", name} <= set(_constraint_oids(pg))

    def test_a_failure_while_replacing_rolls_everything_back(self, pg, monkeypatch):
        """Drop and create share one transaction, so a failed create cannot strand the table."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        with pg.begin() as con:
            con.exec_driver_sql(
                "ALTER TABLE agent_conversations ADD CONSTRAINT stale_no_cascade "
                "FOREIGN KEY (task_id) REFERENCES tasks(id)"
            )
        before = _constraint_oids(pg)

        def explode(*_args, **_kwargs):
            raise RuntimeError("simulated failure after the drop")

        monkeypatch.setattr(migration.op, "create_foreign_key", explode)

        with pg.connect() as con:
            transaction = con.begin()
            with Operations.context(MigrationContext.configure(con)):
                with pytest.raises(RuntimeError, match="simulated failure"):
                    migration.upgrade()
            transaction.rollback()

        assert _constraint_oids(pg) == before, "the original constraint must still exist after the rollback"
