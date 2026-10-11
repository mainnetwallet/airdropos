"""Tests that the Alembic chain stays consistent with the ORM models.

`1_initial_schema.py` was hand-written and drifted from `backend/app/models/` in
three places: `wallets` had no encryption columns at all, `knowledge_relationships`
described a memory-to-memory edge the ORM does not use, and `memory_records` was
missing five columns `MemoryManager` touches on every write.

Nothing caught it, because every test builds its database from
`Base.metadata.create_all` rather than from the migration chain. A deployment
built from the migrations would have failed at runtime. These tests check the two
representations agree, which is the property that was missing.

They are static checks on the migration source, not a database round-trip: the
project has no PostgreSQL instance available in CI, and a test that silently
skipped would be worse than an honest static comparison.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from backend.app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "backend" / "app" / "migrations" / "versions"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if not p.name.startswith("__"))


def _orm_columns(table: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table].columns}


def _migration_columns(source: str, table: str) -> set[str] | None:
    """Columns the migration gives `table`, or None if it never creates it.

    Reads `op.create_table(<table>, ...)` and `op.add_column(<table>, ...)`
    calls. Deliberately a source scan: it runs without a database and fails
    loudly if the migration shape changes, rather than passing vacuously.
    """
    if not re.search(rf'["\']{table}["\']', source):
        return None

    columns: set[str] = set()
    for block in re.finditer(
        rf'(?:op\.create_table|op\.add_column)\(\s*["\']{table}["\']\s*,(.*?)\n\s*\)\s*\n',
        source,
        re.DOTALL,
    ):
        columns.update(re.findall(r'sa\.Column\(\s*["\']([a-zA-Z_]+)["\']', block.group(1)))

    # `create_table` puts the primary key inline; catch columns declared directly
    # on the call as well as ones matched above.
    for match in re.finditer(
        rf'op\.create_table\(\s*["\']{table}["\'],(.*?)\n    \)', source, re.DOTALL
    ):
        columns.update(re.findall(r'sa\.Column\(\s*["\']([a-zA-Z_]+)["\']', match.group(1)))

    return columns


# ======================================================================
# Revision chain
# ======================================================================
def test_migrations_are_discoverable():
    files = _migration_files()
    assert len(files) >= 2, "expected at least an initial schema and a corrective one"


def test_revision_chain_is_linear_and_terminates():
    """Exactly one root and no dangling parent references."""
    modules = [_load(p) for p in _migration_files()]
    revisions = {m.revision for m in modules}
    roots = [m for m in modules if m.down_revision is None]

    assert len(roots) == 1, f"expected exactly one root migration, found {len(roots)}"

    for module in modules:
        if module.down_revision is not None:
            assert module.down_revision in revisions, (
                f"{module.revision} points at missing parent {module.down_revision}"
            )


def test_revisions_are_unique():
    modules = [_load(p) for p in _migration_files()]
    revisions = [m.revision for m in modules]
    assert len(set(revisions)) == len(revisions), "duplicate revision id"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.stem)
def test_every_migration_defines_both_directions(path):
    module = _load(path)
    assert callable(module.upgrade), f"{path.name} has no upgrade()"
    assert callable(module.downgrade), f"{path.name} has no downgrade()"


# ======================================================================
# Schema agreement
# ======================================================================
def _all_migration_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _migration_files())


@pytest.mark.parametrize(
    "table,required",
    [
        # The columns the Wallet Management API writes. Without these, importing
        # a wallet against a migrated database fails outright.
        (
            "wallets",
            [
                "encrypted_secret",
                "encryption_iv",
                "encryption_tag",
                "derivation_path",
                "notes",
            ],
        ),
        # MemoryManager reads and writes these on every remember/recall.
        (
            "memory_records",
            [
                "is_inference",
                "access_count",
                "last_verified_at",
                "related_entities",
                "review_policy",
            ],
        ),
        # The typed-edge shape the Knowledge Graph Service writes.
        (
            "knowledge_relationships",
            [
                "source_entity_type",
                "source_entity_id",
                "target_entity_type",
                "target_entity_id",
                "relationship_type",
                "weight",
                "properties",
            ],
        ),
    ],
)
def test_migrations_provide_every_column_the_models_declare(table, required):
    source = _all_migration_source()
    columns = _migration_columns(source, table)
    assert columns is not None, f"no migration mentions '{table}'"

    missing = [c for c in required if c not in columns]
    assert not missing, (
        f"migrations never create {table}.{missing}; the ORM declares them "
        f"({sorted(_orm_columns(table))}), so a migrated database would reject "
        f"the application's own writes"
    )


def test_knowledge_relationships_final_shape_replaces_the_obsolete_one():
    """The typed-edge shape must supersede the memory-to-memory one.

    Migration 1 legitimately still declares `from_memory_id` -- it is history,
    and rewriting a shipped migration to pretend otherwise would break every
    existing deployment. What matters is the state a fresh `upgrade head` lands
    in: the last migration to *create* this table must be the one that defines
    the typed-edge columns.

    Scoped to `op.create_table` bodies so a prose mention of the old column, or
    the `downgrade` that restores it, does not read as a live definition.
    """
    def _creates(source: str) -> list[str]:
        """Bodies of every `op.create_table("knowledge_relationships", ...)` call.

        Matches the closing paren by indentation rather than a fixed width, so a
        create nested inside an `if` (as the rebuild in migration 2 is) is found
        too.
        """
        return [
            match.group(1)
            for match in re.finditer(
                r'op\.create_table\(\s*["\']knowledge_relationships["\'],(.*?)\n[ \t]+\)',
                source,
                re.DOTALL,
            )
        ]

    last_creation = None
    for path in _migration_files():
        source = path.read_text(encoding="utf-8")
        # Only the upgrade body establishes the schema a fresh `upgrade head`
        # lands in. The downgrade deliberately restores the old shape, so it
        # must not be read as a live definition.
        parts = re.split(r"^def downgrade\(\)", source, flags=re.MULTILINE)
        upgrade_body = parts[0]
        blocks = _creates(upgrade_body)
        if blocks:
            last_creation = (path, blocks[-1])

    assert last_creation is not None, "no migration creates knowledge_relationships"
    path, block = last_creation

    assert "from_memory_id" not in block, (
        f"{path.name} is the migration that creates knowledge_relationships but "
        f"still declares the obsolete memory-to-memory shape"
    )
    for required in ("source_entity_type", "target_entity_type", "weight", "properties"):
        assert required in block, f"{path.name} omits {required}"


def test_the_obsolete_shape_is_dropped_not_merged():
    """A rebuild must drop the old table before creating the new one.

    Creating the typed columns on top of the memory-to-memory table would leave
    both shapes in one table, which is neither of them.
    """
    modules = [_load(p) for p in _migration_files()]
    rebuilders = [
        (m, Path(m.__file__).read_text(encoding="utf-8"))
        for m in modules
        if "drop_table" in Path(m.__file__).read_text(encoding="utf-8")
        and "knowledge_relationships" in Path(m.__file__).read_text(encoding="utf-8")
    ]
    assert rebuilders, "no migration rebuilds knowledge_relationships"
    for module, source in rebuilders:
        assert re.search(r'op\.drop_table\(\s*["\']knowledge_relationships["\']', source), (
            f"{module.revision} must drop the obsolete table before recreating it"
        )


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.stem)
def test_migrations_are_reversible(path):
    """A downgrade must not raise NotImplementedError.

    Losing data on downgrade is acceptable and is documented in the module
    docstring; refusing to downgrade at all is not.
    """
    module = _load(path)
    source = path.read_text(encoding="utf-8")
    body = source.split("def downgrade()", 1)
    assert len(body) == 2, f"{path.name} has no downgrade body"
    assert "NotImplementedError" not in body[1], (
        f"{path.name} refuses to downgrade"
    )
    assert module.downgrade is not None