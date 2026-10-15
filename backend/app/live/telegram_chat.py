"""Operator chat for a live Telegram session: a real-Telegram-like view of the account.

The dashboard shows the chat list and the messages of the logged-in Telethon account, and
lets the operator send a message or press an inline button. Everything goes through the
SAME session object (and its lock) the agent uses, so the operator and the agent never
talk to Telegram at the same time, and the account's pause/stop state is respected.

Safety is unchanged from the agent tools:
  * outgoing text that looks like a private key or seed phrase is refused;
  * only plain and callback buttons are pressed. Links are returned so the browser can open
    them on the operator's explicit click; payment, phone/location sharing, login and
    Mini App buttons are refused with the reason;
  * no session material, phone number or access hash ever leaves this module.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

from backend.app.live.events import redact_text
from backend.app.live.models import Conflict, NotSupported

MAX_TEXT = 4096
_PEER_RE = re.compile(r"^-?\d{1,20}$")

_MEDIA_LABELS = (
    ("photo", "Photo"),
    ("video", "Video"),
    ("voice", "Voice message"),
    ("audio", "Audio"),
    ("sticker", "Sticker"),
    ("gif", "GIF"),
    ("document", "File"),
    ("poll", "Poll"),
    ("geo", "Location"),
    ("contact", "Contact"),
    ("webpage", "Link preview"),
)


def _tools():
    # Imported lazily: the Telethon tool module pulls in the whole tool registry.
    from backend.app.tools import telegram_user_tools as t

    return t


def _peer(value: Any) -> int:
    text = str(value or "").strip()
    if not _PEER_RE.match(text):
        raise NotSupported("Invalid chat reference.")
    return int(text)


def _media_label(msg: Any) -> Optional[str]:
    if getattr(msg, "media", None) is None:
        return None
    for attr, label in _MEDIA_LABELS:
        if getattr(msg, attr, None):
            return label
    return "Attachment"


def _sender_name(msg: Any) -> Optional[str]:
    sender = getattr(msg, "sender", None)
    if sender is None:
        return None
    name = getattr(sender, "title", None) or " ".join(
        p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p
    )
    return str(name)[:80] if name else None


def _describe(msg: Any) -> Dict[str, Any]:
    t = _tools()
    rows: List[List[Dict[str, Any]]] = []
    for r, row in enumerate(getattr(msg, "buttons", None) or []):
        cells = []
        for c, button in enumerate(row):
            kind = t._button_kind(button)
            url = getattr(button, "url", None)
            cells.append(
                {
                    "row": r,
                    "col": c,
                    "text": str(getattr(button, "text", ""))[:80],
                    "kind": kind,
                    "clickable": kind in t.CLICKABLE_BUTTON_KINDS,
                    # Only plain web links are ever handed to the browser.
                    "url": url if kind == "url" and isinstance(url, str) and url.lower().startswith(("https://", "http://")) else None,
                }
            )
        rows.append(cells)
    return {
        "id": msg.id,
        "from_me": bool(getattr(msg, "out", False)),
        "date": msg.date.isoformat() if getattr(msg, "date", None) else None,
        "sender": _sender_name(msg),
        "text": (getattr(msg, "message", None) or "")[:MAX_TEXT],
        "media": _media_label(msg),
        "reply_to": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        "edited": bool(getattr(msg, "edit_date", None)),
        "buttons": rows,
    }


def _dialog_card(dialog: Any) -> Optional[Dict[str, Any]]:
    entity = getattr(dialog, "entity", None)
    if entity is None:
        return None
    t = _tools()
    ref = t._describe_entity(entity)
    last = getattr(dialog, "message", None)
    snippet = (getattr(last, "message", None) or "") if last is not None else ""
    if not snippet and last is not None:
        snippet = _media_label(last) or ""
    return {
        "peer": str(dialog.id),  # marked id: unambiguous for users, groups and channels
        "title": ref.get("title") or ref.get("username") or "Unknown",
        "username": ref.get("username"),
        "kind": "bot" if getattr(entity, "bot", False) else ref.get("entity_kind"),
        "unread": int(getattr(dialog, "unread_count", 0) or 0),
        "last_text": snippet[:120],
        "last_from_me": bool(getattr(last, "out", False)) if last is not None else False,
        "last_date": last.date.isoformat() if last is not None and getattr(last, "date", None) else None,
        "pinned": bool(getattr(dialog, "pinned", False)),
    }


def _translate(exc: BaseException) -> Exception:
    """Telethon/session failures -> operator-safe Conflict (no phone, no raw text)."""
    t = _tools()
    name = type(exc).__name__
    if isinstance(exc, t._NotLoggedIn):
        return Conflict(
            "Telegram is not logged in. Run `python scripts/telegram_login.py` once in a terminal.", "not_logged_in"
        )
    if isinstance(exc, ImportError):
        return NotSupported("Telethon is not installed (pip install telethon).")
    if name == "FloodWaitError":
        return Conflict(f"Telegram asked to wait {int(getattr(exc, 'seconds', 0) or 0)}s (flood wait).", "rate_limited")
    if name in ("ChatWriteForbiddenError", "UserBannedInChannelError", "ChatAdminRequiredError"):
        return Conflict("Telegram does not allow you to write in this chat.", "forbidden")
    if name in ("MessageIdInvalidError", "BotResponseTimeoutError"):
        return Conflict("The button did not answer (the message may be old).", "button_failed")
    if name in ("PeerIdInvalidError", "ValueError"):
        return Conflict("This chat could not be found on the account.", "not_found")
    return Conflict(f"Telegram error ({name}).", "telegram_error")


async def _with_client(tg: Any, fn):
    """Run ``fn(client)`` under the session's lock. Never bypasses stop; pause only pauses the agent."""
    if tg.stopped:
        raise Conflict("The Telegram session is stopped. Resume it first.", "stopped")
    async with tg._lock:
        if tg.stopped:  # stopped while this call waited for the lock
            raise Conflict("The Telegram session is stopped. Resume it first.", "stopped")
        try:
            client = await tg._connect()
            return await fn(client)
        except (Conflict, NotSupported):
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a safe message
            raise _translate(exc) from None


async def run(tg: Any, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    t = _tools()

    if op == "dialogs":
        limit = max(1, min(int(payload.get("limit") or 40), 100))
        search = str(payload.get("search") or "").strip().lower()[:60]

        async def dialogs(client: Any) -> Dict[str, Any]:
            out: List[Dict[str, Any]] = []
            async for dialog in client.iter_dialogs(limit=limit * 3 if search else limit):
                card = _dialog_card(dialog)
                if card is None:
                    continue
                if search and search not in f"{card['title']} {card['username'] or ''}".lower():
                    continue
                out.append(card)
                if len(out) >= limit:
                    break
            return {"dialogs": out}

        return await _with_client(tg, dialogs)

    if op == "history":
        peer = _peer(payload.get("peer"))
        limit = max(1, min(int(payload.get("limit") or 40), 100))

        async def history(client: Any) -> Dict[str, Any]:
            entity = await client.get_entity(peer)
            messages = await client.get_messages(entity, limit=limit)
            title = t._describe_entity(entity)
            return {
                "peer": str(peer),
                "title": title.get("title") or title.get("username") or "Chat",
                "username": title.get("username"),
                "is_bot": bool(getattr(entity, "bot", False)),
                "messages": [_describe(m) for m in reversed(list(messages))],
            }

        return await _with_client(tg, history)

    if op == "send":
        peer = _peer(payload.get("peer"))
        text = str(payload.get("text") or "")
        if not text.strip():
            raise NotSupported("Type a message first.")
        if len(text) > MAX_TEXT:
            raise NotSupported(f"Messages are limited to {MAX_TEXT} characters.")
        if t.looks_like_secret(text):
            raise Conflict(
                "Refused: the text looks like a private key or seed phrase. Only public addresses may be sent.",
                "secret_blocked",
            )
        reply_to = payload.get("reply_to")

        async def send(client: Any) -> Dict[str, Any]:
            entity = await client.get_entity(peer)
            sent = await client.send_message(entity, text, reply_to=int(reply_to) if reply_to else None)
            tg._emit("telegram.operator", f"Operator sent a message ({len(text)} chars)", "ok")
            # Give a bot a moment to answer so the operator sees the reply right away.
            await asyncio.sleep(1.2)
            return {"sent_id": sent.id}

        return await _with_client(tg, send)

    if op == "click":
        peer = _peer(payload.get("peer"))
        message_id = int(payload.get("message_id") or 0)
        row, col = int(payload.get("row", -1)), int(payload.get("col", -1))
        if message_id <= 0 or row < 0 or col < 0:
            raise NotSupported("Invalid button reference.")

        async def click(client: Any) -> Dict[str, Any]:
            entity = await client.get_entity(peer)
            msg = await client.get_messages(entity, ids=message_id)
            if msg is None:
                raise Conflict("Message not found.", "not_found")
            try:
                button = msg.buttons[row][col]
            except (TypeError, IndexError):
                raise Conflict("That button is no longer on the message.", "not_found")
            kind = t._button_kind(button)
            if kind not in t.CLICKABLE_BUTTON_KINDS:
                reason = t.REFUSED_BUTTON_REASONS.get(kind, "This button type cannot be pressed from here.")
                raise Conflict(f"Not pressed. {reason}", "button_refused")
            answer = await msg.click(row, col)
            alert = getattr(answer, "message", None) if answer is not None else None
            tg._emit("telegram.operator", f"Operator pressed button '{redact_text(str(button.text))[:40]}'", "ok")
            await asyncio.sleep(1.2)
            return {"clicked": True, "answer": (str(alert)[:500] if alert else None)}

        return await _with_client(tg, click)

    raise NotSupported(f"Unknown chat operation '{op}'.")
