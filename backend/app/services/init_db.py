"""Database Initialization Script.

Creates all tables and seeds baseline system settings.

Run with: python -m backend.app.services.init_db

This is NOT Alembic. Alembic migrations remain the authoritative path for
schema changes in production; this script exists for first-run bootstrapping.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.core.logging_setup import configure_logging
from backend.app.database import schema_sync
from backend.app.database.session import AsyncSessionLocal, Base, engine
from backend.app.models import SystemSetting
from backend.app.terminal.events import publish_success

# The shared configuration, not a private basicConfig: this script runs as its own
# process, and it has to write to the same rotating diagnostic log and the same
# event stream as the API and the worker, or the launcher's startup record has a
# hole where the database was created.
configure_logging()
logger = logging.getLogger("airdropos.init_db")

# Baseline settings. Secrets are stored encrypted in a real deployment; these
# are non-sensitive defaults only.
DEFAULT_SETTINGS = [
    {
        "key": "llm.model_id",
        "value": {"model_id": "gpt-4o", "note": "Single fixed model. Changing this changes the only model."},
        "is_secret": False,
        "description": "The one active LLM model identifier",
    },
    {
        "key": "limits.transaction",
        "value": {
            "max_transaction_value_usd": 50.0,
            "max_daily_spend_usd": 25.0,
            "max_concurrent_tasks": 5,
        },
        "is_secret": False,
        "description": "Deterministic spend and concurrency ceilings",
    },
    {
        "key": "discovery.sources",
        "value": {
            "enabled": ["OFFICIAL_SITE", "OFFICIAL_DOCS", "ANNOUNCEMENT_CHANNELS"],
            "interval_minutes": 60,
        },
        "is_secret": False,
        "description": "Configured airdrop discovery sources and interval",
    },
]


async def main() -> None:
    logger.info("Creating all tables from metadata...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tables created.")

    # create_all never alters an existing table, so a database made by an older version can lack
    # columns the models now declare (-> "no such column"). Add them, SQLite only, additively.
    if schema_sync.is_sqlite():
        async with engine.connect() as conn:
            missing = await conn.run_sync(schema_sync.find_missing)
        if missing:
            backup = schema_sync.backup_sqlite_file()
            if backup:
                logger.info("Database backup before schema update: %s", backup)
            async with engine.begin() as conn:
                await conn.run_sync(schema_sync.apply_missing, missing)
            logger.info(
                "Added %d missing column(s): %s",
                len(missing), ", ".join(f"{t}.{c}" for t, c, _ in missing),
            )
        else:
            logger.info("Database schema is up to date.")

    async with AsyncSessionLocal() as db:
        for spec in DEFAULT_SETTINGS:
            existing = await db.scalar(select(SystemSetting).where(SystemSetting.key == spec["key"]))
            if existing is None:
                db.add(SystemSetting(**spec))
                logger.info("Seeded setting '%s'", spec["key"])
            else:
                logger.info("Setting '%s' already present; skipped", spec["key"])
        await db.commit()

    await engine.dispose()
    logger.info("Database initialization complete at %s", datetime.now(timezone.utc).isoformat())
    # A milestone the caller has actually reached, not an inference drawn by the
    # dashboard: the schema exists and the baseline settings are committed.
    publish_success("airdropos.init_db", "Database initialization complete")


if __name__ == "__main__":
    asyncio.run(main())
