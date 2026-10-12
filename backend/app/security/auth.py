"""REST Authentication and Authorization.

Designed for a single-operator VPS deployment:
  * A single operator account is defined by configuration, not hardcoded.
  * Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes, never plaintext.
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
import ipaddress
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Any, Callable, Deque, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.models import AuditLog, User, UserSession

logger = logging.getLogger(__name__)

PBKDF2_ROUNDS = 600_000  # OWASP guidance for PBKDF2-HMAC-SHA256
# Hashes written with an older work factor (e.g. 390_000) keep verifying; anything
# below this floor, or above the ceiling, is refused rather than trusted blindly.
PBKDF2_MIN_ROUNDS = 100_000
PBKDF2_MAX_ROUNDS = 10_000_000
MIN_SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 256  # must match LoginRequest.password max_length
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
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Operator password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Operator password must be at most {MAX_PASSWORD_LENGTH} characters."
        )
    salt = salt or os.urandom(16)
    if len(salt) < MIN_SALT_BYTES:
        raise ValueError(f"Salt must be at least {MIN_SALT_BYTES} bytes.")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def _parse_password_hash(stored: object) -> Optional[tuple[int, bytes, bytes]]:
    """Return (rounds, salt, digest) for a well-formed, acceptable hash, else None."""
    if not isinstance(stored, str):
        return None
    try:
        algorithm, rounds_s, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2":
            return None
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except ValueError:
        return None
    if not (PBKDF2_MIN_ROUNDS <= rounds <= PBKDF2_MAX_ROUNDS):
        return None
    if len(salt) < MIN_SALT_BYTES or len(digest) != 32:
        return None
    return rounds, salt, digest


def is_valid_password_hash(stored: object) -> bool:
    """True when `stored` is a usable pbkdf2$rounds$salt$hash value."""
    return _parse_password_hash(stored) is not None


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password verification. Fails closed on malformed or weak hashes."""
    parsed = _parse_password_hash(stored)
    if parsed is None or not isinstance(password, str):
        return False
    rounds, salt, digest = parsed
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, digest)


_DUMMY_PASSWORD_HASH: Optional[str] = None


def verify_against_dummy(password: str) -> None:
    """Burn the same PBKDF2 work as a real check, for logins naming an unknown account.

    Without it an unknown email answers in microseconds and a known one takes a full PBKDF2
    round, so response time reveals which accounts exist.
    """
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-equalisation")
    verify_password(password, _DUMMY_PASSWORD_HASH)


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


@lru_cache(maxsize=8)
def _parse_trusted_proxies(raw: str) -> tuple:
    """Parse TRUSTED_PROXIES (comma-separated IPs/CIDRs) into networks. Bad entries are skipped.

    A skipped entry can only make a proxy *untrusted*, which collapses clients into the proxy's
    bucket (safe, noisy) and never lets an unlisted address vouch for a forwarded header.
    """
    networks = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.error("Ignoring invalid TRUSTED_PROXIES entry %r", item)
    return tuple(networks)


def _normalize_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def client_ip(request: Request) -> str:
    """The address rate limits are keyed on.

    ``X-Forwarded-For`` is client-controlled, so it is consulted ONLY when the directly connected
    peer is one of ``settings.TRUSTED_PROXIES``. The header is then read right to left, skipping
    addresses that are themselves trusted proxies; the first address that is not trusted is the
    client. Anything unusable (no trusted proxy configured, an untrusted peer, a malformed header)
    falls back to the peer address, so a spoofed header can never choose its own bucket.
    """
    peer_raw = request.client.host if request.client else None
    if not peer_raw:
        return "unknown"
    networks = _parse_trusted_proxies(getattr(settings, "TRUSTED_PROXIES", "") or "")
    peer = _normalize_ip(peer_raw)
    if not networks or peer is None or not any(peer in net for net in networks):
        return peer_raw

    hops = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        addr = _normalize_ip(hop)
        if addr is None:
            return peer_raw  # malformed header: do not guess
        if not any(addr in net for net in networks):
            return str(addr)
    return peer_raw


def enforce_rate_limit(request: Request, bucket: str) -> None:
    limit, window = RATE_LIMITS.get(bucket, RATE_LIMITS["read"])
    client = client_ip(request)
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
    # When the session this principal came from stops being valid (UTC). Lets long-lived
    # connections end exactly at expiry instead of waiting for the next database revalidation.
    session_expires_at: Optional[datetime] = None

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

# Minimum gap between two last_used_at writes for the same session.
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


async def resolve_session(db: AsyncSession, token: Optional[str], touch: bool = True) -> Principal:
    """Resolve a raw session token into a Principal. The single source of truth.

    ``touch=False`` is for periodic revalidation of an already-open connection: it performs every
    check but does not write ``last_used_at``, so frequent revalidation costs a read, not a write.

    Both REST (`get_principal`) and the WebSocket endpoint call this, so a
    revoked, expired, unknown or inactive-account session is refused identically
    everywhere. Raises `AuthError` (401) on any failure.

    Scopes are deny-by-default: a session holds exactly the scopes written onto
    it at login. An empty or missing scope list grants nothing; it never falls
    back to the account's role, which would turn a deliberately narrowed (or
    corrupted) session into full operator access.
    """
    if not token:
        raise AuthError("Authentication required.")

    session = await db.scalar(
        select(UserSession).where(UserSession.token == hash_token(token))
    )
    if session is None or session.revoked_at is not None:
        raise AuthError("Invalid or revoked session.")

    now = datetime.now(timezone.utc)
    expires = session.expires_at
    if expires is None:
        raise AuthError("Invalid or revoked session.")
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise AuthError("Session expired. Log in again.")

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise AuthError("Account is inactive.")

    granted = session.scopes if isinstance(session.scopes, (list, tuple, set)) else []
    scopes = frozenset(s for s in granted if isinstance(s, str) and s in ALL_SCOPES)

    if touch:
        # last_used_at is bookkeeping, not a security check. Writing it on every request made each
        # dashboard poll a write to the same row, which on SQLite serialises against the worker's
        # own writes ("database is locked"). Refresh it at most once per interval.
        last_used = session.last_used_at
        if last_used is not None and last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=timezone.utc)
        if last_used is None or now - last_used >= LAST_USED_WRITE_INTERVAL:
            session.last_used_at = now

    return Principal(
        user_id=str(user.id),
        email=user.email,
        scopes=scopes,
        session_token=token,
        channel="rest",
        is_operator=bool(user.is_superuser),
        session_expires_at=expires,
    )


async def get_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Resolve the caller from a bearer session token in the Authorization header.

    Tokens are accepted ONLY from the Authorization header. A query-string token
    would end up in access logs, proxy logs, browser history and Referer headers.
    (The WebSocket endpoint, which browsers cannot send headers to, uses the
    `Sec-WebSocket-Protocol` handshake instead; see `endpoints/ws.py`.)
    """
    token: Optional[str] = None
    if credentials is not None and credentials.credentials:
        token = credentials.credentials

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

    try:
        return await resolve_session(db, token)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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
