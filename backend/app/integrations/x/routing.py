"""Deciding HOW an X.com task should be carried out.

This is deterministic code, not a prompt instruction. The model may ask for a
method; this module decides whether that request is actually supported,
authorized and available, and what happens when it is not.

The rules, in order:

1. A task that is inherently about the WEBSITE (open x.com, look at this page,
   inspect its controls, do something only the UI offers) goes to the browser,
   whatever the preference says.
2. An operation the official API supports, on an account that is configured and
   authorized, goes to the API when the preference allows it.
3. The other method is tried only when the preferred one is genuinely
   unavailable, and the result says so. It is NEVER used to route around an
   X-side restriction: the X integration refuses to use the browser to evade a
   rate limit or a permission denial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

AUTO = "auto"
API = "api"
BROWSER = "browser"
METHODS = (AUTO, API, BROWSER)

MODE_AUTO = "auto"
MODE_API_PREFERRED = "api_preferred"
MODE_BROWSER_PREFERRED = "browser_preferred"
PREFERENCES = (MODE_AUTO, MODE_API_PREFERRED, MODE_BROWSER_PREFERRED)


@dataclass(frozen=True)
class Operation:
    """One X capability and what each method can do with it."""

    name: str
    label: str
    is_write: bool
    api: bool
    browser: bool
    api_scope: str = ""
    browser_note: str = ""
    # Set when the API path is real but carries a cost or a restriction the
    # operator must know about. Surfaced on the page, never hidden.
    api_caveat: str = ""

    @property
    def supported_by(self) -> List[str]:
        methods = []
        if self.api:
            methods.append(API)
        if self.browser:
            methods.append(BROWSER)
        return methods


# X API v2 is pay-per-usage credit-based (verified 2026-10-18 against
# docs.x.com/x-api/getting-started/pricing): there are no subscriptions and no
# free/paid tiers gating writes. Every call is billed - writes PER REQUEST
# (e.g. create post $0.015, create with a URL $0.200, user interaction $0.015),
# reads PER RESOURCE returned. Requests are blocked once the prepaid balance is
# exhausted. So the real caveats are COST and CREDIT BALANCE, not a plan tier.
_COST_CAVEAT_WRITE = (
    "Billed per request by X's pay-per-usage credits (no subscriptions). Needs a positive credit "
    "balance; requests are blocked when it is exhausted."
)
_COST_CAVEAT_POST = (
    "Billed per request by X's pay-per-usage credits (no subscriptions) - creating a post costs "
    "credits, and one containing a URL costs considerably more. Needs a positive credit balance."
)

OPERATIONS: Dict[str, Operation] = {
    "read_profile": Operation(
        name="read_profile",
        label="Read a profile",
        is_write=False,
        api=True,
        browser=True,
        api_scope="users.read",
        api_caveat="Billed per user returned.",
        browser_note="Renders https://x.com/<handle>. Shows the same data plus anything the page reveals that the API does not.",
    ),
    "read_post": Operation(
        name="read_post",
        label="Read a post",
        is_write=False,
        api=True,
        browser=True,
        api_scope="tweet.read,users.read",
        api_caveat="Billed per post returned.",
    ),
    "create_post": Operation(
        name="create_post",
        label="Publish a post",
        is_write=True,
        api=True,
        browser=True,
        api_scope="tweet.read,tweet.write,users.read",
        api_caveat=_COST_CAVEAT_POST,
    ),
    "create_reply": Operation(
        name="create_reply",
        label="Publish a reply",
        is_write=True,
        api=True,
        browser=True,
        api_scope="tweet.read,tweet.write,users.read",
        api_caveat=_COST_CAVEAT_POST,
    ),
    "like": Operation(
        name="like",
        label="Like a post",
        is_write=True,
        api=True,
        browser=True,
        api_scope="like.write,tweet.read,users.read",
        api_caveat=_COST_CAVEAT_WRITE,
    ),
    "unlike": Operation(
        name="unlike",
        label="Remove a like",
        is_write=True,
        api=True,
        browser=True,
        api_scope="like.write,tweet.read,users.read",
        api_caveat=_COST_CAVEAT_WRITE,
    ),
    "follow": Operation(
        name="follow",
        label="Follow an account",
        is_write=True,
        api=True,
        browser=True,
        api_scope="users.read,tweet.read,follows.write",
        api_caveat=(
            _COST_CAVEAT_WRITE + " Following a protected account only submits a pending request."
        ),
    ),
    "unfollow": Operation(
        name="unfollow",
        label="Unfollow an account",
        is_write=True,
        api=True,
        browser=True,
        api_scope="users.read,tweet.read,follows.write",
        api_caveat=_COST_CAVEAT_WRITE,
    ),
    "verify_action": Operation(
        name="verify_action",
        label="Check whether an action already happened",
        is_write=False,
        api=True,
        browser=True,
        api_scope="tweet.read,like.read,follows.read,users.read",
        api_caveat=(
            "Verification reads are billed per resource, and each list endpoint is page-bounded: a "
            "negative answer from a partially-read list is reported as PARTIAL, not as proof."
        ),
    ),
}

WRITE_OPERATIONS = {name for name, op in OPERATIONS.items() if op.is_write}


# ----------------------------------------------------------------------
# Intent classification
# ----------------------------------------------------------------------
# Phrases that make a task about the WEBSITE rather than about the account.
# They are matched on the operator's own words (and the model's restatement of
# them) before any method is chosen.
_BROWSER_INTENT = re.compile(
    r"\b("
    r"open\s+(?:x|twitter)\.com|go\s+to\s+(?:x|twitter)\.com|"
    r"website|web\s?site|web\s?ui|in\s+the\s+browser|open\s+(?:the\s+)?browser|"
    r"look\s+at\s+the\s+page|inspect\s+the\s+(?:page|controls|buttons|ui)|"
    r"visible\s+(?:controls?|buttons?|ui)|what\s+does\s+the\s+page|"
    r"take\s+a\s+screenshot|scroll\s+the\s+page|dark\s+mode|"
    r"browser\s+(?:mode|profile)|live\s+view"
    r")\b",
    re.IGNORECASE,
)

# Phrases that make a task about the ACCOUNT through the API. A write phrased
# this way is satisfied by the API when it is configured and authorized.
_API_INTENT = re.compile(
    r"\b("
    r"via\s+the\s+api|through\s+the\s+api|use\s+the\s+api|api\s+(?:post|tweet|call|only)|"
    r"official\s+api|using\s+the\s+official"
    r")\b",
    re.IGNORECASE,
)


def browser_intent(text: str) -> bool:
    """Whether the request is explicitly about driving the x.com website."""
    return bool(_BROWSER_INTENT.search(str(text or "")))


def api_intent(text: str) -> bool:
    """Whether the request explicitly asks for the official API."""
    return bool(_API_INTENT.search(str(text or "")))


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------
@dataclass
class RouteDecision:
    """Why a method was chosen. Returned to the agent verbatim so it can explain."""

    method: str
    operation: str
    reason: str
    # True when the requested method was not available and another was used.
    fallback: bool = False
    # What the operator asked for, before eligibility was considered.
    requested: str = AUTO
    # Set when neither method can run, so the agent reports this instead of trying.
    blocked_reason: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked_reason

    def as_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "operation": self.operation,
            "reason": self.reason,
            "fallback": self.fallback,
            "requested_method": self.requested,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }


def choose_method(
    operation: str,
    *,
    preference: str = MODE_AUTO,
    requested: str = AUTO,
    api_available: bool = False,
    api_unavailable_reason: str = "",
    browser_available: bool = False,
    browser_unavailable_reason: str = "",
    task_text: str = "",
) -> RouteDecision:
    """Pick the method for one operation, or explain why neither is possible."""
    op = OPERATIONS.get(operation)
    if op is None:
        return RouteDecision(
            method="",
            operation=operation,
            reason="",
            requested=requested,
            blocked_reason=(
                f"'{operation}' is not an X operation this integration implements. Supported: "
                f"{', '.join(sorted(OPERATIONS))}."
            ),
        )

    requested = (requested or AUTO).strip().lower()
    if requested not in METHODS:
        requested = AUTO
    preference = (preference or MODE_AUTO).strip().lower()
    if preference not in PREFERENCES:
        preference = MODE_AUTO

    notes = [op.api_caveat] if op.api_caveat else []

    # A request that is explicitly about the website overrides the preference.
    # "Go to x.com and post this" means the website, and quietly posting through
    # the API would be answering a question nobody asked.
    if browser_intent(task_text) and op.browser:
        requested = BROWSER

    can_api = op.api and api_available
    can_browser = op.browser and browser_available

    order: List[Tuple[str, str]] = []
    if requested == API:
        order = [(API, "The task asked for the official API."), (BROWSER, "")]
    elif requested == BROWSER:
        order = [(BROWSER, "The task asked for the browser."), (API, "")]
    elif preference == MODE_API_PREFERRED or api_intent(task_text):
        order = [(API, "API is preferred."), (BROWSER, "")]
    elif preference == MODE_BROWSER_PREFERRED or browser_intent(task_text):
        order = [(BROWSER, "Browser is preferred."), (API, "")]
    else:
        # auto: a write goes to the API when it is available, because the API is
        # the method that produces a verifiable identifier. A read follows the
        # same rule. The browser is the fallback, not the default.
        order = [(API, "auto mode prefers the API for this operation."), (BROWSER, "")]

    fallback_reason = ""
    for method, why in order:
        if method == API:
            if can_api:
                return RouteDecision(
                    method=API, operation=operation,
                    reason=f"{why} {api_unavailable_reason}".strip() or why,
                    fallback=bool(fallback_reason), requested=requested, notes=notes,
                )
            fallback_reason = fallback_reason or api_unavailable_reason
            continue
        if can_browser:
            decision = RouteDecision(
                method=BROWSER, operation=operation,
                reason=(f"{why} " + fallback_reason).strip() if fallback_reason else why,
                fallback=bool(fallback_reason), requested=requested, notes=notes,
            )
            return decision

    # Neither method is usable. Report both reasons: an operator can only fix
    # this if they are told which half is missing.
    missing = []
    if op.api and api_unavailable_reason:
        missing.append(f"API: {api_unavailable_reason}")
    if op.browser and browser_unavailable_reason:
        missing.append(f"Browser: {browser_unavailable_reason}")
    return RouteDecision(
        method="", operation=operation, reason="",
        requested=requested,
        blocked_reason=(
            f"'{op.label}' cannot be done right now. " + " ".join(missing) if missing
            else f"'{op.label}' is not supported by either method."
        ),
    )


def describe_catalog() -> Dict[str, Any]:
    """Machine-readable summary of what this integration can do, by method."""
    return {
        "preferences": list(PREFERENCES),
        "methods": [API, BROWSER],
        "operations": [
            {
                "name": op.name,
                "label": op.label,
                "write": op.is_write,
                "supported_by": op.supported_by,
                "api_scope": op.api_scope,
                "api_caveat": op.api_caveat,
                "browser_note": op.browser_note,
            }
            for op in sorted(OPERATIONS.values(), key=lambda o: o.name)
        ],
    }