#!/usr/bin/env python3
"""One-time Telegram login for the AirdropOS agent (run in YOUR terminal).

    python scripts/telegram_login.py

You type the phone number, the login code Telegram sends you, and your 2FA
password (if any) here in the terminal. They are never sent to the AI model, the
chat, or the AirdropOS API. Only the resulting session file is stored locally.

Prerequisites (in .env):
    TELEGRAM_API_ID=...      # from https://my.telegram.org -> API development tools
    TELEGRAM_API_HASH=...
    TELEGRAM_SESSION_PATH=./data/telegram/agent   # optional

IMPORTANT: the session file gives full access to the Telegram account. Use a
separate account for the agent, keep the file private, and never commit it
(data/ and *.session are git-ignored). To revoke access: Telegram ->
Settings -> Devices -> terminate the session.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> int:
    try:
        from telethon import TelegramClient
    except ImportError:
        print("telethon is not installed. Run: pip install telethon")
        return 1

    from backend.app.core.config import settings

    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH):
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (https://my.telegram.org).")
        return 1

    session_path = Path(settings.TELEGRAM_SESSION_PATH)
    if not session_path.is_absolute():
        session_path = ROOT / session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), int(settings.TELEGRAM_API_ID), str(settings.TELEGRAM_API_HASH))
    # start() prompts for phone, code and (hidden) 2FA password in this terminal.
    await client.start()
    me = await client.get_me()
    print(f"\nLogged in as: {me.first_name or ''} {me.last_name or ''} (@{me.username or 'no-username'}, id {me.id})")
    await client.disconnect()

    for candidate in session_path.parent.glob(session_path.name + ".session*"):
        try:
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)  # owner-only where supported
        except OSError:
            pass

    print(f"Session saved to: {session_path}.session")
    print("Next: set TELEGRAM_USER_ENABLED=true in .env, then restart backend and worker.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
