"""Registry of the Telegram user accounts the agent can act through.

Each account has its own Telethon session file plus a human *label* chosen at
login time (e.g. "main", "farm-2"). The registry is a small JSON file next to
the sessions. It stores only profile display data (name, username, numeric id):
never the phone number, API hash, login code, 2FA password or session contents.

Backwards compatible: a legacy single session at ``TELEGRAM_SESSION_PATH`` shows
up as the account labelled ``default`` until it is re-registered.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.core.config import settings

ROOT = Path(__file__).resolve().parents[3]
LEGACY_LABEL = "default"
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


def resolve_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _base_dir() -> Path:
    return resolve_path(settings.TELEGRAM_SESSION_PATH).parent


def registry_file() -> Path:
    return _base_dir() / "accounts.json"


def accounts_dir() -> Path:
    return _base_dir() / "accounts"


def valid_label(label: str) -> bool:
    return bool(_LABEL_RE.match(label or ""))


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "account"


def session_file_exists(session_path: str) -> bool:
    return Path(f"{resolve_path(session_path)}.session").exists()


def _read() -> List[Dict[str, Any]]:
    try:
        data = json.loads(registry_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("accounts") if isinstance(data, dict) else None
    return [a for a in items if isinstance(a, dict) and a.get("label")] if isinstance(items, list) else []


def _write(accounts: List[Dict[str, Any]]) -> None:
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"accounts": accounts}, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_accounts() -> List[Dict[str, Any]]:
    """All accounts, default first. Includes the legacy single session if nothing is registered."""
    accounts = _read()
    if not accounts and session_file_exists(settings.TELEGRAM_SESSION_PATH):
        accounts = [{"label": LEGACY_LABEL, "session_path": settings.TELEGRAM_SESSION_PATH,
                     "is_default": True, "legacy": True}]
    out = []
    for a in accounts:
        item = dict(a)
        item["session_ready"] = session_file_exists(item["session_path"])
        name = " ".join(p for p in (item.get("first_name"), item.get("last_name")) if p)
        item["profile_name"] = name or (f"@{item['username']}" if item.get("username") else None)
        out.append(item)
    out.sort(key=lambda a: (not a.get("is_default"), a["label"].lower()))
    return out


def get_account(label: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The account with this label (case-insensitive), or the default when label is empty."""
    accounts = list_accounts()
    if not label:
        return accounts[0] if accounts else None
    wanted = label.strip().lower()
    return next((a for a in accounts if a["label"].lower() == wanted), None)


def add_account(label: str, me: Any, session_path: str, make_default: bool = False) -> Dict[str, Any]:
    """Create or update the account with this label from a Telethon ``me`` user object."""
    if not valid_label(label):
        raise ValueError("Label must be 1-40 characters: letters, digits, space, _ . -")
    accounts = _read()
    existing = next((a for a in accounts if a["label"].lower() == label.lower()), None)
    entry = {
        "label": label,
        "session_path": session_path,
        "user_id": getattr(me, "id", None),
        "first_name": getattr(me, "first_name", None),
        "last_name": getattr(me, "last_name", None),
        "username": getattr(me, "username", None),
        "added_at": (existing or {}).get("added_at") or datetime.now(timezone.utc).isoformat(),
        "is_default": bool((existing or {}).get("is_default")) or not accounts or make_default,
    }
    accounts = [a for a in accounts if a is not existing]
    if entry["is_default"]:
        for a in accounts:
            a["is_default"] = False
    accounts.append(entry)
    _write(accounts)
    return entry


def set_default(label: str) -> bool:
    accounts = _read()
    hit = next((a for a in accounts if a["label"].lower() == label.lower()), None)
    if hit is None:
        return False
    for a in accounts:
        a["is_default"] = a is hit
    _write(accounts)
    return True


def remove_account(label: str) -> bool:
    """Forget an account in the registry. The session file is left for the operator to delete."""
    accounts = _read()
    kept = [a for a in accounts if a["label"].lower() != label.lower()]
    if len(kept) == len(accounts):
        return False
    if kept and not any(a.get("is_default") for a in kept):
        kept[0]["is_default"] = True
    _write(kept)
    return True
