"""Value objects the Discord integration passes around.

These are plain dataclasses, not ORM rows. They exist so the browser adapter, the
service, the tools and the API all speak one shape, and so a value that must never
reach the database or the browser (a message body) can be kept apart from one that
should (a fingerprint, a channel id, a permalink).

``MessageRecord`` in particular is the seam that keeps personal data out of storage:
the full text lives only in the field the adapter returns to the live caller, while
``fingerprint`` - a salted hash - is what gets persisted for duplicate detection.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Activity statuses. Mirrors the X integration's vocabulary so the UI reads the same.
OK = "SUCCESS"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"
BLOCKED = "NEEDS_USER_ACTION"
SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"

#: Discord's own hard limit. Enforced before anything is typed so an over-long
#: message fails as a validation error instead of half-filling the composer.
MAX_MESSAGE_CHARS = 2000

#: The longest slice of message text kept in an activity record for later
#: verification. Enough to recognise "the message I sent", short enough that the
#: history is not a transcript of everything AirdropOS has read.
MAX_STORED_EXCERPT = 240


def text_fingerprint(text: str, *, reply_to: str = "") -> str:
    """A stable, non-reversible fingerprint of a message body.

    Used for duplicate detection, so the SAME text an hour apart still collides -
    which is the duplicate we are trying to stop - while the stored value cannot be
    reversed into the operator's words.
    """
    body = hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()[:32]
    return f"text:{body}" + (f"|reply:{reply_to}" if reply_to else "")


@dataclass
class LoginReading:
    """What we could actually observe about a profile's Discord session.

    ``state`` is one of ``LOGGED_IN`` / ``LOGGED_OUT`` / ``UNKNOWN`` / ``NO_BROWSER``.
    ``UNKNOWN`` is a real answer, not a failure: it is what stops a transient page
    error from being reported to the operator as a request to log in again.
    """

    state: str
    indicator: str = ""
    detail: str = ""
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_logged_out(self) -> bool:
        return self.state == "LOGGED_OUT"

    @property
    def is_logged_in(self) -> bool:
        return self.state == "LOGGED_IN"

    @property
    def is_verified(self) -> bool:
        """True only for a positive reading either way."""
        return self.state in ("LOGGED_IN", "LOGGED_OUT")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "indicator": self.indicator,
            "detail": self.detail,
            "observed_at": self.observed_at,
        }


@dataclass
class MessageRecord:
    """One Discord message as read from the page.

    Only what the page itself showed. Nothing here is inferred: if the author or
    timestamp were not rendered, the field is empty rather than filled in.
    """

    message_id: str = ""
    author: str = ""
    timestamp: str = ""
    content: str = ""
    permalink: str = ""
    is_reply: bool = False
    reply_to_message_id: str = ""

    @property
    def fingerprint(self) -> str:
        return text_fingerprint(self.content)

    def summary(self) -> Dict[str, Any]:
        """What is safe to persist: ids, metadata and a bounded excerpt.

        The full body is deliberately NOT here. Activity history is a record of what
        AirdropOS did and where, not a transcript of other people's conversations.
        """
        body = self.content.strip()
        return {
            "message_id": self.message_id,
            "author": self.author[:80],
            "timestamp": self.timestamp,
            "excerpt": body[:MAX_STORED_EXCERPT],
            "excerpt_truncated": len(body) > MAX_STORED_EXCERPT,
            "permalink": self.permalink[:300],
            "fingerprint": self.fingerprint,
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "author": self.author,
            "timestamp": self.timestamp,
            "content": self.content,
            "permalink": self.permalink,
            "is_reply": self.is_reply,
            "reply_to_message_id": self.reply_to_message_id,
        }


@dataclass
class ChannelRef:
    """Where in Discord an operation happened.

    ``server_id`` / ``channel_id`` come from the page URL when it carries them. They
    are not secrets and they are what makes a result referenceable.
    """

    server_id: str = ""
    server_name: str = ""
    channel_id: str = ""
    channel_name: str = ""

    @property
    def label(self) -> str:
        if self.server_name and self.channel_name:
            return f"{self.server_name} / #{self.channel_name}"
        if self.channel_name:
            return f"#{self.channel_name}"
        if self.channel_id:
            return f"channel:{self.channel_id}"
        return self.server_name or "Discord"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "label": self.label,
        }


@dataclass
class DiscordOutcome:
    """One Discord operation, with everything the caller needs to act on it.

    ``status`` is the honest verdict. ``UNCERTAIN`` is a first-class result for a
    write whose fate could not be determined, and it is never silently upgraded to
    success or downgraded to failure.
    """

    ok: bool
    operation: str
    status: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_category: str = ""
    uncertain: bool = False
    needs_user: bool = False
    channel: Optional[ChannelRef] = None
    evidence_path: str = ""
    fingerprint: str = ""
    #: The browser profile this ran against. The account IS the profile, so it is
    #: carried on every outcome - including the failures - and never re-guessed.
    browser_profile_id: str = ""
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "status": self.status,
            "data": dict(self.data),
            "error": self.error,
            "error_category": self.error_category,
            "outcome_uncertain": self.uncertain,
            "needs_user_action": self.needs_user,
            "channel": self.channel.as_dict() if self.channel else {},
            "evidence_path": self.evidence_path,
            "fingerprint": self.fingerprint,
            # Emitted under the key the task controller reads to build an
            # ``integration_ref``, so a parked Discord login request names the exact
            # browser profile the operator has to open. Prefixed with the
            # integration name because a bare key could be a Telegram session, and
            # reading one as a browser profile would send the operator to the wrong
            # place entirely.
            "session_key": f"browser:{self.browser_profile_id}" if self.browser_profile_id else "",
            "browser_profile_id": self.browser_profile_id,
            "notes": list(self.notes),
        }