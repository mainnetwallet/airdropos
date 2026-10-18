"""Errors the Discord integration raises, and what each one means.

The important separation in this module is between "the page asked this profile to
sign in" and everything else.

:class:`DiscordLoginRequired` is raised ONLY on positive evidence that Discord is
showing its sign-in form (see ``browser.login_state``). Every other failure - a
navigation timeout, a missing element, a 5xx, a rate limit, a dead connection - is
a :class:`DiscordBrowserError` carrying a *category*.

That distinction is the whole reason this file exists. A transient page error read
as "you are logged out" strands a task and sends the operator to sign in to an
account that was already fine; the reverse - treating a real login wall as a blip -
resumes a task straight back into the same wall. Neither is acceptable, so the
categories are kept apart rather than collapsed into one boolean.

``error_category`` values are the strings ``inbox.service.classify_reason`` reads.
``CAT_LOGIN_REQUIRED`` is the one it maps to a ``DISCORD_LOGIN_REQUIRED``
intervention; no other category ever becomes a login request.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ----------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------
CAT_LOGIN_REQUIRED = "DISCORD_LOGIN_REQUIRED"
CAT_INTENT_MISMATCH = "DISCORD_INTENT_MISMATCH"
CAT_NETWORK = "DISCORD_NETWORK"
CAT_RATE_LIMITED = "DISCORD_RATE_LIMITED"
CAT_PERMISSION = "DISCORD_PERMISSION"
CAT_PAGE_ERROR = "DISCORD_PAGE_ERROR"
CAT_NOT_FOUND = "DISCORD_NOT_FOUND"
CAT_REFUSED = "DISCORD_REFUSED"
CAT_UNKNOWN = "DISCORD_UNKNOWN_ERROR"

#: The only category that is allowed to become an operator login request.
#: Kept as a literal so this module stays importable from ``inbox`` without a cycle.
LOGIN_CATEGORY = CAT_LOGIN_REQUIRED

ALL_CATEGORIES = (
    CAT_LOGIN_REQUIRED,
    CAT_INTENT_MISMATCH,
    CAT_NETWORK,
    CAT_RATE_LIMITED,
    CAT_PERMISSION,
    CAT_PAGE_ERROR,
    CAT_NOT_FOUND,
    CAT_REFUSED,
    CAT_UNKNOWN,
)

#: Categories that may clear on their own. A retry is not obviously wrong, but the
#: task planner still decides whether to spend a retry budget on one.
TRANSIENT_CATEGORIES = frozenset({CAT_NETWORK, CAT_RATE_LIMITED, CAT_PAGE_ERROR})


class DiscordError(Exception):
    """Base for every Discord integration failure.

    ``category`` is what the caller branches on; ``status_code`` is only used when
    the failure surfaces through the REST API.
    """

    category = CAT_UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        category: Optional[str] = None,
        status_code: int = 400,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if category:
            self.category = category
        self.status_code = status_code
        self.detail: Dict[str, Any] = dict(detail or {})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "error_category": self.category,
            "detail": dict(self.detail),
        }


class DiscordBrowserError(DiscordError):
    """The browser could not complete the operation. NOT a login problem by default."""


class DiscordLoginRequired(DiscordError):
    """Positive evidence that Discord is asking this profile to sign in.

    Constructed only where the sign-in form or a login route was actually observed.
    Carries ``indicator`` so the operator can see WHY we concluded it, which is the
    difference between a fact and a guess.
    """

    category = CAT_LOGIN_REQUIRED

    def __init__(self, message: str, *, indicator: str = "") -> None:
        super().__init__(message, category=CAT_LOGIN_REQUIRED, status_code=409)
        self.indicator = indicator

    def as_dict(self) -> Dict[str, Any]:
        return {**super().as_dict(), "indicator": self.indicator}


class DiscordProfileError(DiscordError):
    """The selected browser profile does not exist, or cannot be opened.

    Always fatal for the task that named it. AirdropOS never quietly substitutes a
    different profile: sending from the wrong account is worse than not acting.
    """

    category = "DISCORD_PROFILE_UNAVAILABLE"

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message, category=self.category, status_code=status_code)


class DiscordIntentError(DiscordError):
    """The requested operation contradicts what the operator asked for.

    Raised when a read-only or draft-only task tries to send. This is the guard that
    makes "read the announcements" unable to become "reply to everyone".
    """

    category = CAT_INTENT_MISMATCH

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message, category=self.category, status_code=status_code)


class DiscordUnknownOutcome(DiscordError):
    """A write may or may not have been applied.

    Never auto-retried. The caller must verify against the live page first, because a
    blind repeat is exactly how a person gets the same message twice.
    """

    category = "DISCORD_OUTCOME_UNKNOWN"

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, category=self.category, status_code=409, detail=detail)