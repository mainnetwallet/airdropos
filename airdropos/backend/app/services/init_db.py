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

from backend.app.database.session import AsyncSessionLocal, Base, engine
from backend.app.models import SystemSetting

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
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


if __name__ == "__main__":
    asyncio.run(main())
