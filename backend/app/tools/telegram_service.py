"""Reusable Telegram service layer for the AirdropOS Telegram tools.

This module contains NO tools. It is the internal surface that every Telegram
tool module (session, bot, message, group, campaign) builds on, so the safety
rules and the Telethon quirks live in exactly one place instead of being
re-copied into a dozen tool modules.

Why this exists
---------------
``telegram_user_tools.py`` already proved out the hard parts: the per-account
lock, pacing, daily cap, FloodWait mapping and the button-kind classification.
What it does not have is *history*. It reads the newest 1-20 messages with
``client.get_messages(entity, limit=...)`` and nothing else, so the agent can
never look at an older message, page backwards, or page towards a known id.

An airdrop campaign needs all three: "read what the bot said at the start",
"show me the page before this one", "jump to message 4321". So this module adds
paginated, cursor-based history retrieval on top of the existing session, and
nothing else about the connection model is changed.

The rules this module keeps
---------------------------
* **API pagination, never simulated scrolling.** A Telethon client is not a UI;
  there is nothing to scroll. History is retrieved with ``min_id``/``max_id``/
  ``offset_id`` pagination. The only UI scrolling in this project is in the
  browser (``browser_scroll``) for Mini Apps, and those tools are reused as-is.
* **Bounded and cancellable waits.** ``wait_for_new_messages`` polls with a
  deadline and reports an honest timeout instead of sleeping forever.
* **Secrets never leave.** Nothing here returns an ``access_hash``, a session
  string, an API hash, a phone number or a login code. ``callback_data`` is
  never returned raw either: a callback payload is opaque server-side state that
  must not be replayed into a later prompt, so only a stable digest is exposed.
* **Untrusted input stays untrusted.** Every message text goes through
  ``sanitize_external_text`` on the way out, exactly as in
  ``telegram_user_tools``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.app.tools.telegram_user_tools import (
    CLICKABLE_BUTTON_KINDS,
    REFUSED_BUTTON_REASONS,
    _button_kind,
    _describe_entity,
    _describe_message,
    _flat_buttons,
    _locate_button,
    _membership_state,
    _numeric_chat_id,
    _ref_payload,
    _type_names,
    looks_like_secret,
    parse_target,
)
from backend.app.tools.web_tools import sanitize_external_text

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------

# A page is a window of recent history. Telegram returns the newest-first by
# default; every helper here re-orders to chronological before returning so a
# caller never has to know which direction the client used.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Ceiling on how many pages one tool call may walk. A tool must never be able to
# pull a whole channel into the context window; the agent pages deliberately.
MAX_PAGES = 5

# How often the wait helpers re-read the chat. Telethon's client here has no
# update handler registered, so polling is the only way to see a bot answer.
POLL_INTERVAL_SECONDS = 1.5

# A wait is always bounded. A bot that never answers must not hold a tool slot
# (and therefore a per-account lock) open indefinitely.
DEFAULT_WAIT_TIMEOUT = 20.0
MAX_WAIT_TIMEOUT = 60.0

# Hard cap on how much message text one tool result may carry.
MAX_TEXT_CHARS = 2000


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class TelegramServiceError(Exception):
    """A service-level failure that a tool must turn into a ToolResult.

    Carries the same shape the session's error mapping produces so tools can
    translate it without knowing about Telethon.
    """

    def __init__(
        self,
        message: str,
        *,
        category: str = "TOOL_FAILURE",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class TargetNotFound(TelegramServiceError):
    """The username, link or numeric id does not resolve to a chat."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="NOT_FOUND", retryable=False)


class TargetForbidden(TelegramServiceError):
    """Telegram refused access to the chat for this account."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="FORBIDDEN", retryable=False)


class RateLimited(TelegramServiceError):
    """Telegram asked us to wait. Never bypassed; always surfaced with its cost."""

    def __init__(self, message: str, *, seconds: int = 0, retryable: bool = True) -> None:
        super().__init__(message, category="RATE_LIMITED", retryable=retryable)
        self.seconds = seconds


class AmbiguousButton(TelegramServiceError):
    """The requested button does not exist, or exists more than once.

    A campaign must never blind-click: if the state is unclear the caller gets
    the alternatives and re-reads instead of guessing.
    """

    def __init__(self, message: str, *, available: Sequence[Dict[str, Any]] = ()) -> None:
        super().__init__(message, category="AMBIGUOUS_INPUT", retryable=False)
        self.available = list(available)


class HumanRequired(TelegramServiceError):
    """The action cannot be automated and the operator must do it."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="HUMAN_INTERVENTION_REQUIRED", retryable=False)


# ----------------------------------------------------------------------
# Button / message description
# ----------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_TG_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>\"')\]]+", re.I)


def callback_ref(button: Any) -> Optional[str]:
    """A stable, opaque handle for a callback button.

    ``callback_data`` is server-side state the bot uses to decide what a press
    means. Returning it raw would put opaque bytes into the model's context and
    tempt it to hand the value back verbatim on a later, different chat, which is
    both useless and a small injection surface. A short digest is enough to
    refer to the same button across calls without leaking anything.
    """
    # Where the payload lives depends on the Telethon layer: the wrapper exposes it
    # as ``button.data``, the raw TL object carries it on the variant held in
    # ``button.button``, and some builds only put it on ``button.button.type.data``.
    # All three are probed so a callback button is never mistaken for one without a
    # payload (which would silently hide the fact that a press is stateful).
    data = getattr(button, "data", None)
    if data is None:
        raw = getattr(button, "button", None)
        for holder in (raw, getattr(raw, "type", None) if raw is not None else None):
            if holder is None:
                continue
            data = getattr(holder, "data", None)
            if data is not None:
                break
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        payload = str(data).encode("utf-8", "replace")
    if not payload:
        return None
    return "cb_" + hashlib.sha256(payload).hexdigest()[:12]


def describe_button(button: Any, *, row: int = 0, column: int = 0) -> Dict[str, Any]:
    """Full, structured description of one inline/reply keyboard button.

    Adds what ``_describe_message`` omits and an agent genuinely needs before
    deciding whether to press it: its grid position, whether the kind is
    pressable at all, *why* it is refused when it is not, and an opaque
    callback handle.
    """
    kind = _button_kind(button)
    return {
        "text": str(getattr(button, "text", ""))[:80],
        "kind": kind,
        "url": getattr(button, "url", None),
        "row": row,
        "column": column,
        "clickable": kind in CLICKABLE_BUTTON_KINDS,
        "refused_reason": REFUSED_BUTTON_REASONS.get(kind),
        "callback_ref": callback_ref(button),
    }


def describe_messages(
    messages: Sequence[Any], *, include_buttons: bool = True
) -> List[Dict[str, Any]]:
    """Describe messages chronologically, keeping ids, buttons and edit state.

    Built on ``_describe_message`` so the injection scrubbing and button kinds
    stay identical to the tools that already exist, then enriched: entities are
    summarised rather than dropped, an edit is visible (a bot that rewrites its
    instructions in place is normal and must not look like a new message), and
    the bot's own instructions are reachable without a second round trip.
    """
    described: List[Dict[str, Any]] = []
    for msg in messages:
        entry = _describe_message(msg)
        entry["entities"] = describe_entities(msg)
        entry["reply_to_message_id"] = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
        edit_date = getattr(msg, "edit_date", None)
        entry["edited"] = bool(edit_date)
        entry["edited_at"] = edit_date.isoformat() if edit_date else None
        entry["has_media"] = bool(
            getattr(msg, "media", None)
            or getattr(msg, "photo", None)
            or getattr(msg, "document", None)
        )
        entry["sender_id"] = getattr(getattr(msg, "sender_id", None), "__int__", lambda: None)() \
            if getattr(msg, "sender_id", None) is not None else None
        if not include_buttons:
            entry["buttons"] = []
        else:
            # Replace the trimmed button list with the enriched one, keeping order.
            enriched: List[Dict[str, Any]] = []
            for r, row in enumerate(getattr(msg, "buttons", None) or []):
                for c, button in enumerate(row):
                    enriched.append(describe_button(button, row=r, column=c))
            entry["buttons"] = enriched
        described.append(entry)
    return described


def describe_entities(msg: Any) -> List[Dict[str, Any]]:
    """Summarise a message's formatting entities as offsets plus a readable type.

    The raw Telethon entity objects carry UTF-16 offsets that are meaningless to
    a reader, and the useful part is only *which kinds* are present and roughly
    where. URLs inside text entities are the interesting ones: airdrop bots put
    their site, their channel and their Mini App in the message body far more
    often than on a button.
    """
    out: List[Dict[str, Any]] = []
    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)
        out.append(
            {
                "type": str(getattr(ent, "_", "") or type(ent).__name__).lower() or "unknown",
                "offset": getattr(ent, "offset", None),
                "length": getattr(ent, "length", None),
                "url": url,
            }
        )
    return out


def extract_urls(text: str) -> List[str]:
    """Every http(s) URL in a block of text, de-duplicated, order preserved.

    Telegram links first (``t.me/...``) is not a priority here, the caller
    filters. Nothing is fetched: extraction only names the destination.
    """
    found: List[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found[:25]


def telegram_links(text: str) -> List[str]:
    """Only the ``t.me`` links in a block of text: channels, groups, bots, invites."""
    out: List[str] = []
    for match in _TG_LINK_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url not in out:
            out.append(url)
    return out[:25]


def collect_buttons(messages: Sequence[Any]) -> List[Dict[str, Any]]:
    """Every button across a message window, newest message first.

    This is what "what can I actually do right now in this chat" means. Order is
    newest-first because the newest message is the one the bot just posted and
    the one whose buttons are still current.
    """
    ordered = sorted(messages, key=lambda m: getattr(m, "id", 0) or 0, reverse=True)
    seen: set = set()
    buttons: List[Dict[str, Any]] = []
    for msg in ordered:
        for r, row in enumerate(getattr(msg, "buttons", None) or []):
            for c, button in enumerate(row):
                entry = describe_button(button, row=r, column=c)
                entry["message_id"] = getattr(msg, "id", None)
                key = (entry["message_id"], entry["row"], entry["column"], entry["text"])
                if key in seen:
                    continue
                seen.add(key)
                buttons.append(entry)
    return buttons


# ----------------------------------------------------------------------
# Entity resolution
# ----------------------------------------------------------------------


async def resolve_entity(client: Any, ref: str) -> Tuple[Any, Dict[str, Any]]:
    """Resolve a username, t.me link or numeric id to ``(entity, safe_ref)``.

    Thin wrapper over the same resolution the existing tools use so that
    behaviour cannot drift, with the service's own error types on failure. An
    invite link is deliberately rejected here: it cannot address a chat for
    reading, only for joining.
    """
    text = str(ref or "").strip()
    if not text:
        raise TargetNotFound("No chat reference was given.")
    try:
        numeric = _numeric_chat_id(text)
        if numeric is not None:
            entity = await client.get_entity(numeric)
        else:
            target = parse_target(text)
            if target["kind"] == "invite":
                raise TargetNotFound(
                    "A private invitation link cannot be used to read messages. Join it with "
                    "telegram_join_chat first, then use the chat id or @username it returns."
                )
            entity = await client.get_entity(target["value"])
    except TelegramServiceError:
        raise
    except ValueError as exc:
        raise TargetNotFound(str(exc)) from exc
    except Exception as exc:
        raise TargetNotFound(
            f"Telegram could not resolve {text[:80]!r} ({type(exc).__name__})."
        ) from exc
    if entity is None:
        raise TargetNotFound(f"Telegram returned no chat for {text[:80]!r}.")
    return entity, _describe_entity(entity)


# ----------------------------------------------------------------------
# Paginated history
# ----------------------------------------------------------------------


@dataclass
class HistoryPage:
    """One page of history plus the cursor needed to ask for the next one."""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    #: id of the oldest message in this page; pass as ``before_id`` for the next one.
    oldest_id: Optional[int] = None
    #: id of the newest message in this page; pass as ``after_id`` to page forwards.
    newest_id: Optional[int] = None
    #: How many raw messages the client returned before ordering/filtering.
    fetched: int = 0
    #: True when Telegram may still have older messages behind this page.
    has_more_older: bool = False
    #: True when more than one page was walked, so the window is not "the latest N".
    paginated: bool = False
    #: Set when the caller asked for a window that is newer than what exists.
    reached_newest: bool = True

    def as_payload(self) -> Dict[str, Any]:
        return {
            "messages": self.messages,
            "count": len(self.messages),
            "fetched": self.fetched,
            "oldest_id": self.oldest_id,
            "newest_id": self.newest_id,
            "has_more_older": self.has_more_older,
            "paginated": self.paginated,
            "reached_newest": self.reached_newest,
        }


def _clamp_limit(limit: Any, *, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_PAGE_SIZE))


def _order_chronological(messages: Sequence[Any]) -> List[Any]:
    """Oldest first.

    Telethon returns newest-first from ``iter_messages`` but the fake and the
    high-level ``get_messages`` helper both have been seen returning oldest
    first. Sorting by id makes this independent of which client is underneath,
    which is exactly the bug ``test_telegram_click_locate.py`` already guards.
    """
    return sorted(list(messages), key=lambda m: getattr(m, "id", 0) or 0)


async def _raw_history(
    client: Any,
    entity: Any,
    *,
    limit: int,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    search: Optional[str] = None,
) -> List[Any]:
    """One raw page from the client, in whatever order it likes.

    Telethon's high-level API is ``client.get_messages(entity, limit=..., min_id=,
    max_id=, search=, offset_id=)``; ``min_id`` is exclusive and ``max_id``
    inclusive. Every keyword beyond ``entity``/``limit`` is passed defensively:
    an older Telethon (or the test double) may not accept them, and silently
    degrading to "newest N" is better than raising, because the caller can see
    from ``paginated`` that it did.
    """
    kwargs: Dict[str, Any] = {"limit": limit}
    if after_id is not None:
        kwargs["min_id"] = int(after_id)
    if before_id is not None:
        kwargs["max_id"] = int(before_id)
    if search:
        kwargs["search"] = str(search)

    getter = getattr(client, "get_messages", None)
    if getter is None:
        raise TelegramServiceError(
            "This Telegram client cannot read message history.", category="UNSUPPORTED_OPERATION"
        )

    try:
        result = await getter(entity, **kwargs)
    except TypeError:
        # The client does not accept the paging keywords. Fall back to the plain
        # newest-N read so the tool still returns real data rather than failing.
        result = await getter(entity, limit=limit)
    except Exception as exc:
        name = type(exc).__name__
        if name == "FloodWaitError":
            raise RateLimited(
                f"Telegram asked us to wait {int(getattr(exc, 'seconds', 0) or 0)}s (flood wait).",
                seconds=int(getattr(exc, "seconds", 0) or 0),
                retryable=int(getattr(exc, "seconds", 0) or 0) <= 120,
            ) from exc
        raise

    if result is None:
        return []
    # ``get_messages`` with limit returns a list; with ``ids=`` it returns one
    # message. Only the list form reaches here.
    if not isinstance(result, (list, tuple)):
        return [result]
    return list(result)


async def fetch_page(
    client: Any,
    entity: Any,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    search: Optional[str] = None,
) -> HistoryPage:
    """One bounded page of a chat's history, chronologically ordered.

    ``before_id`` walks backwards towards older messages, ``after_id`` forwards
    towards newer ones. Passing neither returns the newest ``limit`` messages,
    which is what an agent wants after an action.
    """
    limit = _clamp_limit(limit)
    raw = await _raw_history(
        client, entity, limit=limit, before_id=before_id, after_id=after_id, search=search
    )
    ordered = _order_chronological(raw)

    # A paging keyword the client silently dropped must not look like a filter.
    paginated = before_id is not None or after_id is not None
    if paginated and before_id is not None:
        ordered = [m for m in ordered if getattr(m, "id", 0) < int(before_id)]
    if paginated and after_id is not None:
        ordered = [m for m in ordered if getattr(m, "id", 0) > int(after_id)]

    # Over-fetch by one to learn whether anything older exists, then drop it.
    has_more = len(raw) > limit
    if has_more:
        ordered = ordered[:limit]

    ids = [getattr(m, "id", 0) for m in ordered if getattr(m, "id", None) is not None]
    return HistoryPage(
        messages=describe_messages(ordered),
        oldest_id=min(ids) if ids else None,
        newest_id=max(ids) if ids else None,
        fetched=len(raw),
        has_more_older=has_more or (before_id is not None and len(ordered) > 0),
        paginated=paginated,
        reached_newest=after_id is None,
    )


async def load_older(
    client: Any, entity: Any, *, before_id: Optional[int] = None, limit: int = DEFAULT_PAGE_SIZE
) -> HistoryPage:
    """The page of messages immediately older than ``before_id``.

    With no cursor, starts from the oldest message of the newest page, which is
    what "show me what came before" means without the caller having to know any
    id. This is the honest replacement for "scroll up": a Telethon client has no
    scroll position, only history.
    """
    limit = _clamp_limit(limit)
    if before_id is None:
        # No cursor: the caller has just been looking at the newest window, so
        # "older" means the page immediately behind it.
        #
        # The anchor is the OLDEST id of a small newest window, not the id returned by
        # a single-message read. `get_messages(limit=1)` returns the NEWEST message on
        # real Telethon and the OLDEST on some other clients, so anchoring on one
        # message pages against whichever end the underlying client happens to use --
        # the same order-dependence that made button location flaky before it was
        # sorted by id. Taking the minimum over a window is order-independent.
        window = await _raw_history(client, entity, limit=min(max(limit, 2), 10))
        ids = [getattr(m, "id", 0) for m in window if getattr(m, "id", None) is not None]
        if not ids:
            return HistoryPage()
        before_id = min(ids)
    return await fetch_page(client, entity, limit=limit, before_id=before_id)


async def scroll_to_message(
    client: Any, entity: Any, message_id: int, *, window: int = 5
) -> HistoryPage:
    """A window of context centred on one message.

    Implemented as one backwards page from ``message_id`` plus the forward walk
    back up to it, because a Telegram client can only page from a known cursor.
    """
    try:
        target = int(message_id)
    except (TypeError, ValueError) as exc:
        raise TelegramServiceError(
            "message_id must be an integer.", category="INVALID_INPUT"
        ) from exc

    raw = await _raw_history(client, entity, limit=1, before_id=target + 1)
    target_msg = next((m for m in raw if getattr(m, "id", None) == target), None)

    if target_msg is None:
        # Fall back to a direct single-message read, which is exact and cheap.
        try:
            fetched = await client.get_messages(entity, ids=target)
        except Exception as exc:
            raise TargetNotFound(
                f"Message {target} could not be read ({type(exc).__name__})."
            ) from exc
        if fetched is None:
            raise TargetNotFound(
                f"Message {target} does not exist in this chat, or Telegram would not return it."
            )
        target_msg = fetched

    window = max(1, min(int(window), 20))
    older = await _raw_history(client, entity, limit=window, before_id=target)
    newer = await _raw_history(client, entity, limit=window, after_id=target)

    combined: List[Any] = list(older) + [target_msg] + list(newer)
    # De-duplicate: the two pages can both return the target on a small chat.
    seen: set = set()
    unique: List[Any] = []
    for msg in _order_chronological(combined):
        mid = getattr(msg, "id", None)
        if mid in seen:
            continue
        seen.add(mid)
        unique.append(msg)

    ids = [getattr(m, "id", 0) for m in unique if getattr(m, "id", None) is not None]
    return HistoryPage(
        messages=describe_messages(unique),
        oldest_id=min(ids) if ids else None,
        newest_id=max(ids) if ids else None,
        fetched=len(combined),
        has_more_older=bool(older),
        paginated=True,
        reached_newest=not newer,
    )


async def search_messages(
    client: Any,
    entity: Any,
    query: str,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    pages: int = 1,
) -> HistoryPage:
    """Search a chat's history for ``query``.

    Uses Telegram's server-side search when the client supports it, and walks
    pages with a local substring filter when it does not. The page walk is
    bounded by ``pages`` (capped at ``MAX_PAGES``) precisely so "search a busy
    channel" cannot become an unbounded scan.
    """
    text = str(query or "").strip()
    if not text:
        raise TelegramServiceError("A search query is required.", category="INVALID_INPUT")
    limit = _clamp_limit(limit)
    pages = max(1, min(int(pages or 1), MAX_PAGES))

    matches: List[Any] = []
    fetched = 0
    cursor: Optional[int] = None
    walked = 0
    for _ in range(pages):
        raw = await _raw_history(client, entity, limit=limit, before_id=cursor)
        fetched += len(raw)
        walked += 1
        if not raw:
            break
        needle = text.lower()
        hits = [m for m in raw if needle in str(getattr(m, "message", "") or "").lower()]
        for hit in hits:
            if len(matches) < limit:
                matches.append(hit)
        oldest = min((getattr(m, "id", 0) for m in raw if getattr(m, "id", None) is not None),
                     default=None)
        if oldest is None or len(raw) < limit:
            break
        cursor = oldest
        if len(matches) >= limit:
            break

    ordered = _order_chronological(matches)
    ids = [getattr(m, "id", 0) for m in ordered if getattr(m, "id", None) is not None]
    return HistoryPage(
        messages=describe_messages(ordered),
        oldest_id=min(ids) if ids else None,
        newest_id=max(ids) if ids else None,
        fetched=fetched,
        has_more_older=walked >= pages and len(matches) >= limit,
        paginated=walked > 1,
        reached_newest=walked == 1 and cursor is None,
    )


# ----------------------------------------------------------------------
# Waiting
# ----------------------------------------------------------------------


def _clamp_timeout(timeout: Any, default: float = DEFAULT_WAIT_TIMEOUT) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return default
    return max(1.0, min(value, MAX_WAIT_TIMEOUT))


@dataclass
class WaitResult:
    """The outcome of a bounded wait: the new messages, or an honest timeout."""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    #: Raw message objects, for callers that need to press a button on one.
    raw: List[Any] = field(default_factory=list)
    received: bool = False
    timed_out: bool = False
    waited_seconds: float = 0.0
    polls: int = 0
    last_message_id: Optional[int] = None

    def as_payload(self) -> Dict[str, Any]:
        return {
            "messages": self.messages,
            "count": len(self.messages),
            "received": self.received,
            "timed_out": self.timed_out,
            "waited_seconds": round(self.waited_seconds, 2),
            "polls": self.polls,
            "last_message_id": self.last_message_id,
        }


async def wait_for_new_messages(
    client: Any,
    entity: Any,
    *,
    after_id: Optional[int] = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    include_own: bool = False,
    stable_polls: int = 1,
) -> WaitResult:
    """Poll until the other side answers, or the bounded timeout expires.

    Design notes that matter for airdrop bots:

    * A timeout is a **success-shaped result, not an exception**. A bot that is
      slow must not look like a tool failure; the caller decides what to do with
      "nothing arrived yet". ``timed_out`` and ``waited_seconds`` are always
      reported honestly, never rounded away.
    * The loop waits for the burst to settle (``stable_polls`` consecutive reads
      with no growth) rather than returning on the first message, because a bot
      posts its instructions across several messages and a caller that reads the
      first one alone sees half the task list.
    * ``include_own`` keeps messages we sent, which is what "wait for my /start"
      looks like from the client side.
    """
    timeout = _clamp_timeout(timeout)
    started = time.monotonic()
    deadline = started + timeout
    stable = max(1, int(stable_polls or 1))

    floor = int(after_id) if after_id is not None else None
    if floor is None:
        # No cursor: start from the newest message already present, so only
        # genuinely new traffic counts as a reply.
        raw = await _raw_history(client, entity, limit=1)
        floor = max(
            (getattr(m, "id", 0) for m in raw if getattr(m, "id", None) is not None), default=0
        )

    seen: List[Any] = []
    last_count = -1
    unchanged = 0
    polls = 0

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            break
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.05, timeout - elapsed)))
        polls += 1

        raw = await _raw_history(client, entity, limit=20)
        fresh = [
            m
            for m in raw
            if (getattr(m, "id", 0) or 0) > floor
            and (include_own or not getattr(m, "out", False))
        ]
        if len(fresh) == last_count and fresh:
            unchanged += 1
            if unchanged >= stable:
                seen = fresh
                break
        else:
            unchanged = 0
        last_count = len(fresh)
        seen = fresh

        if seen and time.monotonic() >= deadline:
            break

    waited = time.monotonic() - started
    ordered = _order_chronological(seen)
    return WaitResult(
        messages=describe_messages(ordered),
        raw=ordered,
        received=bool(ordered),
        timed_out=not ordered,
        waited_seconds=waited,
        polls=polls,
        last_message_id=max((getattr(m, "id", 0) for m in ordered), default=None) or None,
    )


async def wait_for_bot_reply(
    client: Any,
    entity: Any,
    *,
    after_id: Optional[int] = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
) -> WaitResult:
    """Wait specifically for the bot, not for any inbound traffic.

    ``telegram_wait_for_bot_response`` is about "did the bot answer *me*". An
    incoming message from a human in a group chat is not that. When the chat
    entity is known to be a bot (which is the only place this is used) every
    non-own message is the bot's; the ``include_own=False`` filter is what makes
    it correct, and the entity check happens at the tool layer.
    """
    return await wait_for_new_messages(client, entity, after_id=after_id, timeout=timeout)


# ----------------------------------------------------------------------
# Buttons
# ----------------------------------------------------------------------


@dataclass
class ButtonTarget:
    """A located button plus everything a caller needs before pressing it."""

    message: Any
    button: Any
    described: Dict[str, Any]
    #: Every button seen in the search window, so an ambiguous request can be
    #: answered with the real alternatives instead of a guess.
    available: List[Dict[str, Any]] = field(default_factory=list)


async def locate_button(
    client: Any, entity: Any, wanted: str, *, message_id: Optional[int] = None
) -> ButtonTarget:
    """Find the button to press, or raise ``AmbiguousButton`` with the alternatives.

    Delegates the search to the existing ``_locate_button`` so the "message_id
    is a hint, not a requirement" behaviour and the newest-first ordering stay
    in one place. The extra work here is refusing to proceed when the state is
    not understood: the caller gets the real button list and must re-read.
    """
    needle = str(wanted or "").strip().lower()
    if not needle:
        raise TelegramServiceError("A button label is required.", category="INVALID_INPUT")

    message, button, flat = await _locate_button(client, entity, needle, message_id)
    available = [
        describe_button(b) for b in flat
    ]

    if button is None:
        labels = sorted({str(getattr(b, "text", "")).strip() for b in flat if getattr(b, "text", "")})
        hint = (
            " Re-read the chat first: a bot that has not posted the button yet cannot be pressed."
            if not labels
            else f" Available buttons: {', '.join(labels[:15])}."
        )
        raise AmbiguousButton(
            f"No button labelled {str(wanted)[:60]!r} is currently in this chat.{hint}",
            available=available,
        )

    described = describe_button(button)
    described["message_id"] = getattr(message, "id", None)
    return ButtonTarget(
        message=message, button=button, described=described, available=available
    )


def assert_pressable(target: ButtonTarget) -> None:
    """Refuse to press anything the operator has to press themselves.

    The kinds mirror ``CLICKABLE_BUTTON_KINDS`` in the tools module, so this can
    never be looser than what ``telegram_click_button`` already enforces. A Mini
    App button routes to the Mini App path instead of being pressed blind.
    """
    kind = target.described.get("kind")
    if kind in CLICKABLE_BUTTON_KINDS:
        return
    reason = REFUSED_BUTTON_REASONS.get(kind, "This button type is not supported for automated pressing.")
    raise HumanRequired(
        f"Button {target.described.get('text', '')[:60]!r} cannot be pressed automatically: {reason}"
    )


def buttons_in_message(message: Any) -> List[Dict[str, Any]]:
    """Every button on one message, enriched and in grid order."""
    return collect_buttons([message])


# ----------------------------------------------------------------------
# Membership
# ----------------------------------------------------------------------


async def membership_state(client: Any, entity: Any) -> Tuple[Optional[bool], str]:
    """``(is_member, method)``; ``None`` means "could not be determined".

    Delegates to the existing ``_membership_state``, which probes Telegram for
    the account's own participant record first and only falls back to scanning
    dialogs. ``None`` must never be reported as success — that distinction is
    the whole reason a campaign cannot fake a join.
    """
    return await _membership_state(client, entity)


async def verify_membership(
    client: Any, entity: Any, ref: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Membership with an explicit ``verified`` flag that cannot be over-read.

    Returns ``is_member`` (True/False/**None**), ``verified`` (True only when
    Telegram itself proved it), and ``method``. A caller that wants to record a
    join must test ``verified``, never ``is_member``.
    """
    state, method = await membership_state(client, entity)
    payload: Dict[str, Any] = {
        "is_member": state,
        "verified": state is True,
        "membership_method": method,
    }
    if state is None:
        payload["unverified_reason"] = (
            "Telegram did not return an authoritative participant record for this account."
        )
    if ref:
        payload.update(_ref_payload(ref))
    return payload


# ----------------------------------------------------------------------
# Outbound safety
# ----------------------------------------------------------------------


def assert_not_secret(text: str) -> None:
    """Refuse outgoing text that looks like a key or a seed phrase.

    The same rule the send tool already applies, exposed here so a command or a
    reply built by another module cannot skip it.
    """
    if looks_like_secret(text):
        raise TelegramServiceError(
            "Refused: the text looks like a private key or a seed phrase. Only public "
            "addresses and ordinary messages may be sent.",
            category="SECRET_BLOCKED",
        )


def assert_rate_budget(seconds: int) -> None:
    """Raise when a FloodWait must be waited out rather than retried.

    Never bypassed. A caller that wants to retry must wait the stated number of
    seconds first; there is no path here that sleeps and pretends otherwise.
    """
    if seconds > 0:
        raise RateLimited(
            f"Telegram asked us to wait {seconds}s before this chat will answer again.",
            seconds=seconds,
            retryable=seconds <= 120,
        )


# ----------------------------------------------------------------------
# Instruction extraction
# ----------------------------------------------------------------------

# A bot's task list is usually a numbered or bulleted line. These are the shapes
# that actually appear in airdrop bots, kept deliberately narrow so a sentence
# of prose is never mistaken for a task.
_TASK_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:\d+|[IVXLC]+)\s*[\.\)]\s+"       # 1. 2)  IV.
    r"|[-*•‣▪]\s+"                          # - * •
    r"|\[[ xX✓✔]\]\s*"                       # [ ] [x]
    r"|[✅☑✔✓]\s*"                           # ✅
    r")(?P<body>\S.*)$",
    re.UNICODE,
)

# Words that make a line an instruction to perform something, rather than prose.
_ACTION_HINT_RE = re.compile(
    r"\b(?:join|follow|subscribe|verify|check|claim|complete|connect|sign|visit|open|"
    r"read|watch|reply|answer|press|click|start|launch|register|mint|swap|bridge|"
    r"hold|invite|submit|confirm|share|retweet|like|send|go to|navigate)\b",
    re.I,
)


# `sanitize_external_text` rewrites a suspicious LINE into
#   "[UNTRUSTED-CONTENT-FLAGGED: <marker>] <line>"
# before the text ever reaches a tool result. A candidate extractor that runs on that
# already-rewritten text then re-tests the rewritten string: the numbered-bullet
# pattern no longer matches (the line now starts with "[") and the bare-action pattern
# usually does not either. The effect is the worst possible one for safety -- the
# injection is flagged and annotated, and the requirement it was hiding silently
# vanishes from the record entirely.
#
# So the marker is stripped for MATCHING only. The text is still untrusted, still
# carries its flag, and is still never treated as an instruction; removing the prefix
# here only lets the extractor see the shape the bot actually wrote.
_FLAG_PREFIX_RE = re.compile(r"^\s*(?:\[UNTRUSTED-CONTENT-FLAGGED:[^\]]*\]\s*)+")


def strip_flag_prefix(line: str) -> str:
    """Remove the sanitizer's annotation prefix so a flagged line can still be parsed.

    Matching only. The caller keeps the flagged form for anything it returns.
    """
    return _FLAG_PREFIX_RE.sub("", str(line or ""))


def looks_like_task_line(line: str) -> bool:
    """Whether one line reads as a discrete, actionable requirement."""
    stripped = strip_flag_prefix(line).strip()
    if not stripped or len(stripped) > 400:
        return False
    if _TASK_LINE_RE.match(stripped) and _ACTION_HINT_RE.search(stripped):
        return True
    # A bare action phrase on its own line is a task even without a bullet.
    return bool(_ACTION_HINT_RE.search(stripped)) and len(stripped) < 160 and stripped.endswith((".", "!", ")"))


def extract_task_lines(text: str, *, max_items: int = 40) -> List[str]:
    """The actionable requirement lines in a bot's message.

    Deliberately deterministic and deliberately dumb: this is a *candidate*
    extractor that narrows a long message down to the lines that look like
    requirements. It is not the judge of what those requirements mean — the
    fixed LLM does that from these candidates, and the human reads the same
    text. Returning raw lines (never instructions to the model) is what keeps a
    bot from steering the agent through a text-preprocessing step.

    A flagged line is still returned, WITH its flag intact, so the checklist records
    what the bot said even when that something is hostile.
    """
    out: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if looks_like_task_line(line):
            # Sanitize each line on its own so the returned candidate keeps its
            # injection annotation even when the input was already annotated.
            cleaned = sanitize_external_text(strip_flag_prefix(line), MAX_TEXT_CHARS)["text"].strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


@dataclass
class BotState:
    """Everything a caller needs to decide what to do next in one chat."""

    ref: Dict[str, Any] = field(default_factory=dict)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    last_message_id: Optional[int] = None
    last_bot_message_id: Optional[int] = None
    last_bot_text: str = ""
    task_lines: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    has_more_older: bool = False
    oldest_id: Optional[int] = None

    def as_payload(self, *, max_messages: int = 8) -> Dict[str, Any]:
        return {
            "chat": _ref_payload(self.ref),
            "messages": self.messages[-max_messages:],
            "available_buttons": self.buttons,
            "last_message_id": self.last_message_id,
            "last_bot_message_id": self.last_bot_message_id,
            "last_bot_text": self.last_bot_text,
            "candidate_task_lines": self.task_lines,
            "links": self.links,
            "has_more_older": self.has_more_older,
            "oldest_id": self.oldest_id,
        }


async def read_bot_state(
    client: Any,
    entity: Any,
    ref: Optional[Dict[str, Any]] = None,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    include_task_lines: bool = True,
) -> BotState:
    """Read a bot chat and summarise it into an actionable state.

    This is the read that the whole campaign rests on, so it deliberately
    returns the *current* state rather than a transcript: the latest messages,
    every button currently on offer (newest first, with pressability and the
    reason for refusal), the newest bot message and its id (the cursor the next
    wait needs), and the candidate task lines.

    ``last_bot_message_id`` is the piece the "return to the bot after joining a
    channel" workflow depends on: it is how a later step knows the bot has not
    yet seen anything new, so it must not click a stale Verify button again.
    """
    page = await fetch_page(client, entity, limit=limit)
    resolved = ref or (await resolve_entity(client, str(getattr(entity, "id", ""))))[1] \
        if ref is None and getattr(entity, "id", None) is not None else (ref or {})

    # Recompute button/message ids from the raw window so last_bot_* is exact.
    raw = await _raw_history(client, entity, limit=limit)
    ordered = _order_chronological(raw)

    last_bot: Any = None
    for msg in reversed(ordered):
        if not getattr(msg, "out", False):
            last_bot = msg
            break
    last_bot_text = str(getattr(last_bot, "message", "") or "") if last_bot is not None else ""
    scrubbed = sanitize_external_text(last_bot_text, MAX_TEXT_CHARS)["text"]

    return BotState(
        ref=resolved,
        messages=page.messages,
        buttons=collect_buttons(ordered),
        last_message_id=page.newest_id,
        last_bot_message_id=getattr(last_bot, "id", None) if last_bot is not None else None,
        last_bot_text=scrubbed,
        task_lines=extract_task_lines(last_bot_text) if include_task_lines else [],
        links=telegram_links(last_bot_text) if include_task_lines else [],
        has_more_older=page.has_more_older,
        oldest_id=page.oldest_id,
    )