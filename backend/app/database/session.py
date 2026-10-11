"""AirdropOS Database Engine & Session Management

Provides SQLAlchemy 2.0 AsyncEngine, async_sessionmaker, and dependency injection helpers.
"""

from datetime import datetime, timezone
from typing import AsyncGenerator
import uuid

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.app.core.config import settings



@compiles(JSONB, "sqlite")
def _render_jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """Models use PostgreSQL JSONB. SQLite (run.py --sqlite) has no JSONB, so use JSON there.

    Registered explicitly because not every SQLAlchemy version does this fallback itself.
    """
    return "JSON"


_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

# Initialize async engine. SQLite uses its own pool, which rejects pool_size/max_overflow.
_engine_kwargs = {} if _IS_SQLITE else {
    "pool_size": settings.DB_POOL_SIZE,
    "max_overflow": settings.DB_MAX_OVERFLOW,
}
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.LOG_LEVEL.upper() == "DEBUG",
    pool_pre_ping=True,
    **_engine_kwargs,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base declarative class with standard UUID primary key and timestamp tracking."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session with automatic cleanup."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
