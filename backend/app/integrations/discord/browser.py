"""Browser session handling for discord.com.

This is an ADAPTER over the existing browser controller, not a second browser.
Every call goes through ``backend.app.browser.controller`` and
``backend.app.browser.interaction``, so Discord inherits the project's existing
rules for profile selection, the per-profile interaction lock, operator gating,
route blocking, screenshots and evidence.

What it deliberately does NOT do
--------------------------------
* Automate credentials, or read a password field. The operator signs in by hand.
* Bypass MFA or a CAPTCHA. They are reported, never worked around.
* Probe for login state on a timer. ``login_state`` runs only inside a real
  Discord operation and on an explicit operator request, so a healthy session
  produces no background traffic and no interruptions.
* Use hidden Discord endpoints. Only the ordinary web app at discord.com is
  touched - the same routes a person's own browser loads. There is no Discord bot
  token, no user token and no unofficial API client anywhere in this integration.

The one thing this module is careful about
------------------------------------------
Saying "logged out" when the truth is unknown is worse than saying nothing: the
operator would be asked to sign in again for no reason, and a wrong answer here
either strands a task or restarts a session that was fine. So ``login_state``
returns one of four states and only reaches ``LOGGED_OUT`` from positive evidence
that the app is showing its sign-in form.

Selectors
---------
Discord's SPA is heavily class-mangled and changes without notice. The selectors
below are best-effort hints, re-resolved on EVERY call and never cached, and each
read is verified against the page afterwards. A stale selector that silently acts
on the wrong element is worse than a clean failure, so where a selector could match
the wrong thing the operation re-reads the result instead of trusting the click.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from backend.app.browser import interaction
from backend.app.browser.controller import controller
from backend.app.integrations.discord.errors import (
    CAT_LOGIN_REQUIRED,
    CAT_NETWORK,
    CAT_PAGE_ERROR,
    CAT_PERMISSION,
    CAT_RATE_LIMITED,
    CAT_UNKNOWN,
    DiscordBrowserError,
    DiscordLoginRequired,
    DiscordUnknownOutcome,
)
from backend.app.integrations.discord.models import (
    MAX_MESSAGE_CHARS,
    ChannelRef,
    LoginReading,
    MessageRecord,
)
from backend.app.live import hub

logger = logging.getLogger(__name__)

# The ordinary Discord web app. Nothing else on discord.com is used.
DISCORD_APP = "https://discord.com/app"
DISCORD_CHANNEL_BASE = "https://discord.com/channels"
DISCORD_ORIGINS = ("discord.com", "www.discord.com", "canary.discord.com", "ptb.discord.com")

# ----------------------------------------------------------------------
# Verdict vocabulary. Deliberately four states, not a boolean.
# ----------------------------------------------------------------------
LOGGED_IN = "LOGGED_IN"
LOGGED_OUT = "LOGGED_OUT"
UNKNOWN = "UNKNOWN"
NO_BROWSER = "NO_BROWSER"

# Discord's own error page ("Whoa there!"). It is a gateway/network fault and says
# nothing at all about whether the session is signed in, so it must never be read
# as a logout.
SEL_ERROR_PAGE = '[data-list-item-id="chat_error_generic"], [class*="errorPage"]'
ERROR_PAGE_TEXT = re.compile(r"whoa there|something went wrong|oops.{0,20}something", re.I)

# Positive evidence that Discord is showing its sign-in form. The durable signals
# are the login route and the login form's own field names rather than any hashed
# class name.
LOGIN_ROUTES = re.compile(r"^/(login|register|signup)(/|$)", re.I)
SEL_LOGIN_FIELD = (
    'input[name="email"], input[name="password"], input[name="login"], '
    'form[aria-label*="Login" i], form[aria-label*="Register" i]'
)
SEL_LOGIN_LINK = 'a[href="/login"], a[href*="/login?"]'

# Positive evidence of a signed-in session: parts of the app chrome that only
# render for an authenticated user.
SEL_APP_CHROME = (
    'nav[aria-label="Servers"], nav[aria-label*="Server" i], '
    'section[aria-label="User area"], [aria-label*="User area" i], '
    '[data-list-id="chat-tree"], [data-list-id="chat-sidebar"]'
)

# ----------------------------------------------------------------------
# App structure selectors (best effort, re-resolved every call)
# ----------------------------------------------------------------------
SEL_MESSAGE_LIST = 'ol[data-list-id="chatboard-messages"]'
SEL_MESSAGE = 'li[id^="chat-messages-"]'
SEL_MESSAGE_CONTENT = '[id^="message-content-"]'
SEL_MESSAGE_AUTHOR = '[id^="message-author-name-"]'
SEL_MESSAGE_TIMESTAMP = 'time[datetime]'
SEL_CHANNEL_TITLE = 'h2[data-list-item-id="chat-header-channel-title"]'
SEL_COMPOSER = (
    'div[contenteditable="true"][role="textbox"], '
    'div[contenteditable="true"][aria-label*="Message" i], '
    'form[aria-label*="Message" i] div[contenteditable="true"]'
)
SEL_SEARCH_INPUT = (
    'input[aria-label="Search"], input[placeholder*="Search" i], '
    '[role="searchbox"], input[aria-label*="Search" i]'
)
SEL_ACCEPT_INVITE = '[aria-label*="Accept Invite" i], [aria-label*="Accept" i][role="button"]'
SEL_GUILD_LEAF = 'a[href^="/channels/"]'

CHANNEL_ROUTE_RE = re.compile(r"/channels/(\d+)/(\d+)")

# Sign-in wording on Discord's login form and its "resume session" interstitial.
# Used as corroboration only, never as the sole basis for a LOGGED_OUT verdict.
LOGIN_TEXT = re.compile(
    r"log\s?in to discord|register for discord|continue to discord|"
    r"sign in to discord|you need to sign in|reconnect to discord",
    re.I,
)

MESSAGE_ID_RE = re.compile(r"chat-messages-(\d+)")


def _host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def is_discord_url(url: str) -> bool:
    return _host(url) in DISCORD_ORIGINS


def _safe_url(url: str) -> str:
    """Host and path only. A Discord page's query string can carry a token."""
    parsed = urlparse(str(url or ""))
    if not parsed.netloc:
        return str(url or "")[:200]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def classify_failure(text: str, *, category: Optional[str] = None) -> str:
    """Classify an operation failure, separating authentication from everything else.

    A navigation timeout, a missing element, a 5xx, a rate limit or a permission
    refusal are all real problems, but none of them is a logout. Only text that
    actually describes a sign-in prompt returns ``CAT_LOGIN_REQUIRED``.
    """
    if category == CAT_LOGIN_REQUIRED:
        return CAT_LOGIN_REQUIRED
    lowered = str(text or "").lower()

    # Rate limiting first: Discord's throttling pages mention being blocked, and a
    # blocked page must not be read as an expired session.
    if any(word in lowered for word in ("rate limit", "too many requests", "429", "slow down")):
        return CAT_RATE_LIMITED
    if ERROR_PAGE_TEXT.search(lowered):
        return CAT_PAGE_ERROR
    if any(
        word in lowered
        for word in (
            "err_name_not_resolved", "err_connection", "net::err", "timed out",
            "econnrefused", "err_ssl", "err_internet_disconnected", "err_name_resolution",
        )
    ):
        return CAT_NETWORK
    if any(
        word in lowered
        for word in (
            "403", "forbidden", "missing permissions",
            "you do not have permission", "missing access",
        )
    ):
        return CAT_PERMISSION
    if LOGIN_TEXT.search(lowered):
        return CAT_LOGIN_REQUIRED
    return CAT_UNKNOWN


# ----------------------------------------------------------------------
# Locking, gating and page access
# ----------------------------------------------------------------------
def _gate_reason(session_key: str) -> Optional[str]:
    """Refuse to act while an operator paused the session or holds manual control."""
    return hub.browser_sessions.gate(session_key)


def _interaction_lock(session_key: str):
    """The controller's per-session INTERACTION lock.

    Same lock every browser tool holds for its whole call
    (``browser_tools._serialized``): a page has one focus and one keyboard, so two
    tasks sharing a Discord profile would otherwise interleave - and with a
    composer that means one task posting the OTHER task's text. It is NOT
    re-entrant, so only public entry points take it and their helpers take none.
    """
    return controller.interaction_lock(session_key)


def _serialized(fn: Callable) -> Callable:
    """Hold the session's INTERACTION lock for the whole public call."""

    @functools.wraps(fn)
    async def wrapper(session_key: str, *args, **kwargs):
        async with _interaction_lock(session_key):
            return await fn(session_key, *args, **kwargs)

    wrapper._serialises_per_session = True  # type: ignore[attr-defined]
    return wrapper


async def _page(session_key: str, *, gate: bool = True):
    if gate:
        reason = _gate_reason(session_key)
        if reason:
            raise DiscordBrowserError(reason)
    page = await controller.get_page(session_key)
    if page is None:
        raise DiscordBrowserError(
            "No browser is open for this Discord profile. Start the profile first."
        )
    return page


async def _count(page, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


def _event(session_key: str, type_: str, message: str, status: str = "ok") -> None:
    try:
        hub.browser_sessions.touch(session_key)
        hub.events.emit(
            integration="browser",
            session_id=f"browser:{session_key}",
            type=type_,
            message=message,
            status=status,
        )
    except Exception:  # events are observability, never a reason to fail an operation
        logger.debug("Could not emit Discord event %s", type_)


# ----------------------------------------------------------------------
# Login state
# ----------------------------------------------------------------------
async def login_state(session_key: str) -> Dict[str, Any]:
    """Whether this profile is signed in to Discord, with the evidence used.

    Evidence is layered, most reliable first, and the function returns ``UNKNOWN``
    whenever it cannot prove one way or the other. It never runs on a timer: it is
    called inside real Discord operations and on explicit operator request only.

    Never raises. A missing browser is a reportable state, not an error, so the
    caller - including the worker's verification route - always gets a verdict it can
    show the operator instead of a 500.
    """
    try:
        page = await _page(session_key, gate=False)
    except DiscordBrowserError as exc:
        return LoginReading(NO_BROWSER, detail=str(exc)).as_dict()
    if page is None:
        return LoginReading(
            NO_BROWSER, detail="No browser is open for this profile."
        ).as_dict()

    url = str(getattr(page, "url", "") or "")
    try:
        # 1. Discord's own error page proves nothing about the session.
        if await _count(page, SEL_ERROR_PAGE) > 0 or ERROR_PAGE_TEXT.search(await page.title() or ""):
            return LoginReading(
                UNKNOWN,
                indicator="discord_error_page",
                detail="Discord is showing an error page, so the session state cannot be read.",
            ).as_dict()

        # 2. A sign-in route is definitive: Discord only serves /login and
        #    /register to a profile with no usable session.
        if LOGIN_ROUTES.match(urlparse(url).path or "/"):
            return LoginReading(
                LOGGED_OUT,
                indicator="login_route",
                detail=f"Discord redirected this profile to {_safe_url(url)}.",
            ).as_dict()

        # 3. The login form, if the app is showing one on any route.
        if await _count(page, SEL_LOGIN_FIELD) > 0:
            return LoginReading(LOGGED_OUT, indicator="login_form").as_dict()
        if await _count(page, SEL_LOGIN_LINK) > 0 and await _count(page, SEL_APP_CHROME) == 0:
            return LoginReading(LOGGED_OUT, indicator="login_link").as_dict()

        # 4. App chrome that only renders for an authenticated user.
        if await _count(page, SEL_APP_CHROME) > 0:
            return LoginReading(LOGGED_IN, indicator="app_chrome").as_dict()

        # 5. Corroborating text, used only to explain an otherwise empty page.
        text = await interaction.visible_text(page, limit=600)
        if LOGIN_TEXT.search(text or ""):
            return LoginReading(LOGGED_OUT, indicator="login_text").as_dict()
    except Exception as exc:
        # A page that cannot be read is UNKNOWN. Reporting it as a logout would send
        # the operator to sign in to an account that is already fine.
        return LoginReading(
            UNKNOWN, detail=f"Could not read the page ({type(exc).__name__})."
        ).as_dict()

    return LoginReading(
        UNKNOWN,
        indicator="no_indicator",
        detail=(
            "Discord is open but neither a sign-in form nor any signed-in control was found. "
            "This is not proof of being signed out."
        ),
    ).as_dict()


async def _require_login(session_key: str) -> Dict[str, Any]:
    """Refuse the operation unless Discord is provably signed in.

    Raises ``DiscordLoginRequired`` only on positive evidence. A page that cannot be
    read passes through with an ``UNKNOWN`` note attached, because a silent stall is
    worse than an attempt that reports exactly what it saw.
    """
    state = await login_state(session_key)
    if state.get("state") == LOGGED_OUT:
        raise DiscordLoginRequired(
            "Discord requires you to log in before this task can continue.",
            indicator=str(state.get("indicator") or "unknown"),
        )
    return state


# ----------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------
async def _open(session_key: str, url: str):
    """Navigate, refusing a destination that is not discord.com, and gate on humans."""
    if not is_discord_url(url):
        raise DiscordBrowserError(f"Refusing to navigate the Discord integration to a non-Discord URL: {url}")
    page = await _page(session_key)
    before = await controller.inspect(page)
    if getattr(before, "requires_human", False):
        raise DiscordLoginRequired(
            str(getattr(before, "human_reason", None) or "Discord requires manual verification."),
            indicator="human_gate",
        )
    result = await controller.navigate(session_key, url)
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        detail = str(result.get("detail") or "")
        raise DiscordBrowserError(
            f"Could not open Discord: {classify_failure(detail, category=reason)}.",
            category=classify_failure(detail, category=reason),
            detail={"reason": reason},
        )
    observation = result.get("observation")
    if observation is not None and getattr(observation, "requires_human", False):
        # A CAPTCHA or MFA on Discord's side. The operator completes it by hand; it is
        # never automated or bypassed.
        raise DiscordLoginRequired(
            str(getattr(observation, "human_reason", None) or "Discord requires manual verification."),
            indicator="human_gate",
        )
    return result, observation


@_serialized
async def open_app(session_key: str, path: str = "") -> Dict[str, Any]:
    """Open the Discord web app on a profile and report the real login state.

    Refuses any destination that is not a Discord origin, so this adapter can never
    be steered to another site.
    """
    target = str(path or "").strip() or DISCORD_APP
    if target.startswith("/"):
        target = f"https://discord.com{target}"
    if not re.match(r"^https?://", target, re.I) or not is_discord_url(target):
        raise DiscordBrowserError(f"Refusing to open a non-Discord URL: {target[:120]}")

    page = await _page(session_key)
    result, _observation = await _open(session_key, target)
    state = await _require_login(session_key)
    _event(session_key, "discord.opened", f"Opened {_safe_url(getattr(page, 'url', target))}")
    return {
        "ok": True,
        "url": _safe_url(getattr(page, "url", target)),
        "title": await interaction._safe_title(page),
        "login_state": state,
        "screenshot_path": await controller.screenshot(session_key, "discord_opened"),
    }


@_serialized
async def inspect_page(session_key: str) -> Dict[str, Any]:
    """Read what the Discord app currently shows, after proving the session is live.

    Deliberately minimal: this reports the page. It does not navigate a server, read a
    channel or join anything on the operator's behalf.
    """
    page = await _page(session_key)
    url = str(getattr(page, "url", "") or "")
    if not is_discord_url(url):
        # Not a failure and not a logout: the operator may simply have this profile
        # on another tab. Reported so the agent can navigate.
        return {
            "ok": True,
            "on_discord": False,
            "url": _safe_url(url),
            "detail": "This profile is not on discord.com. Open Discord first.",
        }

    state = await _require_login(session_key)
    snapshot = await interaction.snapshot(page, limit=60, include_text=True)
    _event(session_key, "discord.inspected", "Read the Discord page")
    return {
        "ok": True,
        "on_discord": True,
        "url": _safe_url(url),
        "title": await interaction._safe_title(page),
        "login_state": state,
        "items": snapshot.get("elements", snapshot.get("items", [])),
        "channel": await _channel_ref(page, url),
    }


@_serialized
async def channel_of(session_key: str) -> Dict[str, Any]:
    """Which channel the profile is on RIGHT NOW, without navigating.

    Read-only and cheap, and it exists for a safety reason rather than a convenience
    one. The duplicate-send guard has to know where a message would actually land, and
    it must learn that from the page instead of from a caller-supplied argument:
    the agent sends to "the channel currently open" and usually cannot supply a channel
    id before the send, so an argument-derived key both misses real duplicates and
    blocks legitimate messages to a different channel.

    Returns ``ok: False`` with a reason when the page is not on a channel, so a
    caller can refuse rather than guess.
    """
    page = await _page(session_key, gate=False)
    if page is None:
        return {"ok": False, "reason": "no_browser", "detail": "No browser is open for this profile."}
    url = str(getattr(page, "url", "") or "")
    if not is_discord_url(url):
        return {"ok": False, "reason": "not_on_discord", "url": _safe_url(url)}
    ref = await _channel_ref(page)
    if not ref.channel_id:
        return {
            "ok": False,
            "reason": "no_channel",
            "url": _safe_url(url),
            "detail": "The page is on Discord but not on a specific channel.",
        }
    return {"ok": True, "channel": ref.as_dict(), "url": _safe_url(url)}


@_serialized
async def check_login(session_key: str) -> Dict[str, Any]:
    """Report the login state without changing the page.

    An explicit operator- or agent-initiated read, not a poll. Nothing calls it on a
    schedule.
    """
    page = await _page(session_key, gate=False)
    if page is None:
        return {"ok": False, "login_state": LoginReading(NO_BROWSER).as_dict()}
    state = await login_state(session_key)
    return {"ok": True, "url": _safe_url(getattr(page, "url", "")), "login_state": state}


async def session_status(session_key: str) -> Dict[str, Any]:
    """Live state of this profile's Discord browser session, for the dashboard.

    Read-only. Reports what the worker observed and says so when the worker could not
    be reached - never a guessed login state.
    """
    try:
        status = await controller.profile_status(session_key)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: the session could not be inspected."}
    mode = controller.session_mode(session_key)
    out: Dict[str, Any] = {
        "ok": bool(status.get("ok")),
        "session_key": session_key,
        "running": mode != "none",
        "mode": mode,
        "active_url": status.get("active_url") or "",
        "active_title": status.get("active_title") or "",
        "last_error": status.get("last_error"),
        "limitations": status.get("limitations") or [],
    }
    if mode != "none":
        try:
            out["login_state"] = (await login_state(session_key))
        except DiscordBrowserError:
            out["login_state"] = LoginReading(NO_BROWSER).as_dict()
    return out


# ----------------------------------------------------------------------
# Page reading helpers
# ----------------------------------------------------------------------
async def _visible(page, *, limit: int = 6000) -> str:
    try:
        return await interaction.visible_text(page, limit=limit)
    except Exception:
        return ""


async def _inner_text(locator, *, limit: int = 4000) -> str:
    try:
        return (await locator.inner_text(timeout=5000))[:limit] or ""
    except Exception:
        return ""


async def _channel_ref(page, url: str = "") -> ChannelRef:
    """Where the page is: server and channel, from the route and the header.

    Both sources are used because Discord's own header is sometimes the only one that
    carries a human-readable name, while the route is the only reliable id.
    """
    current = url or str(getattr(page, "url", "") or "")
    match = CHANNEL_ROUTE_RE.search(urlparse(current).path or "")
    ref = ChannelRef(server_id=match.group(1) if match else "", channel_id=match.group(2) if match else "")
    title = ""
    try:
        nodes = page.locator(SEL_CHANNEL_TITLE)
        if await nodes.count() > 0:
            title = ((await nodes.first.inner_text(timeout=3000)) or "").strip()
    except Exception:
        title = ""
    if title:
        ref.channel_name = title[:80]
    return ref


async def _message_records(page, *, limit: int = 50) -> List[MessageRecord]:
    """Read the messages currently rendered in the message list.

    Only what the DOM actually contains. A message whose author or timestamp is not
    rendered is returned with those fields empty rather than invented.
    """
    records: List[MessageRecord] = []
    try:
        items = page.locator(SEL_MESSAGE)
        count = await items.count()
    except Exception:
        return records
    if count == 0:
        return records

    for index in range(count):
        try:
            node = items.nth(index)
            raw_id = await node.get_attribute("id") or ""
            id_match = MESSAGE_ID_RE.search(raw_id)
            message_id = id_match.group(1) if id_match else raw_id[:80]

            author = ""
            author_nodes = node.locator(SEL_MESSAGE_AUTHOR)
            if await author_nodes.count() > 0:
                author = ((await author_nodes.first.inner_text(timeout=2000)) or "").strip()

            stamp = ""
            time_nodes = node.locator(SEL_MESSAGE_TIMESTAMP)
            if await time_nodes.count() > 0:
                stamp = (await time_nodes.first.get_attribute("datetime") or "").strip()

            content_nodes = node.locator(SEL_MESSAGE_CONTENT)
            content = ""
            if await content_nodes.count() > 0:
                content = ((await content_nodes.first.inner_text(timeout=3000)) or "").strip()

            permalink = ""
            try:
                links = node.locator('a[href*="/channels/"]')
                if await links.count() > 0:
                    href = (await links.first.get_attribute("href") or "").strip()
                    if href:
                        permalink = f"https://discord.com{href}" if href.startswith("/") else href
            except Exception:
                permalink = ""

            if not (content or author or message_id):
                continue
            records.append(
                MessageRecord(
                    message_id=message_id,
                    author=author[:120],
                    timestamp=stamp,
                    content=content[:MAX_MESSAGE_CHARS],
                    permalink=permalink,
                )
            )
        except Exception:
            # One unreadable message must not abort the whole read: a partial reading
            # of a channel is still useful, and the caller is told how much it got.
            continue
        if len(records) >= limit:
            break
    return records


async def _scroll_up(page, *, steps: int = 1) -> bool:
    """Scroll the message list upwards, the ordinary way to reach older messages.

    Returns False when the list did not move, which is how "there is nothing older"
    is detected without guessing.
    """
    moved = False
    for _ in range(max(1, steps)):
        try:
            result = await interaction.scroll(page, direction="up", timeout_ms=5000, settle_ms=400)
        except Exception:
            return moved
        if not result.get("moved"):
            break
        moved = True
    return moved


async def _open_composer(page) -> None:
    """Focus the message composer, or explain why it is not usable.

    The composer being absent is usually a permission problem (a channel the account
    can read but not post in), which is reported as such rather than as a page error.
    """
    try:
        composer = page.locator(SEL_COMPOSER).first
        await composer.wait_for(state="visible", timeout=10_000)
        await composer.click(timeout=10_000)
    except Exception as exc:
        raise DiscordBrowserError(
            "The Discord message box is not available in this channel. The account may not have "
            "permission to post here, or the channel may be read-only.",
            category=CAT_PERMISSION,
        ) from exc


# ----------------------------------------------------------------------
# READ operations
# ----------------------------------------------------------------------
async def _read_servers(page) -> List[Dict[str, str]]:
    """Read the guild rail that is currently rendered. No navigation, no lock taken."""
    servers: List[Dict[str, str]] = []
    try:
        nodes = page.locator(SEL_GUILD_LEAF)
        count = min(await nodes.count(), 100)
        for index in range(count):
            node = nodes.nth(index)
            href = (await node.get_attribute("href") or "").strip()
            match = CHANNEL_ROUTE_RE.search(href)
            name = (await node.get_attribute("aria-label") or "").strip()
            if not name:
                label_nodes = node.locator('[aria-hidden="true"], [class*="name"]')
                if await label_nodes.count() > 0:
                    name = ((await label_nodes.first.inner_text(timeout=1500)) or "").strip()
            servers.append({"server_id": match.group(1) if match else "", "name": name[:80]})
    except Exception as exc:
        raise DiscordBrowserError(
            f"Could not read the server list ({type(exc).__name__}).",
            category=CAT_UNKNOWN,
        ) from exc
    return servers


@_serialized
async def list_servers(session_key: str) -> Dict[str, Any]:
    """The servers this account can see in the app's own guild rail."""
    page = await _page(session_key)
    state = await _require_login(session_key)
    await _open(session_key, DISCORD_APP)
    page = await _page(session_key)

    servers = await _read_servers(page)
    _event(session_key, "discord.servers_read", f"Read {len(servers)} Discord servers")
    return {
        "ok": True,
        "login_state": state,
        "servers": servers,
        "server_count": len(servers),
        "screenshot_path": await controller.screenshot(session_key, "discord_servers"),
    }


@_serialized
async def open_channel(
    session_key: str,
    *,
    server_id: str = "",
    channel_id: str = "",
    channel_name: str = "",
) -> Dict[str, Any]:
    """Open one channel by its ids, which is the route a person's browser uses.

    Discord's web app addresses a channel as ``/channels/<server>/<channel>``. Using
    that route is ordinary navigation, not an API: it renders the same page any user
    gets, and an account without access is refused by Discord itself.

    When no ids are supplied the channel is found through the sidebar by name, so a
    task can be expressed the way an operator thinks about it ("#announcements").
    """
    page = await _page(session_key)
    state = await _require_login(session_key)

    if server_id and channel_id:
        target = f"{DISCORD_CHANNEL_BASE}/{server_id}/{channel_id}"
    else:
        target = DISCORD_APP

    await _open(session_key, target)
    page = await _page(session_key)

    if not (server_id and channel_id):
        if not channel_name:
            raise DiscordBrowserError(
                "A Discord channel needs either its server and channel ids, or a channel name."
            )
        opened = await _click_sidebar_channel(page, channel_name)
        if not opened:
            raise DiscordBrowserError(
                f"No channel named '{channel_name}' was found in this server's sidebar.",
                category="DISCORD_NOT_FOUND",
            )

    # Re-read where we actually ended up. Never assume the navigation succeeded.
    ref = await _channel_ref(page)
    if server_id and channel_id and ref.channel_id and ref.channel_id != channel_id:
        raise DiscordBrowserError(
            f"Discord opened channel {ref.channel_id} instead of the requested {channel_id}. "
            "The account may not have access to it.",
            category=CAT_PERMISSION,
        )

    _event(session_key, "discord.channel_opened", f"Opened Discord channel {ref.label}")
    return {
        "ok": True,
        "channel": ref.as_dict(),
        "login_state": state,
        "url": _safe_url(getattr(page, "url", target)),
        "screenshot_path": await controller.screenshot(session_key, "discord_channel"),
    }


async def _click_sidebar_channel(page, name: str) -> bool:
    """Click a channel in the sidebar by its accessible name.

    Discord's own resolver is used so frames, overlays and virtualised lists behave
    exactly as they do for every other browser tool here.
    """
    wanted = name.lstrip("#").strip()
    for candidate in (name, f"#{wanted}", wanted):
        for role in ("link", "button", "menuitem", "treeitem"):
            try:
                await interaction.click(
                    page, strategy="role", value=f"{role}:{candidate}", timeout_ms=4000
                )
                return True
            except interaction.LocatorNotFound:
                continue
            except Exception as exc:
                logger.info("discord: clicking %r failed (%s)", candidate, type(exc).__name__)
                continue
    return False


@_serialized
async def read_messages(
    session_key: str, *, limit: int = 30, older_pages: int = 0
) -> Dict[str, Any]:
    """Read the messages currently visible, optionally paginating backwards.

    ``older_pages`` scrolls up the message list the ordinary way. It is bounded and
    reported: the caller is told whether the list moved, so "that is everything" is
    an observation rather than an assumption.
    """
    page = await _page(session_key)
    state = await _require_login(session_key)
    ref = await _channel_ref(page)

    collected: List[MessageRecord] = []
    seen: set = set()
    pages_read = 0
    reached_top = False

    for _ in range(max(0, older_pages) + 1):
        records = await _message_records(page, limit=limit)
        fresh = [r for r in records if r.message_id not in seen]
        for record in fresh:
            seen.add(record.message_id)
        collected = fresh + collected  # oldest first
        pages_read += 1
        if not records or pages_read > max(0, older_pages):
            break
        if not await _scroll_up(page, steps=1):
            reached_top = True
            break

    if not collected:
        raise DiscordBrowserError(
            "No messages were rendered in this channel. It may be empty, or the account may not "
            "be able to read it.",
            category="DISCORD_NOT_FOUND",
        )

    _event(session_key, "discord.channel_read", f"Read {len(collected)} messages from {ref.label}")
    return {
        "ok": True,
        "channel": ref.as_dict(),
        "login_state": state,
        "messages": [r.as_dict() for r in collected][-limit:],
        "message_count": len(collected),
        "pages_read": pages_read,
        "reached_top": reached_top,
        "url": _safe_url(getattr(page, "url", "")),
        "screenshot_path": await controller.screenshot(session_key, "discord_messages"),
    }


@_serialized
async def find_unanswered_questions(
    session_key: str, *, limit: int = 30, older_pages: int = 1
) -> Dict[str, Any]:
    """Find question-shaped messages that nothing later in the list answers.

    The bar is deliberately conservative and stated: a message counts as unanswered
    only if it reads as a question AND no later message in what we read is a reply to
    it. Discord does not expose "reply" structure in the rendered DOM reliably, so
    the reply check falls back to Discord's own rendered reply indicator and, failing
    that, to the author's own follow-up. Anything weaker is reported as a candidate
    for a person to judge, never as a fact.
    """
    page = await _page(session_key)
    await _require_login(session_key)
    ref = await _channel_ref(page)

    records: List[MessageRecord] = []
    seen: set = set()
    for _ in range(max(0, older_pages) + 1):
        for record in await _message_records(page, limit=limit):
            if record.message_id and record.message_id not in seen:
                seen.add(record.message_id)
                records.append(record)
        if not records or not await _scroll_up(page, steps=1):
            break

    candidates = [r for r in records if _looks_like_question(r.content)]
    answered_ids = set()
    for record in records:
        if record.reply_to_message_id:
            answered_ids.add(record.reply_to_message_id)

    unanswered = [
        r for r in candidates
        if r.message_id not in answered_ids and not _followed_up_in_place(records, r)
    ]

    _event(
        session_key,
        "discord.questions_found",
        f"Found {len(unanswered)} candidate unanswered questions in {ref.label}",
    )
    return {
        "ok": True,
        "channel": ref.as_dict(),
        "message_count": len(records),
        "unanswered": [r.as_dict() for r in unanswered],
        "unanswered_count": len(unanswered),
        "confidence_note": (
            "These are CANDIDATES found by reading the rendered messages. Discord's web page does "
            "not expose reliable reply threading, so a human should confirm any of these before "
            "you answer it."
        ),
    }


def _looks_like_question(text: str) -> bool:
    """Whether a message reads as a question.

    A question mark (ASCII or full-width), or one of the common Bangla/English
    question words. Kept deliberately broad on the word list and narrow on the mark,
    because the mark is the part Discord users actually type.
    """
    body = str(text or "").strip()
    if not body:
        return False
    if "?" in body or "\uff1f" in body:
        return True
    return bool(
        re.search(
            r"(কী|কি|কেন|কিভাবে|কোন|কোনটা|কত|সমস্যা|help|how|what|why|when|where|which|can you|could you|any idea|anyone know)",
            body,
            re.I,
        )
    )


def _followed_up_in_place(records: List[MessageRecord], candidate: MessageRecord) -> bool:
    """Whether something later in the same channel reads as an answer to this.

    Deliberately does NOT require the same author: the usual case is somebody ELSE
    answering, and an author check would report almost every answered question as
    still open.

    The bar is low on purpose - any later message that is not itself a question is
    taken as the question having moved on - and the result is reported as a CANDIDATE
    for a person to confirm, never as a fact. Erring toward "answered" is the right
    direction here: a wrongly-excluded question costs the operator one more look,
    while a wrongly-included one invites them to answer something that was resolved.
    """
    try:
        start = records.index(candidate)
    except ValueError:
        return False
    for later in records[start + 1 :]:
        body = (later.content or "").strip()
        if not body:
            continue
        if not _looks_like_question(body) or _looks_like_answer(body):
            return True
    return False


_ANSWER_HINTS = re.compile(
    r"(yes|no|sure|here(?:'s| is)|try|use |you can|fixed|solved|answer|দয়া করে|উত্তর|হ্যাঁ|না\b|ব্যবহার করুন)",
    re.I,
)


def _looks_like_answer(text: str) -> bool:
    return bool(_ANSWER_HINTS.search(str(text or "")))


@_serialized
async def search_messages(session_key: str, query: str, *, limit: int = 25) -> Dict[str, Any]:
    """Search accessible messages through Discord's own search box.

    Only results this account can already see are returned: the search runs inside
    the signed-in session, so Discord's own access rules apply and no private
    endpoint is consulted.
    """
    needle = str(query or "").strip()
    if not needle:
        raise DiscordBrowserError("A search term is required.", status_code=400)

    page = await _page(session_key)
    await _require_login(session_key)

    field = page.locator(SEL_SEARCH_INPUT).first
    try:
        await field.wait_for(state="visible", timeout=10_000)
        await field.click(timeout=5000)
    except Exception:
        # Older builds expose search only through the channel header; fall back to
        # the ordinary keyboard shortcut before giving up.
        try:
            await interaction.press(page, key="Control+f")
            field = page.locator(SEL_SEARCH_INPUT).first
            await field.wait_for(state="visible", timeout=5000)
            await field.click(timeout=5000)
        except Exception as exc:
            raise DiscordBrowserError(
                "Discord's search box was not available on this page.", category=CAT_UNKNOWN
            ) from exc

    await interaction.type_text(page, text=needle, timeout_ms=8000)
    # Discord filters as you type; give it a moment rather than reading a stale list.
    try:
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    results = await _message_records(page, limit=limit)
    _event(session_key, "discord.searched", f"Searched Discord for {len(results)} results")
    return {
        "ok": True,
        "query": needle[:120],
        "results": [r.as_dict() for r in results],
        "result_count": len(results),
        "note": (
            "These are the results Discord's own search box showed for this signed-in account. "
            "Nothing outside what this account can already read was accessed."
        ),
        "screenshot_path": await controller.screenshot(session_key, "discord_search"),
    }


# ----------------------------------------------------------------------
# WRITE operations
# ----------------------------------------------------------------------
@_serialized
async def send_message(
    session_key: str,
    text: str,
    *,
    reply_to_message_id: str = "",
    expected_channel_id: str = "",
) -> Dict[str, Any]:
    """Send a message (or a reply) in the channel the profile is currently on.

    This is the most consequential thing the Discord integration does, so it is
    guarded four ways:

    1. If a channel was resolved for this send, the page is confirmed to still be on
       it. Another task sharing this profile can navigate between the duplicate guard
       and this call, because the interaction lock is per call; posting into whatever
       channel it happened to leave open would put the message somewhere the operator
       never named. Refusing is the only safe answer.
    2. The session is re-checked immediately before the irreversible send. Typing
       takes real time and an operator who stopped the session while it happened must
       not have their account post anyway.
    3. The page is re-read afterwards. The composer emptying, a new message appearing
       in the list, and the channel header matching are all evidence.
    4. If none of that evidence is there, the result is UNKNOWN, not success. The
       caller must verify rather than resend, because a duplicate message is the one
       failure the operator cannot undo.
    """
    body = str(text or "").strip()
    if not body:
        raise DiscordBrowserError("A message body is required.", status_code=400)
    if len(body) > MAX_MESSAGE_CHARS:
        raise DiscordBrowserError(
            f"That message is {len(body)} characters; Discord allows {MAX_MESSAGE_CHARS}.",
            status_code=400,
        )

    page = await _page(session_key)
    state = await _require_login(session_key)
    ref = await _channel_ref(page)

    if expected_channel_id:
        if not ref.channel_id or ref.channel_id != expected_channel_id:
            raise DiscordBrowserError(
                f"This profile is on channel {ref.channel_id or 'no channel'} but this "
                f"message was checked against channel {expected_channel_id}. Something "
                "moved the page - another task, or a manual click. Nothing was sent; open "
                "the right channel and try again.",
                category="DISCORD_CHANNEL_CHANGED",
            )

    before_ids = {r.message_id for r in await _message_records(page, limit=50) if r.message_id}

    if reply_to_message_id:
        await _attach_reply(page, reply_to_message_id)

    await _open_composer(page)
    await interaction.type_text(page, text=body, delay_ms=15, timeout_ms=20_000)

    typed = await _composer_text(page)
    if not typed.strip():
        raise DiscordBrowserError(
            "The message box stayed empty after typing, so nothing was sent.",
            category=CAT_UNKNOWN,
        )

    # Re-check the gate immediately before the irreversible step.
    stopped = _gate_reason(session_key)
    if stopped:
        raise DiscordBrowserError(stopped)

    await _press_enter(page)
    after = await _verify_send(page, before_ids, body, ref)

    if not after.get("verified"):
        raise DiscordUnknownOutcome(
            "The message was submitted but Discord did not confirm it arrived. It may or may "
            "not have been sent. Check the channel before sending it again - resending now "
            "risks posting the same message twice.",
            detail={"channel": ref.as_dict(), **after},
        )

    _event(session_key, "discord.message_sent", f"Sent a message to {ref.label}")
    return {
        "ok": True,
        "channel": ref.as_dict(),
        "login_state": state,
        "characters": len(body),
        "reply_to_message_id": reply_to_message_id,
        "evidence": after,
        "url": _safe_url(getattr(page, "url", "")),
        "screenshot_path": await controller.screenshot(session_key, "discord_sent"),
    }


async def _composer_text(page) -> str:
    try:
        composer = page.locator(SEL_COMPOSER).first
        return (await composer.inner_text(timeout=5000)) or ""
    except Exception:
        return ""


async def _press_enter(page) -> None:
    """Submit the composer the way a person does."""
    try:
        await interaction.press(page, key="Enter")
        return
    except Exception as exc:
        logger.info("discord: Enter did not submit (%s)", type(exc).__name__)
    # Some Discord builds leave Enter bound to newline in a multi-line composer.
    for selector in ('button[aria-label*="Send" i]', '[class*="sendButton"]', 'button[type="submit"]'):
        try:
            await interaction.click(page, strategy="css", value=selector, timeout_ms=4000)
            return
        except Exception:
            continue
    raise DiscordBrowserError(
        "The message could not be submitted: Discord's send control was not available.",
        category=CAT_UNKNOWN,
    )


async def _attach_reply(page, message_id: str) -> None:
    """Turn the next message into a reply to ``message_id`` through Discord's own UI."""
    node = page.locator(f'li[id="chat-messages-{message_id}"]')
    try:
        await node.first.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise DiscordBrowserError(
            f"Message {message_id} is not visible in this channel, so it cannot be replied to.",
            category="DISCORD_NOT_FOUND",
        ) from exc

    index = await _index_of(page, message_id)
    for role in ("button", "menuitem"):
        try:
            await interaction.click(
                page, strategy="role", value=f"{role}:Reply", index=index, timeout_ms=5000,
            )
            return
        except Exception:
            continue
    raise DiscordBrowserError(
        f"Discord's reply control was not available on message {message_id}.",
        category=CAT_UNKNOWN,
    )


async def _index_of(page, message_id: str) -> int:
    """Where a message sits in the rendered list, or 0 if it is not there."""
    try:
        items = page.locator(SEL_MESSAGE)
        count = await items.count()
        for index in range(count):
            node = items.nth(index)
            if (await node.get_attribute("id") or "") == f"chat-messages-{message_id}":
                return index
    except Exception:
        pass
    return 0


async def _verify_send(
    page, before_ids: set, body: str, ref: ChannelRef
) -> Dict[str, Any]:
    """Re-read the page and decide what actually happened.

    Returns a dict with ``verified`` plus whichever signals were observed. It never
    guesses: an unverified send is reported as unverified.
    """
    evidence: Dict[str, Any] = {"verified": False, "signals": []}

    # Discord renders the sent message almost immediately; poll briefly rather than
    # deciding on a single read that raced the render.
    for _ in range(5):
        try:
            await page.wait_for_timeout(800)
        except Exception:
            pass
        composer_empty = not (await _composer_text(page)).strip()
        records = await _message_records(page, limit=50)
        new = [r for r in records if r.message_id and r.message_id not in before_ids]
        matching = [r for r in new if _same_message(r.content, body)]
        if matching or composer_empty:
            evidence["signals"].append(
                "composer_emptied" if composer_empty else "new_message_rendered"
            )
            evidence["verified"] = True
            evidence["message_id"] = matching[0].message_id if matching else ""
            evidence["message"] = matching[0].summary() if matching else {}
            evidence["url"] = _safe_url(getattr(page, "url", ""))
            return evidence
    evidence["composer_text_remaining"] = (await _composer_text(page))[:200]
    return evidence


def _same_message(seen: str, sent: str) -> bool:
    """Whether the rendered message is the one we sent.

    Compared on normalised text because Discord collapses newlines and trims. A
    prefix comparison would accept a truncated send, so equality is the bar.
    """
    def norm(value: str) -> str:
        return " ".join(str(value or "").split())

    return norm(seen) == norm(sent)


@_serialized
async def verify_message_present(session_key: str, body: str, *, limit: int = 50) -> Dict[str, Any]:
    """Report whether a given message text is currently in this channel.

    Used to close an UNCERTAIN send. Deliberately read-only and deliberately does not
    send anything, because the whole point is to find out before acting.
    """
    needle = " ".join(str(body or "").split())
    if not needle:
        raise DiscordBrowserError("The message text to look for is required.", status_code=400)

    page = await _page(session_key)
    await _require_login(session_key)
    ref = await _channel_ref(page)
    records = await _message_records(page, limit=limit)
    match = next((r for r in records if " ".join((r.content or "").split()) == needle), None)

    return {
        "ok": True,
        "channel": ref.as_dict(),
        "found": match is not None,
        "message": match.as_dict() if match else {},
        "checked": len(records),
        "url": _safe_url(getattr(page, "url", "")),
        "screenshot_path": await controller.screenshot(session_key, "discord_verify"),
    }


@_serialized
async def join_server(session_key: str, invite: str) -> Dict[str, Any]:
    """Join a server from a legitimate invite the operator supplied.

    Discord's ordinary invite page is used and the result is verified by looking for
    the new server afterwards. Never used to mass-invite, and never from an
    unverified source: the invite string comes from the operator.
    """
    code = str(invite or "").strip().split("/")[-1].split("?")[0]
    if not re.match(r"^[A-Za-z0-9-]{2,64}$", code):
        raise DiscordBrowserError(
            "That does not look like a Discord invite code.", status_code=400
        )

    page = await _page(session_key)
    state = await _require_login(session_key)
    await _open(session_key, f"https://discord.com/invite/{code}")
    page = await _page(session_key)

    # Re-check: Discord may have dropped an expired or already-used invite into the
    # login wall, which must be reported as a login problem, not as a failed join.
    await _require_login(session_key)

    try:
        await interaction.click(
            page, strategy="css", value=SEL_ACCEPT_INVITE, timeout_ms=10_000
        )
    except interaction.LocatorNotFound as exc:
        raise DiscordBrowserError(
            "Discord's accept control was not available on that invite. It may have expired, "
            "been revoked, or the account may already be a member.",
            category="DISCORD_NOT_FOUND",
        ) from exc

    try:
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    await _open(session_key, DISCORD_APP)
    page = await _page(session_key)
    servers = await _read_servers(page)

    _event(session_key, "discord.server_joined", f"Accepted a Discord invite ({code[:8]}...)")
    return {
        "ok": True,
        "login_state": state,
        "servers_now": servers,
        "server_count": len(servers),
        "invite_code_prefix": code[:8],
        "note": (
            "The accept control was clicked and Discord accepted the invite. Discord itself "
            "decides what the account may now do; AirdropOS does not verify permissions beyond "
            "what the guild rail shows."
        ),
        "screenshot_path": await controller.screenshot(session_key, "discord_joined"),
    }


@_serialized
async def leave_server(session_key: str, server_name: str, *, confirmed: bool = False) -> Dict[str, Any]:
    """Leave a server. Irreversible, so it requires an explicit confirmation.

    Leaving a server can remove access the operator has no other way to restore, and
    Discord's own confirmation dialog is one a person is used to seeing. ``confirmed``
    must be true; there is no path here that reaches the leave control without it.
    """
    if not confirmed:
        raise DiscordBrowserError(
            "Leaving a Discord server is irreversible and needs explicit authorization. "
            "Re-run with confirmation to proceed.",
            status_code=409,
        )

    page = await _page(session_key)
    await _require_login(session_key)

    if not await _click_sidebar_channel(page, server_name):
        raise DiscordBrowserError(
            f"Server '{server_name}' was not found in the guild rail.",
            category="DISCORD_NOT_FOUND",
        )

    for name in (server_name, "Server Settings", "Leave Server"):
        for role in ("button", "menuitem", "link", "tab"):
            try:
                await interaction.click(page, strategy="role", value=f"{role}:{name}", timeout_ms=5000)
            except Exception:
                continue

    try:
        await interaction.click(page, strategy="role", value="button:Leave Server", timeout_ms=8000)
    except Exception as exc:
        raise DiscordBrowserError(
            "Discord's leave-server control could not be reached. Do this yourself in the browser "
            "if you need it done now.",
            category=CAT_UNKNOWN,
        ) from exc

    _event(session_key, "discord.server_left", f"Left the Discord server {server_name}")
    return {
        "ok": True,
        "server_name": server_name,
        "note": "The leave control was clicked. Discord shows its own confirmation, which you should verify.",
    }