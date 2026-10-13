"""Reconcile the initial schema with the ORM models.

Revision ID: 2_reconcile_schema_with_models
Revises: 1_initial_schema
Create Date: 2026-10-11

`1_initial_schema.py` was hand-written and drifted from the models in three
places. Nothing caught it because the application only ever runs against a
database created from `Base.metadata.create_all` in tests; a real deployment
built from the migration chain would fail on the first write to any of these
tables.

The three drifts:

1. **wallets** is missing every column the Wallet Management API needs:
   `derivation_path`, `encrypted_secret`, `encryption_iv`, `encryption_tag`,
   and `notes`. It also carries a `wallet_id` column with a unique index that
   the ORM has no equivalent of, and `supported_networks` is an ARRAY there
   while the ORM uses JSONB. Without the encryption columns, no wallet could be
   imported at all against a migrated database.

2. **knowledge_relationships** has an unrelated shape: `from_memory_id` /
   `to_memory_id` foreign keys into `memory_records`, with no entity types, no
   weight, and no properties column. The Knowledge Graph Service writes
   typed, weighted edges between arbitrary entities, which this table cannot
   hold. The table is rebuilt rather than altered because every column differs.

3. **memory_records** is missing `access_count`, `is_inference`,
   `last_verified_at`, `related_entities`, and `review_policy`, all of which
   `MemoryManager` reads and writes on every `remember` and `recall`.

`downgrade` reverses each step. For `knowledge_relationships` the reversal
restores the original memory-to-memory shape, which is lossy: the typed edges
this migration stores cannot be expressed in it, so the downgrade drops them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2_reconcile_schema_with_models"
down_revision: Union[str, None] = "1_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    """True when the table already exists with the column.

    Makes the migration safe to run against a database created from
    `Base.metadata.create_all` rather than from the migration chain, which is how
    every development and test database in this project is built.
    """
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # ------------------------------------------------------------------
    # 0. users.telegram_user_id
    # ------------------------------------------------------------------
    # Declared on the model and written by the auth layer, but revision 1 never created it,
    # so any database built through the migration chain could not insert a user at all.
    if "users" in tables and not _has_column("users", "telegram_user_id"):
        op.add_column("users", sa.Column("telegram_user_id", sa.Integer(), nullable=True))
        if "ix_users_telegram_user_id" not in {
            ix["name"] for ix in inspector.get_indexes("users")
        }:
            op.create_index("ix_users_telegram_user_id", "users", ["telegram_user_id"])

    # ------------------------------------------------------------------
    # 0b. user_sessions.updated_at
    # ------------------------------------------------------------------
    # Inherited from Base on the model and written by the auth layer on every session
    # update, but never created. Without it no session can be inserted or touched, so a
    # chain-built database could not authenticate anyone.
    if "user_sessions" in tables and not _has_column("user_sessions", "updated_at"):
        op.add_column(
            "user_sessions", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )

    # ------------------------------------------------------------------
    # 1. wallets: the key-material columns the management API writes.
    # ------------------------------------------------------------------
    # Written as literal calls rather than a loop over a table of names. A loop
    # hides the DDL from grep, from review, and from the migration-consistency
    # tests, which is exactly what let the original drift go unnoticed.
    if "wallets" in tables:
        if not _has_column("wallets", "derivation_path"):
            op.add_column(
                "wallets", sa.Column("derivation_path", sa.String(length=100), nullable=True)
            )
        if not _has_column("wallets", "encrypted_secret"):
            op.add_column(
                "wallets", sa.Column("encrypted_secret", sa.Text(), nullable=True)
            )
        if not _has_column("wallets", "encryption_iv"):
            op.add_column(
                "wallets", sa.Column("encryption_iv", sa.String(length=64), nullable=True)
            )
        if not _has_column("wallets", "encryption_tag"):
            op.add_column(
                "wallets", sa.Column("encryption_tag", sa.String(length=64), nullable=True)
            )
        if not _has_column("wallets", "notes"):
            op.add_column("wallets", sa.Column("notes", sa.Text(), nullable=True))

    # ------------------------------------------------------------------
    # 2. knowledge_relationships: rebuild in the shape the ORM declares.
    # ------------------------------------------------------------------
    # Migration 1 created this as a memory-to-memory edge
    # (from_memory_id -> to_memory_id), which the ORM does not use and the
    # Knowledge Graph Service cannot write. Every column differs, so the table
    # is rebuilt rather than altered.
    if "knowledge_relationships" in tables:
        columns = {c["name"] for c in inspector.get_columns("knowledge_relationships")}
        typed_shape = {"source_entity_type", "target_entity_type", "weight"}
        if not typed_shape.issubset(columns):
            op.drop_table("knowledge_relationships")
            op.create_table(
                "knowledge_relationships",
                sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
                sa.Column("source_entity_type", sa.String(length=50), nullable=False),
                sa.Column("source_entity_id", sa.String(length=64), nullable=False),
                sa.Column("target_entity_type", sa.String(length=50), nullable=False),
                sa.Column("target_entity_id", sa.String(length=64), nullable=False),
                sa.Column("relationship_type", sa.String(length=100), nullable=False),
                sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
                sa.Column("properties", postgresql.JSONB(), nullable=True),
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("now()"),
                ),
                sa.Column(
                    "updated_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("now()"),
                ),
            )
            op.create_index(
                "ix_knowledge_relationships_source_entity_id",
                "knowledge_relationships",
                ["source_entity_id"],
            )
            op.create_index(
                "ix_knowledge_relationships_target_entity_id",
                "knowledge_relationships",
                ["target_entity_id"],
            )

    # ------------------------------------------------------------------
    # 3. memory_records: columns MemoryManager reads and writes.
    # ------------------------------------------------------------------
    if "memory_records" in tables:
        if not _has_column("memory_records", "is_inference"):
            # NOT NULL on a possibly-populated table needs a default for the
            # rows already present.
            op.add_column(
                "memory_records",
                sa.Column("is_inference", sa.Boolean(), nullable=False, server_default="false"),
            )
        if not _has_column("memory_records", "access_count"):
            op.add_column(
                "memory_records",
                sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
            )
        if not _has_column("memory_records", "last_verified_at"):
            op.add_column(
                "memory_records",
                sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
            )
        if not _has_column("memory_records", "related_entities"):
            op.add_column(
                "memory_records",
                sa.Column("related_entities", postgresql.JSONB(), nullable=True),
            )
        if not _has_column("memory_records", "review_policy"):
            op.add_column(
                "memory_records",
                sa.Column("review_policy", postgresql.JSONB(), nullable=True),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "users" in tables and _has_column("users", "telegram_user_id"):
        if "ix_users_telegram_user_id" in {ix["name"] for ix in inspector.get_indexes("users")}:
            op.drop_index("ix_users_telegram_user_id", table_name="users")
        op.drop_column("users", "telegram_user_id")

    if "user_sessions" in tables and _has_column("user_sessions", "updated_at"):
        op.drop_column("user_sessions", "updated_at")

    if "memory_records" in tables:
        for name in (
            "review_policy",
            "related_entities",
            "last_verified_at",
            "access_count",
            "is_inference",
        ):
            if _has_column("memory_records", name):
                op.drop_column("memory_records", name)

    if "knowledge_relationships" in tables:
        columns = {c["name"] for c in inspector.get_columns("knowledge_relationships")}
        if {"source_entity_type", "weight"}.issubset(columns):
            # Lossy: the typed edges stored here cannot be expressed as
            # memory-to-memory pairs, so they are dropped rather than guessed at.
            op.drop_table("knowledge_relationships")
            op.create_table(
                "knowledge_relationships",
                sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
                sa.Column(
                    "from_memory_id",
                    postgresql.UUID(as_uuid=True),
                    sa.ForeignKey("memory_records.id"),
                    nullable=False,
                ),
                sa.Column(
                    "to_memory_id",
                    postgresql.UUID(as_uuid=True),
                    sa.ForeignKey("memory_records.id"),
                    nullable=False,
                ),
                sa.Column("relationship_type", sa.String(length=100), nullable=False),
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("now()"),
                ),
            )

    if "wallets" in tables:
        for name in ("notes", "encryption_tag", "encryption_iv", "encrypted_secret", "derivation_path"):
            if _has_column("wallets", name):
                op.drop_column("wallets", name)