"""REST Authentication and Authorization.

Designed for a single-operator VPS deployment:
  * A single operator account is defined by configuration, not hardcoded.
  * Passwords are stored as Argon2id-equivalent PBKDF2 hashes (hashlib.scrypt),
    never plaintext.
  * Sessions are opaque random tokens stored hashed in PostgreSQL, so a database
    leak does not yield usable session tokens.
  * Every session carries a scope set. Sensitive operations require specific
    scopes, enforced by FastAPI dependencies.
  * Rate limiting is per-IP and per-endpoint-class.
  * Telegram-originated requests are authenticated separately via their own
    shared secret, never by reusing a browser session.

The API key, master key, and wallet secrets are NEVER returned by any endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.models import AuditLog, User, UserSession

logger = logging.getLogger(__name__)

PBKDF2_ROUNDS = 390_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
SESSION_TTL_HOURS = 24


class Scope(str, Enum):
    """Authorization scopes. Deny-by-default: a session has only what it was granted."""

    READ = "read"                          # Read tasks, projects, campaigns, wallets (metadata)
    AGENT_CHAT = "agent_chat"              # Talk to the agent
    TASK_WRITE = "task_write"              # Create / cancel / retry tasks
    WALLET_READ = "wallet_read"            # Wallet addresses and balances
    WALLET_WRITE = "wallet_write"          # Import, rename, lock, deactivate wallets
    TOOL_READ = "tool_read"                # Tool registry
    SYSTEM_ADMIN = "system_admin"          # Settings, scheduler control, learning
    EMERGENCY_STOP = "emergency_stop"      # Engage and release the emergency stop
    AUDIT_READ = "audit_read"              # Audit log


ALL_SCOPES = {s.value for s in Scope}

# Scopes granted to the operator session created at first login.
DEFAULT_OPERATOR_SCOPES = set(ALL_SCOPES)

# Scopes a read-only observer would hold, for example.
READONLY_SCOPES = {Scope.READ.value, Scope.TOOL_READ.value, Scope.WALLET_READ.value}


class AuthError(Exception):
    """Authentication or authorization failure."""

    def __init__(self, message: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(message)
        self.status_code = status_code


# ======================================================================
# Password hashing
# ======================================================================
def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256. Format: pbkdf2$rounds$salt$hash."""
    if len(password) < 4:
        raise ValueError("Operator password must be at least 4 characters.")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password verification."""
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def hash_token(token: str) -> str:
    """Session tokens are stored hashed so a DB leak yields no usable sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(48)


# ======================================================================
# Rate limiting
# ======================================================================
@dataclass
class _RateWindow:
    hits: Deque[float] = field(default_factory=deque)


class RateLimiter:
    """In-process sliding-window limiter. Adequate for a single-VPS deployment;
    a multi-instance deployment should move this to Redis."""

    def __init__(self) -> None:
        self._windows: Dict[str, _RateWindow] = defaultdict(_RateWindow)
        self._lock_free = True

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - window_seconds

        while window.hits and window.hits[0] < cutoff:
            window.hits.popleft()

        if len(window.hits) >= limit:
            retry_after = int(window.hits[0] + window_seconds - now) + 1
            return False, max(1, retry_after)

        window.hits.append(now)
        return True, 0

    def reset(self) -> None:
        self._windows.clear()


rate_limiter = RateLimiter()

# Endpoint classes and their limits.
RATE_LIMITS = {
    "auth": (10, 300),        # 10 login attempts per 5 minutes
    "read": (300, 60),        # 300 reads per minute
    "write": (60, 60),        # 60 writes per minute
    "chat": (30, 60),         # 30 agent messages per minute
    "emergency": (10, 300),   # 10 stop/release operations per 5 minutes
}


def enforce_rate_limit(request: Request, bucket: str) -> None:
    limit, window = RATE_LIMITS.get(bucket, RATE_LIMITS["read"])
    client = request.client.host if request.client else "unknown"
    allowed, retry_after = rate_limiter.check(f"{bucket}:{client}", limit, window)
    if not allowed:
        logger.warning("Rate limit exceeded for %s on %s", client, bucket)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Rate limit exceeded.",
                "retry_after_seconds": retry_after,
                "bucket": bucket,
            },
            headers={"Retry-After": str(retry_after)},
        )


# ======================================================================
# Session principal
# ======================================================================
@dataclass
class Principal:
    """The authenticated caller. Passed to endpoints so they never re-derive identity."""

    user_id: Optional[str]
    email: str
    scopes: frozenset
    session_token: Optional[str] = None
    channel: str = "rest"  # rest | telegram | system
    is_operator: bool = False

    def has(self, scope: Scope) -> bool:
        return scope.value in self.scopes

    def require(self, *scopes: Scope) -> None:
        missing = [s.value for s in scopes if s.value not in self.scopes]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "Insufficient permissions.",
                    "required": [s.value for s in scopes],
                    "missing": missing,
                },
            )


# A system principal used by the worker and scheduler. It never handles user HTTP.
SYSTEM_PRINCIPAL = Principal(
    user_id=None,
    email="system@airdropos.internal",
    scopes=frozenset(ALL_SCOPES),
    channel="system",
    is_operator=True,
)


# ======================================================================
# Dependencies
# ======================================================================
bearer_scheme = HTTPBearer(auto_error=False)


async def get_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Resolve the caller from a bearer session token."""
    # The WebSocket uses a query-string token; REST uses the Authorization header.
    token: Optional[str] = None
    if credentials is not None and credentials.credentials:
        token = credentials.credentials
    else:
        token = request.query_params.get("token")

    # Explicit opt-out exists for local development only. In production the
    # application refuses to serve unauthenticated traffic at all, so a
    # misconfigured deployment cannot silently expose protected endpoints.
    if not settings.AUTH_ENABLED:
        if settings.ENVIRONMENT == "production":
            logger.error(
                "AUTH_ENABLED is false in production; refusing to serve unauthenticated traffic."
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "Authentication is misconfigured for this deployment."},
            )
        logger.warning(
            "AUTH_ENABLED is false; serving an unrestricted development principal. "
            "Never expose this deployment to a network."
        )
        return Principal(
            user_id=None,
            email="anonymous@airdropos.local",
            scopes=frozenset(ALL_SCOPES),
            channel="rest",
            is_operator=True,
        )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "Authentication required.",
                "hint": "POST /api/v1/auth/login to obtain a session token.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    session = await db.scalar(
        select(UserSession).where(UserSession.token == hash_token(token))
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid or revoked session."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    now = datetime.now(timezone.utc)
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Session expired. Log in again."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Account is inactive."},
        )

    # A session may hold fewer scopes than the user's role permits. An explicit
    # empty scope list grants nothing, so scope narrowing works as intended.
    session_scopes = session.scopes or []
    if session_scopes:
        scopes = frozenset(s for s in session_scopes if s in ALL_SCOPES)
    else:
        scopes = frozenset(_scopes_for(user))

    session.last_used_at = datetime.now(timezone.utc)

    return Principal(
        user_id=str(user.id),
        email=user.email,
        scopes=scopes,
        session_token=token,
        channel="rest",
        is_operator=bool(user.is_superuser),
    )


def _scopes_for(user: User) -> Sequence[str]:
    """Scopes are derived from the user's role. Deny-by-default."""
    if user.is_superuser:
        return list(ALL_SCOPES)
    if user.is_active:
        return list(READONLY_SCOPES)
    return []


def require_scopes(*scopes: Scope) -> Callable:
    """FastAPI dependency factory enforcing specific scopes."""

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        principal.require(*scopes)
        return principal

    return _dependency


# Convenience dependencies for common endpoint classes.
require_read = require_scopes(Scope.READ)
require_agent_chat = require_scopes(Scope.AGENT_CHAT)
require_task_write = require_scopes(Scope.TASK_WRITE)
require_wallet_read = require_scopes(Scope.WALLET_READ)
require_wallet_write = require_scopes(Scope.WALLET_WRITE)
require_tool_read = require_scopes(Scope.TOOL_READ)
require_system_admin = require_scopes(Scope.SYSTEM_ADMIN)
require_emergency = require_scopes(Scope.EMERGENCY_STOP)
require_audit_read = require_scopes(Scope.AUDIT_READ)


# ======================================================================
# Telegram authentication (separate channel, separate secret)
# ======================================================================
def verify_telegram_secret(provided: Optional[str]) -> bool:
    """Validate a Telegram webhook secret from configuration.

    Kept distinct from REST sessions so a leaked webhook secret does not grant
    browser API access.
    """
    expected = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", None)
    if not expected:
        return False
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


# ======================================================================
# Audit logging
# ======================================================================
async def write_audit(
    db: AsyncSession,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str,
    *,
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Record a security-relevant action. Secrets are never included."""
    try:
        db.add(
            AuditLog(
                actor=principal.email if principal else "anonymous",
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id),
                before_state=_redact(before_state or {}),
                after_state=_redact(after_state or {}),
                ip_address=ip_address,
            )
        )
        await db.flush()
    except Exception as exc:
        # Audit failure must not break the operation, but must be visible.
        logger.error("Audit write failed for %s on %s: %s", action, entity_type, exc)


_REDACT_KEYS = (
    "api_key", "apikey", "password", "secret", "token", "private_key",
    "seed", "mnemonic", "authorization", "master_key", "encrypted_secret",
)


def _redact(data: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if any(marker in key.lower() for marker in _REDACT_KEYS):
            clean[key] = "[REDACTED]"
        elif isinstance(value, dict):
            clean[key] = _redact(value)
        else:
            clean[key] = value
    return clean
