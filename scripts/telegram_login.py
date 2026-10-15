#!/usr/bin/env python3
"""One-time Telegram login for the AirdropOS agent (run in YOUR terminal).

    python scripts/telegram_login.py                 # asks for a label
    python scripts/telegram_login.py --label farm-2  # add another account
    python scripts/telegram_login.py --list          # show logged-in accounts

Run it once per Telegram account. Each account gets its own label and session file
(data/telegram/accounts/<label>.session). The agent picks the default account unless
a task names a label.

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

import argparse
import asyncio
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log a Telegram account in for the AirdropOS agent.")
    parser.add_argument("--label", help="Name for this account, e.g. main, farm-2")
    parser.add_argument("--default", action="store_true", help="Make this the default account")
    parser.add_argument("--list", action="store_true", help="List registered accounts and exit")
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    from backend.app.integrations import telegram_accounts as registry

    if args.list:
        accounts = registry.list_accounts()
        if not accounts:
            print("No Telegram accounts yet.")
        for a in accounts:
            star = " (default)" if a.get("is_default") else ""
            uname = f"@{a['username']}" if a.get("username") else "no-username"
            print(f"- {a['label']}{star}: {a.get('profile_name') or '-'} ({uname}), "
                  f"{'logged in' if a['session_ready'] else 'NOT logged in'}")
        return 0

    try:
        from telethon import TelegramClient
    except ImportError:
        print("telethon is not installed. Run: pip install telethon")
        return 1

    from backend.app.core.config import settings

    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH):
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (https://my.telegram.org).")
        return 1

    label = (args.label or "").strip()
    while not registry.valid_label(label):
        label = input("Label for this account (e.g. main, farm-2): ").strip()
        if not registry.valid_label(label):
            print("Use 1-40 characters: letters, digits, space, _ . -")
    existing = registry.get_account(label)
    if existing and existing.get("session_ready") and not existing.get("legacy"):
        print(f"'{label}' already exists; it will be reused if still logged in.")
    slug = registry.slugify(label)
    session_path = registry.accounts_dir() / slug
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), int(settings.TELEGRAM_API_ID), str(settings.TELEGRAM_API_HASH))
    # start() prompts for phone, code and (hidden) 2FA password in this terminal.
    await client.start()
    me = await client.get_me()
    print(f"\nLogged in as: {me.first_name or ''} {me.last_name or ''} (@{me.username or 'no-username'}, id {me.id})")
    await client.disconnect()

    # Store the profile name/username/id (never the phone number) under the label.
    rel = os.path.relpath(session_path, ROOT)
    registry.add_account(label, me, rel, make_default=args.default)

    for candidate in session_path.parent.glob(session_path.name + ".session*"):
        try:
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)  # owner-only where supported
        except OSError:
            pass

    print(f"Account '{label}' saved. Session: {session_path}.session")
    print("Next: set TELEGRAM_USER_ENABLED=true in .env, then restart backend and worker.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
