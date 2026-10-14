"""Telegram Mini App / Web App detection and browser handoff.

Honest scope, stated up front because it is easy to overclaim here:

* IMPLEMENTED: detecting that a button is a Mini App, identifying which launch
  mechanism it uses, and preserving the originating bot/chat/message/task context
  across the handoff. The handoff REUSES the existing browser subsystem.
* IMPLEMENTED: classifying what the browser can then do (inspect, fill, click) through
  the already-registered browser tools, with their existing click-mode and
  human-verification controls untouched.
* NOT IMPLEMENTED, deliberately: fabricating Telegram ``initData`` or a
  ``window.Telegram.WebApp`` object. That data is signed by Telegram for a specific
  WebView and cannot be invented. A Mini App opened in an ordinary Playwright page is
  therefore an UNAUTHENTICATED page, and any Mini App that depends on the authenticated
  WebView session will not work through it.

Because of that, these tools do not claim Mini App automation succeeds. They report the
launch detected, hand the URL to the browser, and -- when the page clearly needs the
authenticated WebView -- raise a structured human-intervention request instead of
pretending. No token, cookie, initData or session value is ever passed to the LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from backend.app.tools.base import BaseTool, SideEffect, ToolResult, ToolResultStatus

logger = logging.getLogger(__name__)

# Signals that a page depends on the authenticated Telegram WebView.
_WEBVIEW_DEPENDENT_MARKERS = (
    "window.telegram", "telegram.webapp", "telegram-web-app.js", "tgwebapp",
    "initdata", "initdatainsafe", "telegram.initdata", "window.cloudstorage",
)

_URL_SCHEME_RE = re.compile(r"^https?://", re.I)


def classify_launch(button: Any) -> Dict[str, Any]:
    """Describe how a button launches a Mini App, WITHOUT opening anything.

    Payment, phone, location and login buttons are reported as unsupported so the caller
    routes them to the operator instead of treating them as ordinary navigation.
    """
    raw = getattr(button, "button", None)
    url = getattr(button, "url", None)
    text = str(getattr(button, "text", "") or "")[:120]
    type_name = type(raw).__name__ if raw is not None else ""

    # Telethon names these KeyboardButtonWebView / KeyboardButtonSimpleWebView.
    if "WebView" in type_name or "WebApp" in type_name:
        target = url or getattr(raw, "url", None)
        return {
            "is_miniapp": True,
            "mechanism": "telegram_webview",
            "url": target,
            "button_text": text,
            "supported": bool(target and _URL_SCHEME_RE.match(str(target))),
            "note": (
                "Telegram Web App button. It can be opened in a browser as an ordinary page, "
                "but that page is NOT the authenticated WebView, so Mini App features that "
                "depend on Telegram session data will not be available."
            ),
        }

    if url and _URL_SCHEME_RE.match(str(url)):
        return {
            "is_miniapp": False,
            "mechanism": "url_button",
            "url": url,
            "button_text": text,
            "supported": False,
            "note": "A plain link button. It is reported, never opened automatically.",
        }

    return {
        "is_miniapp": False,
        "mechanism": "unsupported",
        "url": None,
        "button_text": text,
        "supported": False,
        "note": (
            f"Button type {type_name or 'unknown'} is not a Mini App launch and is not opened "
            "automatically (payment, phone/location share, login or callback)."
        ),
    }


def safe_url(url: Optional[str]) -> str:
    """Strip credentials and query from a URL before it is shown or logged.

    Query strings on Mini App URLs routinely carry tokens.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 80:
        path = path[:77] + "..."
    return f"{parts.scheme}://{host}{path}" if parts.scheme else ""


def looks_webview_dependent(page_text: str, page_html: str = "") -> bool:
    """Whether a page appears to require the authenticated Telegram WebView.

    A heuristic on page CONTENT only. No initData is ever read, constructed or requested.
    """
    haystack = f"{page_text or ''} {page_html or ''}".lower()
    return any(marker in haystack for marker in _WEBVIEW_DEPENDENT_MARKERS)


class TelegramDetectMiniAppTool(BaseTool):
    """Inspect a bot message's buttons and report Mini App launch capability.

    Read-only: it never opens anything. Detection must happen before any decision to
    click, so an unsupported button (payment, login, phone share) is never pressed.
    """

    name = "telegram_detect_miniapp"
    version = "1.0.0"
    description = (
        "Inspect the buttons under a Telegram bot message and report which of them launch a "
        "Mini App (Web App), how it launches, and what the agent can do with it. Read-only."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300},
            "message_id": {"type": "integer", "minimum": 1},
        },
        "required": ["chat", "message_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.tools.telegram_user_tools import (
            _evidence,
            _resolve_entity,
            _session,
        )

        chat = str(params["chat"]).strip()
        known = _session.resolve_ref(chat)
        if known is not None and known.get("chat_id"):
            chat = str(known["chat_id"])
        message_id = int(params["message_id"])

        async def action(client: Any) -> ToolResult:
            try:
                entity = await _resolve_entity(client, chat)
            except ValueError as exc:
                return ToolResult.failure(str(exc), category="INVALID_INPUT")
            msg = await client.get_messages(entity, ids=message_id)
            if msg is None:
                return ToolResult.failure(
                    f"Message {message_id} was not found.", category="NOT_FOUND"
                )
            flat = [b for row in (getattr(msg, "buttons", None) or []) for b in row]
            launches: List[Dict[str, Any]] = [classify_launch(b) for b in flat]
            miniapps = [item for item in launches if item["is_miniapp"]]
            return ToolResult.success(
                data={
                    "chat": chat,
                    "message_id": message_id,
                    "button_count": len(launches),
                    "miniapp_count": len(miniapps),
                    "launches": launches,
                    "miniapps": miniapps,
                    "capability": (
                        "Mini App launch detected. The URL can be opened in the existing browser "
                        "session as an ordinary (unauthenticated) page. Telegram-authenticated "
                        "WebApp features are NOT available this way."
                        if miniapps else "No Mini App launch button on this message."
                    ),
                },
                evidence=_evidence("miniapp_launch_detected", count=len(miniapps)),
            )

        return await _session.run(
            action, interactive=False, label=f"Inspected buttons on message {message_id}"
        )


class TelegramOpenMiniAppTool(BaseTool):
    """Hand a Mini App URL to the EXISTING browser, preserving task context.

    It does not build a competing browser subsystem: navigation goes through the
    already-registered ``browser_navigate`` tool, so every browser security control
    (private-network blocking, click mode, human-verification gates) still applies.

    It reports honestly when the target needs the authenticated WebView.
    """

    name = "telegram_open_miniapp"
    version = "1.0.0"
    description = (
        "Open a Telegram Mini App (Web App) URL in the existing browser session so the agent can "
        "inspect and interact with it, and return the originating Telegram context so the "
        "workflow can return to the bot afterwards. Reports clearly when the page needs the "
        "authenticated Telegram WebView, which an ordinary browser cannot provide."
    )
    required_permissions = ["telegram.read", "browser.navigate"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 60
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "maxLength": 500},
            "origin_bot": {"type": "string", "maxLength": 100,
                           "description": "@bot the workflow must return to"},
            "origin_chat": {"type": "string", "maxLength": 100},
            "origin_message_id": {"type": "integer", "minimum": 0},
        },
        "required": ["url"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.live import hub
        from backend.app.tools.browser_tools import BrowserNavigateTool
        from backend.app.tools.telegram_user_tools import _evidence

        raw_url = str(params["url"]).strip()
        if not _URL_SCHEME_RE.match(raw_url):
            return ToolResult.failure(
                "A Mini App must be opened over http(s).", category="INVALID_INPUT"
            )

        # Context needed to come BACK to the bot after the browser handoff.
        origin = {
            "origin_bot": params.get("origin_bot") or None,
            "origin_chat": params.get("origin_chat") or None,
            "origin_message_id": params.get("origin_message_id") or None,
            "return_tool": "telegram_return_to_bot",
        }

        # Announce the handoff on the existing event bus, with a redacted URL.
        hub.events.emit(
            integration="telegram",
            session_id="telegram:agent",
            type="miniapp.handoff",
            message=f"Handing a Mini App to the browser: {safe_url(raw_url)}",
            status="ok",
        )

        nav = await BrowserNavigateTool().execute({"url": raw_url})
        opened = nav.status is ToolResultStatus.SUCCESS
        data = nav.data or {}
        page_text = str(data.get("visible_text") or "")
        needs_webview = looks_webview_dependent(page_text)

        if not opened:
            return ToolResult(
                status=nav.status,
                error=(
                    "The Mini App URL could not be opened in the browser session: "
                    f"{nav.error or 'unknown browser error'}"
                ),
                error_category=nav.error_category or "MINIAPP_OPEN_FAILED",
                retryable=bool(getattr(nav, "retryable", False)),
                data={**origin, "miniapp_opened": False, "url_safe": safe_url(raw_url)},
            )

        if needs_webview:
            # Honest limitation: do not pretend the authenticated page was reached.
            return ToolResult.needs_user(
                "This Mini App depends on the authenticated Telegram WebView, which an ordinary "
                "browser session cannot provide. The operator must open the Mini App inside "
                "Telegram and complete it there. The task context is preserved so the task can "
                "resume afterwards.",
                data={
                    **origin,
                    "miniapp_opened": True,
                    "authenticated_webview_available": False,
                    "url_safe": safe_url(raw_url),
                    "capability": "requires_user_intervention",
                },
            )

        return ToolResult.success(
            data={
                **origin,
                "miniapp_opened": True,
                # Stated explicitly so no caller can mistake this for the real WebView.
                "authenticated_webview_available": False,
                "url_safe": safe_url(raw_url),
                "page_title": data.get("title"),
                "visible_text_excerpt": page_text[:600],
                "capability": "unauthenticated_page_only",
                "next_tools": [
                    "inspect_page",
                    "browser_find",
                    "browser_click",
                    "browser_fill_field",
                    "browser_submit",
                    "capture_evidence",
                ],
                "note": (
                    "The page opened as an ordinary, UNAUTHENTICATED page. Telegram session "
                    "data is not available to it."
                ),
            },
            evidence=_evidence("miniapp_launch_detected", url_safe=safe_url(raw_url)),
        )


MINIAPP_TOOLS = (TelegramDetectMiniAppTool, TelegramOpenMiniAppTool)
