"""Integrations that exist in AirdropOS but have NO live-session provider yet.

They are declared (so the UI can list them honestly with zero sessions) but they
report no capabilities. Adding a real provider later means writing one class and
registering it; the Live Broadcast page does not change.
"""

from __future__ import annotations

from backend.app.core.config import settings
from backend.app.live.models import Capabilities
from backend.app.live.providers.base import SessionProvider


class _Planned(SessionProvider):
    capabilities = Capabilities(events=False, metadata=False)

    def descriptor(self):
        d = super().descriptor()
        d.available = False
        return d


class XProvider(_Planned):
    integration = "x"
    label = "X.com"
    note = "API monitor only. No live X.com session provider is implemented yet."

    def configured(self) -> bool:
        return bool(settings.TWITTER_BEARER_TOKEN or settings.TWITTER_API_KEY)


class DiscordProvider(_Planned):
    integration = "discord"
    label = "Discord"
    note = "Bot integration only. No live Discord session provider is implemented yet."

    def configured(self) -> bool:
        return bool(settings.DISCORD_BOT_TOKEN)
