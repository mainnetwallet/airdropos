"""Authentication endpoints.

The operator account is bootstrapped from configuration. No credentials are
hardcoded and no default password exists: if `OPERATOR_PASSWORD_HASH` is unset,
the bootstrap endpoint refuses to create an account rather than falling back to
something guessable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.models import User, UserSession
from backend.app.security.auth import (
    ALL_SCOPES,
    DEFAULT_OPERATOR_SCOPES,
    SESSION_TTL_HOURS,
    Principal,
    enforce_rate_limit,
    generate_token,
    get_principal,
    hash_token,
    is_valid_password_hash,
    require_system_admin,
    verify_against_dummy,
    verify_password,
    write_audit,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: str
    scopes: List[str]
    email: str


@router.post("/auth/login", tags=["auth"])
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Exchange operator credentials for a session token.

    The raw token is returned exactly once and stored only as a SHA-256 hash.
    """
    enforce_rate_limit(request, "auth")

    user = await db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    # Same work whether the account exists or not, so timing does not reveal valid emails.
    if user is None:
        verify_against_dummy(payload.password)
    if user is None or not verify_password(payload.password, user.hashed_password):
        # The client only ever sees "Invalid credentials". The server log says which it was.
        reason = "no account with that email" if user is None else "password does not match the stored hash"
        logger.warning("Failed login attempt for %s from %s: %s", payload.email,
                       request.client.host if request.client else "unknown", reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid credentials."},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Account is inactive."},
        )

    raw_token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(
        hours=settings.SESSION_TTL_HOURS or SESSION_TTL_HOURS
    )

    db.add(
        UserSession(
            user_id=user.id,
            token=hash_token(raw_token),
            expires_at=expires,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:500],
            scopes=sorted(DEFAULT_OPERATOR_SCOPES if user.is_superuser else _readonly(user)),
        )
    )

    principal = Principal(
        user_id=str(user.id), email=user.email,
        scopes=frozenset(DEFAULT_OPERATOR_SCOPES if user.is_superuser else _readonly(user)),
    )
    await write_audit(
        db, principal, "AUTH_LOGIN", "user", str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return SessionResponse(
        token=raw_token,
        expires_at=expires.isoformat(),
        scopes=sorted(DEFAULT_OPERATOR_SCOPES if user.is_superuser else _readonly(user)),
        email=user.email,
    )


def _readonly(user: User) -> List[str]:
    from backend.app.security.auth import READONLY_SCOPES

    return sorted(READONLY_SCOPES) if user.is_active else []


@router.post("/auth/logout", tags=["auth"])
async def logout(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Revoke the current session."""
    if principal.session_token:
        session = await db.scalar(
            select(UserSession).where(UserSession.token == hash_token(principal.session_token))
        )
        if session is not None:
            session.revoked_at = datetime.now(timezone.utc)
            await db.commit()
        # End any live event streams opened with this session right away.
        from backend.app.api.v1.endpoints.ws import bus

        await bus.close_session(principal.session_token)
    return {"revoked": True}


@router.get("/auth/me", tags=["auth"])
async def me(principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Identity and granted scopes for the current session."""
    return {
        "email": principal.email,
        "user_id": principal.user_id,
        "scopes": sorted(principal.scopes),
        "is_operator": principal.is_operator,
        "channel": principal.channel,
    }


@router.post("/auth/bootstrap", tags=["auth"])
async def bootstrap_operator(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Create the operator account from configuration, once.

    Refuses if an account already exists or if `OPERATOR_PASSWORD_HASH` is unset.
    There is deliberately no default password.
    """
    if not settings.AUTH_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Authentication is disabled. Bootstrap is unavailable."},
        )

    existing = await db.scalar(select(User).limit(1))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "An account already exists. Use /auth/login instead."},
        )

    if not settings.OPERATOR_PASSWORD_HASH:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "OPERATOR_PASSWORD_HASH is not configured.",
                "hint": (
                    "Generate a hash with: python -c \"from backend.app.security.auth "
                    "import hash_password; print(hash_password('your-password'))\" "
                    "and set it in .env. No default password exists by design."
                ),
            },
        )

    if not is_valid_password_hash(settings.OPERATOR_PASSWORD_HASH):
        # A plaintext password, a truncated paste, or a deliberately cheap hash
        # must never become the operator credential.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "OPERATOR_PASSWORD_HASH is not a valid pbkdf2$rounds$salt$hash value.",
                "hint": "Regenerate it with hash_password() and set it in .env.",
            },
        )

    user = User(
        email=settings.OPERATOR_EMAIL.strip().lower(),
        hashed_password=settings.OPERATOR_PASSWORD_HASH,
        full_name="Operator",
        is_active=True,
        is_superuser=True,
    )
    db.add(user)
    await db.flush()

    await write_audit(
        db,
        Principal(user_id=str(user.id), email=user.email, scopes=frozenset(ALL_SCOPES),
                  channel="system", is_operator=True),
        "AUTH_BOOTSTRAP", "user", str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    logger.info("Operator account bootstrapped: %s", user.email)
    return {
        "created": True,
        "email": user.email,
        "note": "Log in via POST /api/v1/auth/login to obtain a session token.",
    }
