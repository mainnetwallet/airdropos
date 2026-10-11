"""Shared tool bootstrap for every AirdropOS process.

The API (``backend.app.main``) and the background worker
(``backend.app.scheduler.worker``) are SEPARATE processes with separate tool
registries. Before this module existed only the API registered tools, so the
worker (the process that actually executes tasks) had an empty catalog and every
planned step lost its tool. Both processes now call ``bootstrap_tools()``.
"""

from __future__ import annotations

import logging

from backend.app.blockchain.chains import known_networks
from backend.app.tools.blockchain_tools import register_blockchain_tools
from backend.app.tools.browser_tools import register_browser_tools
from backend.app.tools.notification_tools import register_notification_tools
from backend.app.tools.registry import AuthorizationPolicy, registry
from backend.app.tools.telegram_user_tools import register_telegram_user_tools
from backend.app.tools.web_tools import register_web_tools

logger = logging.getLogger("airdropos.tools")

_bootstrapped = False


def bootstrap_tools() -> None:
    """Register every tool the agent may invoke, then apply authorization.

    Permissions are granted by the operator at startup. The agent cannot grant
    itself access to anything not listed here. Safe to call more than once.
    """
    global _bootstrapped
    if _bootstrapped:
        return

    register_web_tools(registry)
    register_blockchain_tools(registry)
    register_browser_tools(registry)
    register_notification_tools(registry)
    telegram_user_enabled = register_telegram_user_tools(registry)

    permissions = [
        "web.read",
        "blockchain.read",
        "browser.read",
        "browser.navigate",
        "browser.interact",
        "notify.send",
        "report.generate",
        "social.read",
    ]
    if telegram_user_enabled:
        permissions += ["telegram.read", "telegram.interact"]

    registry.policy = AuthorizationPolicy(
        enabled_permissions=permissions,
        allowed_networks=known_networks(),
        allow_web_interaction=True,
        # Value-moving permissions are intentionally NOT granted by default.
        # Enabling them is an explicit operator decision.
        allow_transaction_submission=False,
        allow_wallet_signing=False,
    )
    _bootstrapped = True
    logger.info(
        "Registered %s tools (telegram user client: %s) with restricted authorization policy",
        len(registry._tools),
        "ENABLED" if telegram_user_enabled else "disabled",
    )
