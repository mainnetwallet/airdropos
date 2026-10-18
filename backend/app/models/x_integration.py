"""X.com (Twitter) integration models.

Two tables:

* ``x_api_configurations`` - one row per X developer app + authorized account the
  operator has configured. Every secret is stored encrypted; nothing in this table
  is ever returned to the browser in readable form.
* ``x_activity_records`` - the integration's own task/activity history: which
  account, which method (api or browser), which operation, when, and what came
  back. This is what makes duplicate prevention and post-hoc verification of an
  uncertain write possible.

Both are additive. They carry no foreign keys into the task tables on purpose: an
X activity record must survive the deletion of the configuration or task it
describes, because that history is the only evidence of what was actually sent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database.session import Base

# Status values for a configuration. `UNVERIFIED` is deliberately distinct from
# `OK`: a row that has never been tested must not be reported as working.
CONFIG_UNVERIFIED = "UNVERIFIED"
CONFIG_OK = "OK"
CONFIG_AUTH_REQUIRED = "AUTH_REQUIRED"
CONFIG_INVALID = "INVALID"
CONFIG_RATE_LIMITED = "RATE_LIMITED"
CONFIG_ERROR = "ERROR"

CONFIG_STATUSES = (
    CONFIG_UNVERIFIED,
    CONFIG_OK,
    CONFIG_AUTH_REQUIRED,
    CONFIG_INVALID,
    CONFIG_RATE_LIMITED,
    CONFIG_ERROR,
)


class XApiConfiguration(Base):
    """One named X API configuration: app credentials + an authorized account.

    Secrets (``client_secret``, ``access_token``, ``refresh_token``) are stored as
    AES-256-GCM ciphertext produced by
    :class:`backend.app.integrations.x.crypto.CredentialVault`. The plaintext is
    decrypted only inside the API client, for the lifetime of one request.
    """

    __tablename__ = "x_api_configurations"
    __table_args__ = (
        Index("ix_x_api_configurations_name", "name", unique=True),
        Index("ix_x_api_configurations_enabled_default", "is_enabled", "is_default"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(400), default="", nullable=False)

    # --- app (developer console) credentials -------------------------------
    # The client id is not a secret and is stored in the clear so an operator can
    # tell two configurations apart; the secret is not.
    client_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    client_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- user authorization (OAuth 2.0 authorization code + PKCE) ----------
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)

    # --- resolved identity (filled by a successful test / me lookup) -------
    x_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    x_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # --- operator controls -------------------------------------------------
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- last observed state ----------------------------------------------
    # `status_detail` is written by our own code from an error CATEGORY and a
    # safe message. It must never contain a token, secret or response body
    # header; `redact` in credentials.py is applied before it is stored.
    status: Mapped[str] = mapped_column(
        String(32), default=CONFIG_UNVERIFIED, nullable=False, index=True
    )
    status_detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit_remaining: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rate_limit_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- audit -------------------------------------------------------------
    created_by: Mapped[str] = mapped_column(String(128), default="operator", nullable=False)

    @property
    def is_authorized(self) -> bool:
        """A configuration can be used for writes only with a user access token."""
        return bool(self.access_token_enc)


class XActivityRecord(Base):
    """One X operation attempt, by any method, from any task."""

    __tablename__ = "x_activity_records"
    __table_args__ = (
        Index("ix_x_activity_records_operation_key_created", "operation_key", "created_at"),
        Index("ix_x_activity_records_created_at", "created_at"),
    )

    # None of these are foreign keys on purpose (see the module docstring).
    config_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    config_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    browser_profile_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    method: Mapped[str] = mapped_column(String(16), nullable=False)  # api | browser
    operation: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    # A short, non-secret description of what was acted on: a post id, a
    # "@handle", or "text:<sha256 prefix>". Never the post body itself.
    target: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Stable fingerprint used to refuse a duplicate write. See
    # `activity.operation_key`.
    operation_key: Mapped[str] = mapped_column(String(160), default="", nullable=False)

    # True when the write may or may not have landed on the far side. A timeout
    # or a lost response sets this; it is what stops an automatic retry.
    outcome_uncertain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Verified identifiers: the created post id, the resolved user id, ...
    result_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    error: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)