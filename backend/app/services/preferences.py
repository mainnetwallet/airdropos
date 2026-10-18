"""Operator preferences that both the API process and the worker must agree on.

The X execution preference lives here rather than in a module-level variable
because AirdropOS runs its API and its worker as SEPARATE processes with
separate registries and separate memory. A preference held in a Python global
would be set in one and ignored by the other, so tasks planned by the API would
be routed differently from tasks planned by the worker.

It is stored in the existing ``system_settings`` table, which is already
audited, already scoped by the endpoint that writes it, and already survives a
restart.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import SystemSetting

logger = logging.getLogger(__name__)

X_EXECUTION_PREFERENCE = "x.execution_preference"
X_BROWSER_PROFILE_ID = "x.browser_profile_id"
X_DUPLICATE_WINDOW_HOURS = "x.duplicate_window_hours"


async def get_preference(db: AsyncSession, key: str, default: str = "") -> str:
    """Read one preference. An unreadable or corrupt row returns the default."""
    try:
        row = await db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    except Exception as exc:
        logger.warning("Could not read setting %s: %s", key, type(exc).__name__)
        return default
    if row is None:
        return default
    value = row.value
    if isinstance(value, dict):
        value = value.get("value", "")
    return str(value) if value not in (None, "") else default


async def set_preference(
    db: AsyncSession, key: str, value: str, *, description: str = ""
) -> SystemSetting:
    """Create or update one preference. Never stores a secret."""
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is None:
        row = SystemSetting(key=key, value=value, is_secret=False, description=description)
        db.add(row)
    else:
        row.value = value
        if description:
            row.description = description
    await db.flush()
    return row


async def all_preferences(db: AsyncSession) -> Dict[str, str]:
    """Every X preference at once, for the settings page."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key.like("x.%"))
    )
    out: Dict[str, str] = {}
    for row in result.scalars().all():
        value: Optional[Any] = row.value
        if isinstance(value, dict):
            value = value.get("value", "")
        out[str(row.key)] = "" if value is None else str(value)
    return out