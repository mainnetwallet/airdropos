"""Browser mode for the X.com integration.

This is an ADAPTER over the existing browser controller, not a second browser.
Every call goes through ``backend.app.browser.controller`` and
``backend.app.browser.interaction``, so X mode inherits the project's existing
rules for profile selection, the interaction lock, session gating, route
blocking, screenshots and evidence.

What it will not do
-------------------
* Bypass a CAPTCHA, a login challenge or MFA. When one appears the operation
  returns ``needs_user_action`` and the operator takes over in Live Broadcast.
* Use the browser to work around an X API rate limit or a permission denial.
  Those refusals are respected, not routed around.
* Report a click as a completed action. Every write re-reads the page afterwards
  and the caller gets what was actually observed.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from backend.app.browser import interaction
from backend.app.browser.controller import _safe_error, controller
from backend.app.browser.interaction import LocatorNotFound
from backend.app.live import hub

logger = logging.getLogger(__name__)

X_ORIGIN = "https://x.com"
X_HOME = f"{X_ORIGIN}/home"
X_COMPOSE = f"{X_ORIGIN}/compose/post"

# Selectors X currently ships. They are treated as best-effort hints and are
# re-resolved on every call, never cached: X changes its DOM regularly and a
# stale selector that silently clicks the wrong thing is worse than a failure.
SEL_COMPOSER = 'div[data-testid="tweetTextarea_0"]'
SEL_COMPOSE_BUTTON = 'div[data-testid="tweetButtonInline"], button[data-testid="tweetButtonInline"]'
SEL_ACCOUNT_SWITCHER = 'div[data-testid="SideNav_AccountSwitcher_Button"]'
SEL_LOGIN_BUTTON = 'a[data-testid="AppTabBar_Login_Link"], a[href="/login"]'

# A status permalink: /<handle>/status/<id>
PERMALINK_RE = re.compile(r"/(\d{5,25})$")


class BrowserGate(Exception):
    """A human must act: login, CAPTCHA, MFA or manual control."""


class BrowserXError(Exception):
    """The browser path could not complete the operation."""


def _gate_reason(session_key: str) -> str:
    """Refuse to act while an operator paused the session or took control."""
    return hub.browser_sessions.gate(session_key)


def _interaction_lock(session_key: str):
    """The controller's per-session INTERACTION lock.

    Every browser tool in this project wraps its call in this lock
    (``browser_tools._serialized``) because ``get_page`` only holds the session's
    LIFECYCLE lock while it fetches the page: the click and the keystrokes that
    follow run unprotected, and two tasks sharing a profile interleave on one
    page. That matters more here than anywhere else - ``create_post`` types into
    the composer and then clicks Post, so an unprotected interleaving would
    publish the OTHER task's text to the operator's public account.

    This adapter therefore takes the same lock for its whole duration, exactly as
    the browser tools do. Lifecycle callers are unaffected: nothing in this module
    opens or closes a session.
    """
    return controller.interaction_lock(session_key)


def _mark_waiting(session_key: str, reason: str) -> None:
    hub.browser_sessions.mark_waiting(session_key, reason)


def _event(session_key: str, type_: str, message: str, status: str = "ok") -> None:
    hub.browser_sessions.touch(session_key)
    hub.events.emit(
        integration="browser", session_id=f"browser:{session_key}", type=type_, message=message, status=status
    )


def _is_x_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")


# ----------------------------------------------------------------------
# Session and login state
# ----------------------------------------------------------------------
async def _page(session_key: str, *, gate: bool = True):
    if gate:
        reason = _gate_reason(session_key)
        if reason:
            raise BrowserGate(reason)
    page = await controller.get_page(session_key)
    if page is None:
        raise BrowserXError(
            "No browser is open for this profile. Start the profile, or let the agent open it, "
            "then retry."
        )
    return page


def _serialized(fn):
    """Hold the session's INTERACTION lock for the whole call.

    Applied to the PUBLIC entry points only. The helpers they call
    (``_open``, ``login_state``, ``_like_state`` …) deliberately take no lock,
    because the controller's lock is not re-entrant: a nested acquisition would
    deadlock. Keeping the lock at the outermost boundary is exactly the shape
    ``browser_tools._serialized`` uses.
    """

    @functools.wraps(fn)
    async def wrapper(session_key: str, *args, **kwargs):
        async with _interaction_lock(session_key):
            return await fn(session_key, *args, **kwargs)

    return wrapper


async def _open(session_key: str, url: str, *, gate: bool = True):
    """Navigate, refusing a destination that is not x.com, and gate on humans."""
    if not _is_x_url(url):
        raise BrowserXError(f"Refusing to navigate the X integration to a non-X URL: {url}")
    page = await _page(session_key, gate=gate)
    before = await controller.inspect(page)
    if getattr(before, "requires_human", False):
        raise BrowserGate(getattr(before, "human_reason", None) or "Human interaction required.")
    result = await controller.navigate(session_key, url)
    if not result.get("ok"):
        raise BrowserXError(str(result.get("detail") or f"Could not open {url}"))
    observation = result.get("observation")
    if observation is not None and getattr(observation, "requires_human", False):
        raise BrowserGate(getattr(observation, "human_reason", None) or "Human interaction required.")
    return result, observation


async def _count(page, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


async def login_state(session_key: str) -> Dict[str, Any]:
    """Whether this profile is signed in to X.

    Reported as a real observation with the indicators that produced it. An
    unreadable page yields ``UNKNOWN``, never "logged out": signing the operator
    out of their own account is a worse error than saying nothing.
    """
    page = await _page(session_key, gate=False)
    if page is None:
        return {"state": "NO_BROWSER", "detail": "No browser is open for this profile."}
    try:
        if not _is_x_url(page.url):
            return {"state": "UNKNOWN", "detail": f"The browser is on {urlparse(page.url).hostname or page.url}."}
        if await _count(page, SEL_ACCOUNT_SWITCHER) > 0:
            return {"state": "LOGGED_IN", "indicator": "account_switcher"}
        if await _count(page, SEL_LOGIN_BUTTON) > 0:
            return {"state": "LOGGED_OUT", "indicator": "login_link"}
        if await _count(page, SEL_COMPOSER) > 0:
            return {"state": "LOGGED_IN", "indicator": "composer"}
    except Exception as exc:
        return {"state": "UNKNOWN", "detail": f"{type(exc).__name__} while reading the page."}
    return {
        "state": "UNKNOWN",
        "detail": "No login or account control was found on the page. Sign in manually to be sure.",
    }


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
@_serialized
async def read_profile(session_key: str, handle: str) -> Dict[str, Any]:
    """Open https://x.com/<handle> and report what the page actually shows."""
    target = f"{X_ORIGIN}/{str(handle).lstrip('@')}"
    result, observation = await _open(session_key, target)
    page = await _page(session_key)
    state = await login_state(session_key)
    if state["state"] == "LOGGED_OUT":
        raise BrowserGate("This browser profile is not signed in to X. Sign in, then retry.")

    summary = await _visible(page, limit=4000)
    return {
        "ok": True,
        "url": page.url,
        "handle": str(handle).lstrip("@"),
        "page_title": getattr(observation, "title", "") or "",
        "page_text": summary,
        "login_state": state["state"],
        "observation": getattr(observation, "__dict__", {}) if observation is not None else {},
        "screenshot_path": await controller.screenshot(session_key, "x_profile"),
    }


@_serialized
async def read_post(session_key: str, post_id: str) -> Dict[str, Any]:
    """Open a single post by id through X's own /i/status/ route."""
    result, observation = await _open(session_key, f"{X_ORIGIN}/i/status/{post_id}")
    page = await _page(session_key)
    return {
        "ok": True,
        "post_id": str(post_id),
        "url": page.url,
        "page_title": getattr(observation, "title", "") or "",
        "page_text": await _visible(page, limit=4000),
        "observation": getattr(observation, "__dict__", {}) if observation is not None else {},
        "screenshot_path": await controller.screenshot(session_key, "x_post"),
    }


async def _visible(page, *, limit: int) -> str:
    try:
        return await interaction.visible_text(page, limit=limit)
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------
@_serialized
async def create_post(
    session_key: str, text: str, *, reply_to: Optional[str] = None
) -> Dict[str, Any]:
    """Compose and submit a post (or a reply) in the real x.com composer.

    Returns what the page showed afterwards. When the composer redirects to the
    new post, its id is extracted and returned as ``post_id``; that id is the
    only thing that lets the service de-duplicate and verify later.
    """
    result, observation = await _open(session_key, X_COMPOSE)
    page = await _page(session_key)
    state = await login_state(session_key)
    if state["state"] == "LOGGED_OUT":
        raise BrowserGate("This browser profile is not signed in to X, so it cannot post.")

    composer = page.locator(SEL_COMPOSER).first
    try:
        await composer.wait_for(state="visible", timeout=15_000)
    except Exception as exc:
        raise BrowserXError(
            "The X composer did not appear. The account may be signed out, rate limited, or "
            "suspended. Inspect the page in Live Broadcast."
        ) from exc

    await composer.click(timeout=10_000)
    await interaction.type_text(page, text=str(text), delay_ms=15, timeout_ms=10_000)

    before = await _composer_state(page)
    submit = page.locator(SEL_COMPOSE_BUTTON).first
    try:
        await submit.wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        raise BrowserXError(
            "The Post button did not become available. The text may exceed X's length limit, "
            "or the composer is in a state that needs the operator."
        ) from exc

    # Re-check the gate immediately before the irreversible click. Typing takes
    # real time, and an operator who stopped the session or took manual control
    # while it was happening must not have their account post anyway. This is the
    # same re-check `browser_click` performs.
    stopped = _gate_reason(session_key)
    if stopped:
        raise BrowserGate(stopped)
    await submit.click(timeout=10_000)

    after = await _composer_state(page)
    post_id = await _detect_created_post(page)
    logged_out = (await login_state(session_key))["state"] == "LOGGED_OUT"

    verified = bool(post_id) or bool(after.get("composer_emptied")) or bool(after.get("confirmation"))
    if not verified:
        # The click happened. That is NOT proof the post was created.
        raise BrowserXError(
            "The Post button was clicked but the page did not confirm that anything was "
            "published. Check the account's timeline in Live Broadcast before retrying: "
            "retrying could produce a duplicate post."
        )

    _event(session_key, "x.post_published", f"Published a post through x.com ({post_id or 'id unknown'})")
    return {
        "ok": True,
        "method": "browser",
        "post_id": post_id or "",
        "url": page.url,
        "composer_state_before": before,
        "composer_state_after": after,
        "logged_out_after": logged_out,
        "screenshot_path": await controller.screenshot(session_key, "x_posted"),
    }


async def _composer_state(page) -> Dict[str, Any]:
    """Whatever is observable about the composer after an action."""
    try:
        composer = page.locator(SEL_COMPOSER).first
        text = (await composer.inner_text(timeout=5_000)) or ""
    except Exception:
        text = ""
    body = ""
    try:
        body = await interaction.visible_text(page, limit=1500)
    except Exception:
        pass
    confirmation = any(
        marker in body
        for marker in ("Your post was sent", "Post sent", "Your reply was sent", "Message sent")
    )
    return {
        "composer_text_length": len(text.strip()),
        "composer_emptied": not text.strip(),
        "confirmation": confirmation,
    }


async def _detect_created_post(page) -> str:
    """The id of the post we just made, if the page exposes one."""
    try:
        url = page.url
    except Exception:
        return ""
    match = PERMALINK_RE.search(str(url or ""))
    if match and "/status/" in str(url or ""):
        return match.group(1)
    return ""


@_serialized
async def like_post(session_key: str, post_id: str) -> Dict[str, Any]:
    """Like a post in the UI and verify by the control's own state change."""
    await _open(session_key, f"{X_ORIGIN}/i/status/{post_id}")
    page = await _page(session_key)

    before = await _like_state(page)
    if before["liked"]:
        # Already liked: nothing to do, and definitely nothing to click.
        return {
            "ok": True, "method": "browser", "already_applied": True,
            "post_id": str(post_id), "like_state": before,
            "note": "The page already showed this post as liked; nothing was clicked.",
        }

    clicked = await _click_by_name(page, ("Like",), test_ids=("like",), timeout_ms=15_000)
    if not clicked:
        raise BrowserXError(
            "No Like control was found on the post page. The post may be deleted, or this "
            "account may be signed out."
        )
    after = await _like_state(page)
    if not after["liked"]:
        raise BrowserXError(
            "The Like control was clicked but the page still does not show the post as liked. "
            "Verify in Live Broadcast before trying again."
        )
    _event(session_key, "x.liked", f"Liked post {post_id} through x.com")
    return {
        "ok": True, "method": "browser", "post_id": str(post_id),
        "like_state_before": before, "like_state_after": after,
        "screenshot_path": await controller.screenshot(session_key, "x_liked"),
    }


@_serialized
async def unlike_post(session_key: str, post_id: str) -> Dict[str, Any]:
    await _open(session_key, f"{X_ORIGIN}/i/status/{post_id}")
    page = await _page(session_key)
    before = await _like_state(page)
    if not before["liked"]:
        return {
            "ok": True, "method": "browser", "already_applied": True,
            "post_id": str(post_id), "note": "The post was not liked; nothing was clicked.",
        }
    clicked = await _click_by_name(page, ("Unlike",), test_ids=("unlike",), timeout_ms=15_000)
    if not clicked:
        raise BrowserXError("No Unlike control was found on the post page.")
    after = await _like_state(page)
    if after["liked"]:
        raise BrowserXError(
            "The Unlike control was clicked but the page still shows the post as liked."
        )
    _event(session_key, "x.unliked", f"Removed a like from post {post_id} through x.com")
    return {
        "ok": True, "method": "browser", "post_id": str(post_id),
        "like_state_before": before, "like_state_after": after,
    }


async def _like_state(page) -> Dict[str, Any]:
    """Whether the page reports this post as liked.

    X marks a liked post by swapping the control's accessible name from "Like"
    to "Unlike"; that is what is read here, so the answer is the page's own.
    """
    try:
        liked = await _count(page, '[data-testid="unlike"]') > 0
        like_button = await _count(page, '[data-testid="like"]') > 0
    except Exception:
        liked, like_button = False, False
    return {
        "liked": liked,
        "like_control_present": liked or like_button,
        "indicator": "unlike_control" if liked else ("like_control" if like_button else "none"),
    }


@_serialized
async def follow_user(session_key: str, handle: str) -> Dict[str, Any]:
    """Follow an account in the UI and verify by the button's own label."""
    clean = str(handle or "").lstrip("@").strip()
    await _open(session_key, f"{X_ORIGIN}/{clean}")
    page = await _page(session_key)

    before = await _follow_state(page)
    if before["following"]:
        return {
            "ok": True, "method": "browser", "already_applied": True,
            "username": clean, "follow_state": before,
            "note": "This profile was already followed; nothing was clicked.",
        }
    clicked = await _click_by_name(page, ("Follow",), test_ids=("follow",), timeout_ms=15_000)
    if not clicked:
        raise BrowserXError(f"No Follow control was found on the profile page for @{clean}.")
    after = await _follow_state(page)
    if not after["following"]:
        raise BrowserXError(
            "The Follow control was clicked but the page still shows 'Follow'. Verify in Live "
            "Broadcast before trying again."
        )
    _event(session_key, "x.followed", f"Followed @{clean} through x.com")
    return {
        "ok": True, "method": "browser", "username": clean,
        "follow_state_before": before, "follow_state_after": after,
        "screenshot_path": await controller.screenshot(session_key, "x_followed"),
    }


@_serialized
async def unfollow_user(session_key: str, handle: str) -> Dict[str, Any]:
    clean = str(handle or "").lstrip("@").strip()
    await _open(session_key, f"{X_ORIGIN}/{clean}")
    page = await _page(session_key)
    before = await _follow_state(page)
    if not before["following"]:
        return {
            "ok": True, "method": "browser", "already_applied": True, "username": clean,
            "note": "This profile was not followed; nothing was clicked.",
        }
    clicked = await _click_by_name(page, ("Following",), test_ids=("unfollow",), timeout_ms=15_000)
    if not clicked:
        raise BrowserXError("No Following control was found; open the profile and use the menu.")
    after = await _follow_state(page)
    if after["following"]:
        raise BrowserXError("The Following control was clicked but the profile still shows it.")
    return {
        "ok": True, "method": "browser", "username": clean,
        "follow_state_before": before, "follow_state_after": after,
    }


async def _follow_state(page) -> Dict[str, Any]:
    try:
        following = await _count(page, '[data-testid="userFollowing"]') > 0
        follow = await _count(page, '[data-testid="follow"]') > 0
        unfollow = await _count(page, '[data-testid="unfollow"]') > 0
    except Exception:
        following = follow = unfollow = False
    return {
        "following": following or unfollow,
        "follow_control_present": following or follow or unfollow,
        "indicator": "userFollowing" if following else ("unfollow" if unfollow else ("follow" if follow else "none")),
    }


async def _click_by_name(
    page, names: Tuple[str, ...], *, test_ids: Tuple[str, ...] = (), timeout_ms: int = 15000
) -> bool:
    """Click the first visible control matching one of X's own test ids, then by
    accessible name.

    ``test_id`` is tried first because X's ``data-testid`` attributes survive
    copy changes to the visible label, and the shared resolver then handles
    frames, overlays and dialogs exactly as every other browser tool here does.
    """
    for test_id in test_ids:
        try:
            await interaction.click(page, strategy="test_id", value=test_id, timeout_ms=timeout_ms)
            return True
        except LocatorNotFound:
            continue
        except Exception as exc:
            logger.info("x browser: clicking test id %r failed (%s)", test_id, type(exc).__name__)
            continue
    for name in names:
        # `role` takes "role:name"; a bare name would look for a ROLE called
        # "Like", which matches nothing.
        try:
            await interaction.click(page, strategy="role", value=f"button:{name}", timeout_ms=timeout_ms)
            return True
        except LocatorNotFound:
            continue
        except Exception as exc:
            logger.info("x browser: clicking %r failed (%s)", name, type(exc).__name__)
            continue
    return False


@_serialized
async def like_state_for(session_key: str, post_id: str) -> Dict[str, Any]:
    """Open a post and report ONLY the like state. Used to verify a write."""
    await _open(session_key, f"{X_ORIGIN}/i/status/{post_id}")
    page = await _page(session_key)
    return {**(await _like_state(page)), "post_id": str(post_id), "url": page.url}


@_serialized
async def follow_state_for(session_key: str, handle: str) -> Dict[str, Any]:
    """Open a profile and report ONLY the follow state. Used to verify a write."""
    clean = str(handle or "").lstrip("@").strip()
    await _open(session_key, f"{X_ORIGIN}/{clean}")
    page = await _page(session_key)
    return {**(await _follow_state(page)), "username": clean, "url": page.url}


async def session_status(session_key: str) -> Dict[str, Any]:
    """Live state of the X browser session, for the overview page."""
    try:
        status = await controller.profile_status(session_key)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {_safe_error(exc)}"}
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
            out["login"] = (await login_state(session_key))["state"]
        except BrowserXError:
            out["login"] = "NO_BROWSER"
    return out