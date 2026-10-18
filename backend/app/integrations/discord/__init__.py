"""Discord integration for AirdropOS - browser-only, on the operator's own accounts.

Architecture
------------
There is NO Discord bot in the working integration. No bot token, no gateway, no
Discord API client, and no unofficial endpoint. Discord is driven the way a person
drives it: the ordinary discord.com web app, opened in one of the existing persistent
Chrome **browser profiles** that the operator signs into by hand.

That is a deliberate choice, not a limitation of effort:

* The operator's existing sessions keep working, including servers a bot cannot see.
* There is no second identity on a server and no extra credential to store or leak.
* Each account is a separate browser profile, so accounts stay genuinely isolated -
  separate cookies, separate storage, separate directories - using the mechanism the
  project already has for that.

Everything reuses existing infrastructure: the browser controller and its per-profile
interaction lock, the task engine and its checkpoints, the Inbox intervention system,
the tool registry's permissions and side-effect policy, and the audit log. No second
browser, no second task engine, no second login-request system.

Modules
-------
``browser``   the adapter over the existing browser controller (the only module that
              touches Playwright)
``service``   the single executor the tools and the API both call
``routing``   the operation catalog and the read/write intent boundary
``state``     the Discord part of a task checkpoint
``store``     activity history, and the duplicate/uncertain-send guards
``models``    value objects shared across the above
``errors``    error taxonomy; only CAT_LOGIN_REQUIRED may become a login request
``bot``       the legacy, non-functional bot router, preserved for compatibility
"""

from __future__ import annotations

from backend.app.integrations.discord.bot import (  # noqa: F401  (compatibility re-export)
    DiscordAuthError,
    DiscordBot,
    discord_bot,
)

__all__ = [
    "DiscordAuthError",
    "DiscordBot",
    "discord_bot",
]