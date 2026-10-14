"""Keep the operator's stored password hash in step with OPERATOR_PASSWORD_HASH.

`python run.py --set-password` writes the new hash to `.env` only. Login, however,
checks `users.hashed_password` in the database, and `/auth/bootstrap` copies the hash
across exactly once (when no account exists). Once the account exists, changing the
password in `.env` therefore never reached the database and the new password was
rejected. This runs at startup and applies the configured hash to the operator row.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.database.session import session_scope
from backend.app.models import User
from backend.app.security.auth import is_valid_password_hash

logger = logging.getLogger(__name__)


async def sync_operator_password() -> None:
    """Apply OPERATOR_PASSWORD_HASH to the operator account if it differs. Never raises."""
    if not settings.AUTH_ENABLED:
        return
    configured = (settings.OPERATOR_PASSWORD_HASH or "").strip()
    if not configured:
        return
    if not is_valid_password_hash(configured):
        logger.error("OPERATOR_PASSWORD_HASH is not a valid pbkdf2$rounds$salt$hash value; "
                     "not syncing. Re-run: python run.py --set-password")
        return
    try:
        async with session_scope() as db:
            user = await db.scalar(
                select(User).where(User.email == settings.OPERATOR_EMAIL.lower().strip())
            )
            if user is None:
                # No account yet: /auth/bootstrap creates it from the same hash.
                return
            if user.hashed_password != configured:
                user.hashed_password = configured
                user.is_active = True
                await db.commit()
                logger.info("Operator password updated from OPERATOR_PASSWORD_HASH (%s)", user.email)
    except Exception:  # startup must not fail because of this
        logger.exception("Could not sync operator password")
