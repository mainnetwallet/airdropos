"""Minimal test isolation: every test gets its own in-memory database.

The earlier suite (and its fixtures) was removed from the tree, so this is deliberately small: it
pins one SQLite connection with StaticPool (otherwise each new connection would see a different
empty `:memory:` database) and points every session factory at it, so no test can touch the
operator's real database.
"""

import os
import tempfile
from pathlib import Path

_STATE_DIR = Path(tempfile.mkdtemp(prefix="airdropos-test-state-"))
os.environ.setdefault("EMERGENCY_STOP_STATE_FILE", str(_STATE_DIR / "emergency_stop.json"))
os.environ.setdefault("SPEND_LEDGER_PATH", str(_STATE_DIR / "spend_ledger.db"))

import pytest  # noqa: E402


@pytest.fixture
async def factory(monkeypatch):
    """A session factory over a fresh in-memory database built from the live models."""
    import backend.app.database.session as session_module
    from backend.app.database.session import Base
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import StaticPool

    import backend.app.models  # noqa: F401  (register every table on Base.metadata)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def make(*args, **kwargs):
        return AsyncSession(engine, expire_on_commit=False)

    monkeypatch.setattr(session_module, "AsyncSessionLocal", make)
    try:  # modules that bound the name at import time
        import backend.app.scheduler.worker as worker_module

        monkeypatch.setattr(worker_module, "AsyncSessionLocal", make)
    except Exception:
        pass
    yield make
    await engine.dispose()
