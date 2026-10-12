"""Shared-secret handling for the worker gateway.

The gateway is internal: only the API process may call it, and it re-checks a
shared secret on every request. When no explicit secret is configured one is
derived from SECRET_KEY, so a normal single-.env deployment needs no extra setup.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from typing import Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_SECRET_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
GATEWAY_HEADER = "X-Live-Gateway-Key"


def gateway_secret() -> str:
    if settings.LIVE_GATEWAY_SECRET:
        return settings.LIVE_GATEWAY_SECRET
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), b"airdropos-live-gateway-v1", hashlib.sha256
    ).hexdigest()


def secret_matches(provided: Optional[str]) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), gateway_secret().encode("utf-8"))


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_allowed() -> Optional[str]:
    """Return None when the gateway may start, otherwise the reason it must not.

    Fail closed: a gateway reachable beyond loopback must not be guarded by a
    secret derived from the publicly known default SECRET_KEY.
    """
    if _is_loopback(settings.LIVE_GATEWAY_HOST):
        return None
    if not settings.LIVE_GATEWAY_SECRET and settings.SECRET_KEY == _DEFAULT_SECRET_KEY:
        return (
            "LIVE_GATEWAY_HOST is not loopback but neither LIVE_GATEWAY_SECRET nor a custom "
            "SECRET_KEY is configured. Set one of them."
        )
    return None
