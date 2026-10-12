"""Provider registry. Register a new integration here; nothing else needs to change."""

from __future__ import annotations

from typing import Dict

from backend.app.live.providers.base import SessionProvider
from backend.app.live.providers.browser import BrowserProvider
from backend.app.live.providers.planned import DiscordProvider, XProvider
from backend.app.live.providers.telegram import TelegramProvider

PROVIDERS: Dict[str, SessionProvider] = {}


def register(provider: SessionProvider) -> None:
    PROVIDERS[provider.integration] = provider


for _p in (BrowserProvider(), TelegramProvider(), XProvider(), DiscordProvider()):
    register(_p)
