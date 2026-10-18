"""Encryption for X API credentials at rest.

X credentials are a bearer secret: whoever holds the access token can post as the
operator's account. They are sealed with AES-256-GCM before they touch the
database and are only ever decrypted inside the API client, for the duration of
one HTTP request.

Key material
------------
The repository already ships two key sources and this module reuses them rather
than inventing a third:

* ``WALLET_VAULT_MASTER_KEY`` (64 hex characters) when it is set. This is the
  project's existing AES-256-GCM master key.
* otherwise a key derived from ``SECRET_KEY`` with a domain-separation suffix,
  which is the same shape ``browser/profiles.py`` uses for session cookies.

In production a deployment whose key is a published placeholder or the shipped
development default is REFUSED, exactly as the browser state vault refuses to
save cookies there. Refusing beats writing plaintext secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.app.core.config import _PLACEHOLDER_PREFIXES, settings

logger = logging.getLogger(__name__)

_DEV_SECRET_KEY = "0123456789abcdef" * 4  # the publicly known development default
_DEV_VAULT_KEY = _DEV_SECRET_KEY
_HEX_RE_LEN = 64
_NONCE_BYTES = 12

# Domain separation for the AAD, so a bundle can never be replayed into another
# subsystem that happens to share the key.
_INFO = b"airdropos/x-api-credentials/v1"

# The fields that make up an encrypted credential bundle. Kept explicit so a new
# field cannot be added and then stored in the clear by accident.
SECRET_FIELDS = ("client_secret", "access_token", "refresh_token")


class VaultUnavailable(RuntimeError):
    """The vault cannot be used in this deployment (unsafe key in production)."""


class DecryptionFailed(RuntimeError):
    """A stored envelope could not be opened with the configured key."""


def _unsafe_key() -> bool:
    """Whether the key protecting X credentials is one anyone can read.

    Two sources are refused in production, not one:

    * a ``WALLET_VAULT_MASTER_KEY`` that is a published placeholder or the
      shipped development value, which ``core.config`` flags as insecure but
      which this module previously did not mirror;
    * no vault key at all, with the publicly known development ``SECRET_KEY``
      (the browser-state vault already refuses exactly this).
    """
    if settings.ENVIRONMENT != "production":
        return False
    vault_key = (settings.WALLET_VAULT_MASTER_KEY or "").strip().lower()
    if vault_key:
        return vault_key.startswith(_PLACEHOLDER_PREFIXES) or vault_key == _DEV_VAULT_KEY
    return (settings.SECRET_KEY or "").strip() == _DEV_SECRET_KEY


def _master_key() -> bytes:
    """The 32 raw bytes used for AES-256-GCM."""
    # The unsafe-key check runs FIRST, before the vault key is accepted. It used
    # to run only on the fall-through path, which meant a configured-but-
    # placeholder WALLET_VAULT_MASTER_KEY sailed straight through: the function
    # returned the key without ever consulting `_unsafe_key()`.
    if _unsafe_key():
        raise VaultUnavailable(
            "X API credentials cannot be stored: this deployment is in production with a "
            "publicly known default encryption key. Set WALLET_VAULT_MASTER_KEY to 64 random "
            "hex characters, or a non-default SECRET_KEY."
        )
    configured = (settings.WALLET_VAULT_MASTER_KEY or "").strip()
    if configured:
        raw = configured[2:] if configured[:2].lower() == "0x" else configured
        if len(raw) != _HEX_RE_LEN:
            raise VaultUnavailable(
                "WALLET_VAULT_MASTER_KEY must be exactly 64 hexadecimal characters "
                "(256-bit key). The value itself is never logged."
            )
        try:
            return bytes.fromhex(raw)
        except ValueError as exc:  # pragma: no cover - guarded by the length check
            raise VaultUnavailable("WALLET_VAULT_MASTER_KEY is not valid hexadecimal.") from exc
    # Same shape as browser/profiles.py: a key derived from SECRET_KEY with a
    # domain-separation suffix, never the SECRET_KEY itself.
    derived = hashlib.sha256(f"{settings.SECRET_KEY}|x-api-credentials-v1".encode("utf-8")).digest()
    return derived


def vault_ready() -> bool:
    """Whether credentials can be encrypted right now, without raising."""
    try:
        _master_key()
        return True
    except VaultUnavailable:
        return False


def vault_status() -> Dict[str, Any]:
    """Safe, non-secret description of the vault for the settings UI."""
    key_source = (
        "WALLET_VAULT_MASTER_KEY"
        if (settings.WALLET_VAULT_MASTER_KEY or "").strip()
        else "SECRET_KEY (derived)"
    )
    return {
        "ready": vault_ready(),
        "key_source": key_source,
        "algorithm": "AES-256-GCM",
        "detail": (
            "Set WALLET_VAULT_MASTER_KEY, or a non-default SECRET_KEY, before storing "
            "X credentials."
            if not vault_ready()
            else ""
        ),
    }


class CredentialVault:
    """Seal and open a JSON credential bundle.

    Envelope layout (single string, safe to store in a TEXT column)::

        xcv1.<b64 nonce>.<b64 ciphertext+tag>.<b64 aad fingerprint>

    The AAD fingerprint binds a blob to its owner, so a ciphertext copied from
    one configuration row into another fails to open instead of silently working.
    """

    PREFIX = "xcv1"

    def __init__(self, owner: str) -> None:
        self._owner = str(owner or "x-api")
        self._key = _master_key()
        self._aad = _aad_for(self._owner)

    def seal(self, values: Dict[str, Any]) -> str:
        payload = {k: values[k] for k in SECRET_FIELDS if values.get(k)}
        if not payload:
            return ""
        nonce = os.urandom(_NONCE_BYTES)
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        sealed = AESGCM(self._key).encrypt(nonce, blob, self._aad)
        fingerprint = hashlib.sha256(self._aad).digest()[:8]
        return ".".join(
            [
                self.PREFIX,
                base64.urlsafe_b64encode(nonce).decode(),
                base64.urlsafe_b64encode(sealed).decode(),
                base64.urlsafe_b64encode(fingerprint).decode(),
            ]
        )

    def open(self, envelope: Optional[str]) -> Dict[str, str]:
        """Decrypt an envelope. Returns only the secret fields, never more."""
        if not envelope:
            return {}
        parts = str(envelope).split(".")
        if len(parts) != 4 or parts[0] != self.PREFIX:
            raise DecryptionFailed("Stored credential envelope has an unrecognised format.")
        try:
            nonce = base64.urlsafe_b64decode(parts[1])
            sealed = base64.urlsafe_b64decode(parts[2])
            fingerprint = base64.urlsafe_b64decode(parts[3])
        except Exception as exc:
            raise DecryptionFailed("Stored credential envelope is not valid base64.") from exc
        if not hmac.compare_digest(fingerprint, hashlib.sha256(self._aad).digest()[:8]):
            raise DecryptionFailed(
                "This encrypted bundle does not belong to this configuration. It was most "
                "likely copied from another row or decrypted with a different key."
            )
        try:
            blob = AESGCM(self._key).decrypt(nonce, sealed, self._aad)
        except InvalidTag as exc:
            raise DecryptionFailed(
                "Credential decryption failed. The encryption key has probably changed since "
                "these credentials were stored; re-authorize the account."
            ) from exc
        payload = json.loads(blob.decode("utf-8"))
        return {str(k): str(v) for k, v in payload.items() if k in SECRET_FIELDS}


def _aad_for(owner: str) -> bytes:
    return _INFO + b"|" + owner.encode("utf-8")


# ----------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------
# Keys whose values must never reach a log line, an API response, the activity
# table or the frontend. Matched case-insensitively as substrings, which is why
# `x_client_secret` and `refresh_token` are both caught.
REDACT_MARKERS = (
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "bearer",
    "private_key",
    "code_verifier",
)
REDACTED = "[REDACTED]"


def redact(value: Any, *, keep_length: bool = False) -> Any:
    """Return a copy of ``value`` with every secret-looking string replaced."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_secret_key(str(k)) else redact(v, keep_length=keep_length))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, keep_length=keep_length) for v in value]
    if isinstance(value, str) and keep_length:
        return mask(value)
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in REDACT_MARKERS)


def mask(secret: Optional[str]) -> str:
    """A length-only hint for a secret.

    Deliberately reveals no prefix or suffix of the value. The UI uses the fixed
    ``••••`` marker in `store.public_configuration` instead; this exists for
    diagnostics that want to show how long a stored value is.
    """
    text = str(secret or "")
    if not text:
        return ""
    return f"{'•' * 4} ({len(text)} chars)"


# Shapes a bearer secret takes: an opaque base64url-ish run, or a long hex digest.
_SECRET_SHAPES = (
    re.compile(r"^[A-Za-z0-9_\-.=]{20,}$"),
    re.compile(r"^[0-9a-fA-F]{40,}$"),
)


def looks_like_secret(text: str) -> bool:
    """Whether a free-text field is probably a token, so bulk import can flag it."""
    candidate = str(text or "").strip()
    if len(candidate) < 20:
        return False
    return any(pattern.match(candidate) for pattern in _SECRET_SHAPES)


# Long opaque runs, as they appear embedded in free text (a quoted header, an
# exception message). Anything this matches is replaced before it is persisted.
_SCRUB_RE = re.compile(r"\b[A-Za-z0-9_\-.=]{24,}\b")


def scrub_text(text: Optional[str], *, limit: int = 600) -> str:
    """Blank out anything token-shaped inside a free-text field.

    Used for the activity `error` and `target` columns, which are strings rather
    than structured fields and so cannot be covered by key-based redaction. It is
    a heuristic and says so: it will also blank a long URL fragment, at the cost
    of some detail in an error message. That is the right trade next to never
    persisting a credential in a table that outlives the key.
    """
    raw = str(text or "")
    if not raw:
        return ""
    return _SCRUB_RE.sub(REDACTED, raw)[:limit]


def assert_no_secrets(payload: Any, *, where: str = "payload") -> None:
    """Last line of defence before a structure is persisted or logged.

    Walks the structure and raises if a value under a secret-looking key still
    holds something. Cheap enough to call on every write path.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _is_secret_key(str(key)) and value not in (None, "", REDACTED):
                raise ValueError(f"Refusing to store an unmasked secret in {where}.{key}.")
            assert_no_secrets(value, where=f"{where}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            assert_no_secrets(value, where=f"{where}[{index}]")