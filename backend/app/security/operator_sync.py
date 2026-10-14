"""Keep the operator account in step with OPERATOR_EMAIL / OPERATOR_PASSWORD_HASH.

`python run.py --set-password` writes the new hash to `.env`, but login checks
`users.hashed_password` in the database. Bootstrap copies the hash across only once, so
after the account existed a changed password (or email) never reached the database and
every login failed with "Invalid credentials".

`apply_operator_credentials()` makes the database match the configuration. It runs
- at API startup, and
- from `run.py --set-password` (`python -m backend.app.security.operator_sync`),
so the new password works immediately, with or without a restart.

The operator is a single account: `.env` is the authority for its email and password.
"""

from __future__ import annotations

import logging
import sys
from typing import Tuple

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.database.session import session_scope
from backend.app.models import User
from backend.app.security.auth import is_valid_password_hash

logger = logging.getLogger(__name__)


def normalized_operator_email() -> str:
    return (settings.OPERATOR_EMAIL or "").strip().strip("'\"").lower()


async def apply_operator_credentials() -> Tuple[str, str]:
    """Create or update the operator row. Returns (status, detail); raises on DB errors."""
    if not settings.AUTH_ENABLED:
        return "skipped", "AUTH_ENABLED is false"
    configured = (settings.OPERATOR_PASSWORD_HASH or "").strip().strip("'\"")
    if not configured:
        return "skipped", "OPERATOR_PASSWORD_HASH is empty (run: python run.py --set-password)"
    if not is_valid_password_hash(configured):
        return "invalid", "OPERATOR_PASSWORD_HASH is not a valid pbkdf2$rounds$salt$hash value"
    email = normalized_operator_email()
    if not email:
        return "invalid", "OPERATOR_EMAIL is empty"

    async with session_scope() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            users = (await db.scalars(select(User).limit(2))).all()
            if len(users) == 1:
                # One account and it is not under the configured email: it is the operator
                # whose email was changed. Follow the configuration instead of orphaning it.
                user = users[0]
                old_email = user.email
                user.email = email
                logger.info("Operator email changed from %s to %s", old_email, email)
        if user is None:
            db.add(User(email=email, hashed_password=configured, full_name="Operator",
                        is_active=True, is_superuser=True))
            return "created", email
        changed = user.hashed_password != configured or not user.is_active or not user.is_superuser
        user.hashed_password = configured
        user.is_active = True
        user.is_superuser = True
        return ("updated" if changed else "unchanged"), email


async def sync_operator_password() -> None:
    """Startup hook. Never raises: startup must not fail because of this."""
    try:
        status, detail = await apply_operator_credentials()
        if status in ("created", "updated"):
            logger.info("Operator account %s: %s", status, detail)
        elif status == "invalid":
            logger.error("Operator account not synced: %s. Re-run: python run.py --set-password", detail)
    except Exception:
        logger.exception("Could not sync operator account")


def _main() -> int:
    import asyncio

    try:
        status, detail = asyncio.run(apply_operator_credentials())
    except Exception as exc:  # DB down, tables not created yet, ...
        print(f"NOT_APPLIED: {type(exc).__name__}: {exc}")
        return 2
    print(f"{status.upper()}: {detail}")
    return 0 if status in ("created", "updated", "unchanged") else 1


if __name__ == "__main__":
    sys.exit(_main())
