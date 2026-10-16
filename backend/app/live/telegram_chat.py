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
from typing import Any, Dict, List, Optional, Tuple

from backend.app.live.events import redact_text
from backend.app.live.models import Conflict, NotSupported

MAX_TEXT = 4096
MAX_FILE_BYTES = 15 * 1024 * 1024  # the gateway call has a short timeout; bigger files belong in Telegram
MAX_PREVIEW_BYTES = 6 * 1024 * 1024
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


_IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def _clean_name(name: Optional[str], fallback: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]", "_", str(name or "")).strip(" .")[:120]
    return cleaned or fallback


def _media_info(msg: Any) -> Optional[Dict[str, Any]]:
    """What the dashboard may show for a message's attachment (never a Telegram file reference)."""
    if getattr(msg, "media", None) is None:
        return None
    f = getattr(msg, "file", None)
    mime = getattr(f, "mime_type", None) or ""
    if getattr(msg, "photo", None):
        kind = "photo"
    elif getattr(msg, "sticker", None):
        kind = "sticker"
    elif getattr(msg, "gif", None):
        kind = "gif"
    elif getattr(msg, "video_note", None) or getattr(msg, "video", None):
        kind = "video"
    elif getattr(msg, "voice", None):
        kind = "voice"
    elif getattr(msg, "audio", None):
        kind = "audio"
    elif getattr(msg, "document", None):
        kind = "image" if mime in _IMAGE_MIMES else "file"
    else:
        return None  # poll, location, contact: shown as a label only
    size = getattr(f, "size", None)
    size = int(size) if isinstance(size, int) else None
    return {
        "kind": kind,
        "name": _clean_name(getattr(f, "name", None), f"{kind}{getattr(f, 'ext', '') or ''}"),
        "size": size,
        "mime": mime or None,
        "preview": kind in ("photo", "sticker", "gif", "video", "image"),
        "downloadable": size is None or size <= MAX_FILE_BYTES,
    }


def _pick_photo_size(photo: Any) -> Any:
    """The largest ordinary size up to ~1280px, so a preview stays light."""
    from telethon.tl import types

    sizes = [s for s in (getattr(photo, "sizes", None) or []) if isinstance(s, types.PhotoSize)]
    if not sizes:
        return -1
    fits = [s for s in sizes if max(s.w, s.h) <= 1280]
    return max(fits, key=lambda s: s.w * s.h) if fits else min(sizes, key=lambda s: s.w * s.h)


def _sniff_image(data: Optional[bytes]) -> Optional[str]:
    """Only these formats are ever served inline, decided from the bytes, not from a claim."""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return None


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
                    # A Mini App is opened in a browser session on request; its signed link never leaves the worker.
                    "miniapp": kind == "webapp",
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
        "media_info": _media_info(msg),
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


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
async def _message(client: Any, peer: int, message_id: int) -> Tuple[Any, Any]:
    entity = await client.get_entity(peer)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise Conflict("Message not found.", "not_found")
    return entity, msg


async def media(tg: Any, peer: int, message_id: int, variant: str) -> Tuple[bytes, str, str]:
    """(bytes, mime, filename) of a message's attachment.

    ``preview`` is a small image decided by its bytes (jpeg/png/webp/gif only). ``file`` is the
    original, always served as an opaque download so a hostile file can never run in the dashboard.
    """
    if variant not in ("preview", "file"):
        raise NotSupported("Unknown media variant.")

    async def go(client: Any) -> Tuple[bytes, str, str]:
        _, msg = await _message(client, peer, message_id)
        info = _media_info(msg)
        if info is None:
            raise Conflict("This message has no attachment.", "not_found")
        if variant == "preview":
            if not info["preview"]:
                raise Conflict("No preview for this attachment.", "no_preview")
            if info["kind"] == "photo":
                data = await client.download_media(msg, file=bytes, thumb=_pick_photo_size(msg.photo))
            elif info["mime"] == "image/webp" and (info["size"] or 0) <= 512 * 1024:
                data = await client.download_media(msg, file=bytes)  # a still sticker
            elif info["kind"] == "image" and (info["size"] or MAX_PREVIEW_BYTES) <= MAX_PREVIEW_BYTES:
                data = await client.download_media(msg, file=bytes)
            else:
                data = await client.download_media(msg, file=bytes, thumb=-1)
            mime = _sniff_image(data)
            if mime is None or len(data) > MAX_PREVIEW_BYTES:
                raise Conflict("No preview for this attachment.", "no_preview")
            return data, mime, ""
        if not info["downloadable"]:
            raise Conflict(
                f"This file is larger than {MAX_FILE_BYTES // 1048576} MB. Open it in Telegram instead.", "too_large"
            )
        data = await client.download_media(msg, file=bytes)
        if not data:
            raise Conflict("The file could not be downloaded.", "download_failed")
        if len(data) > MAX_FILE_BYTES:
            raise Conflict(f"This file is larger than {MAX_FILE_BYTES // 1048576} MB.", "too_large")
        return data, "application/octet-stream", info["name"]

    return await _with_client(tg, go)


# ---------------------------------------------------------------------------
# Mini Apps
# ---------------------------------------------------------------------------
def scrub(text: str, url: str) -> str:
    """Remove a Mini App link (it carries Telegram's signed login data) from a message."""
    from backend.app.live.events import safe_url

    out = str(text).replace(url, safe_url(url))
    return re.sub(r"tgWebApp\w*=[^\s&\"')]+", "tgWebApp…", out)


async def miniapp_url(tg: Any, payload: Dict[str, Any]) -> str:
    """Ask Telegram, as the signed-in account, for the launch link of a Mini App button.

    This is what the official Telegram clients do when the button is tapped. The link includes
    Telegram's signed ``initData`` for THIS account, so it is a credential: it is handed straight
    to the browser session by the caller and is never returned to the dashboard, logged or stored.
    Nothing is forged; Telegram issues the data.
    """
    t = _tools()
    peer = _peer(payload.get("peer"))
    message_id = int(payload.get("message_id") or 0)
    row, col = int(payload.get("row", -1)), int(payload.get("col", -1))
    if message_id <= 0 or row < 0 or col < 0:
        raise NotSupported("Invalid button reference.")

    async def go(client: Any) -> str:
        from telethon.tl import functions

        entity, msg = await _message(client, peer, message_id)
        try:
            button = msg.buttons[row][col]
        except (TypeError, IndexError):
            raise Conflict("That button is no longer on the message.", "not_found")
        if t._button_kind(button) != "webapp":
            raise Conflict("This button is not a Mini App.", "not_miniapp")
        raw = getattr(button, "button", None)
        url = (
            getattr(button, "url", None)
            or getattr(getattr(raw, "type", None), "url", None)
            or getattr(raw, "url", None)
        )
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            raise Conflict("The Mini App has no secure (https) address.", "bad_url")
        sender = getattr(msg, "sender_id", None)
        bot = await client.get_input_entity(sender if sender else entity)
        simple = any("simplewebview" in n for n in t._type_names(raw))
        if simple:
            result = await client(functions.messages.RequestSimpleWebViewRequest(bot=bot, platform="weba", url=url))
        else:
            result = await client(functions.messages.RequestWebViewRequest(peer=entity, bot=bot, platform="weba", url=url))
        launch = getattr(result, "url", None)
        if not isinstance(launch, str) or not launch.lower().startswith("https://"):
            raise Conflict("Telegram did not return a launch link for this Mini App.", "no_launch")
        return launch

    return await _with_client(tg, go)
