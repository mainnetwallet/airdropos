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
from backend.app.tools.airdrop_tools import register_airdrop_tools
from backend.app.tools.arithmetic_tools import VERIFICATION_TOOLS
from backend.app.tools.blockchain_tools import register_blockchain_tools
from backend.app.tools.browser_tools import register_browser_tools
from backend.app.tools.diagnostic_tools import register_diagnostic_tools
from backend.app.tools.miniapp_tools import MINIAPP_TOOLS
from backend.app.tools.notification_tools import register_notification_tools
from backend.app.tools.registry import AuthorizationPolicy, registry
from backend.app.tools.task_tools import register_task_tools
from backend.app.tools.telegram_user_tools import register_telegram_user_tools
from backend.app.tools.transfer_tools import register_transfer_tools
from backend.app.tools.wallet_tools import register_wallet_tools
from backend.app.tools.web_tools import register_web_tools

logger = logging.getLogger("airdropos.tools")

_bootstrapped = False


def bootstrap_tools() -> None:
    """Register every tool the agent may invoke, then apply authorization.

    Permissions are granted by the operator at startup. The agent cannot grant
    itself access to anything not listed here. Safe to call more than once.

    Logs one line carrying the count and nothing else. The tool names are never
    printed at INFO: sixty-odd of them is an unreadable wall that pushes the startup
    summary off the terminal, in BOTH processes. They remain available at DEBUG
    (one line per tool, from `ToolRegistry.register`) and through `GET /tools`.
    """
    global _bootstrapped
    if _bootstrapped:
        return

    register_web_tools(registry)
    register_blockchain_tools(registry)
    register_browser_tools(registry)
    register_notification_tools(registry)
    register_wallet_tools(registry)
    register_task_tools(registry)
    register_diagnostic_tools(registry)
    register_airdrop_tools(registry)
    register_transfer_tools(registry)
    telegram_user_enabled = register_telegram_user_tools(registry)

    # Deterministic verification helpers. Always available: they need no external
    # dependency and are what stop an ordinary arithmetic question from being either
    # guessed at or needlessly escalated to the operator.
    registry.register_or_replace_many([tool() for tool in VERIFICATION_TOOLS])

    # Mini App detection is read-only and needs no credentials, so it is registered
    # unconditionally. `telegram_open_miniapp` additionally requires browser.navigate;
    # if the operator has not granted that, the policy refuses the call at invoke time
    # with a precise reason rather than the tool silently vanishing from the catalog.
    registry.register_or_replace_many([tool() for tool in MINIAPP_TOOLS])

    # Read and analyse capabilities. These are what let the agent answer a question
    # about the system or its own records from chat instead of explaining how the
    # operator could navigate there themselves.
    permissions = [
        "web.read",
        "blockchain.read",
        "browser.read",
        "browser.navigate",
        "browser.interact",
        # Starting/stopping/inspecting a saved Chrome profile, and reading or opening
        # extension surfaces. Separate from browser.interact so a deployment can hand out
        # page automation without handing out "open this profile's browser".
        "browser.profile",
        "browser.extension",
        "notify.send",
        "report.generate",
        "social.read",
        "wallet.read",
        "wallet.write",
        "task.read",
        "task.write",
        "tool.read",
        "system.read",
        "airdrop.read",
        "transaction.read",
        # Recording a human's approval of one specific transfer is a state change
        # the agent may perform only AFTER the operator has confirmed the details it
        # was shown. It does not by itself authorise a broadcast: signing and
        # submission stay separately gated below and by the emergency stop.
        "transaction.approve",
    ]
    if telegram_user_enabled:
        permissions += ["telegram.read", "telegram.interact"]

    # Signing and broadcasting require a named human approval of one specific
    # transaction, enforced by `ApprovalService` and `TransactionEngine`. The policy
    # flags below are the operator's process-wide opt-in; they are still not
    # sufficient on their own - an approval row bound to the exact transaction is
    # required for every broadcast.
    value_moving_enabled = _value_moving_enabled()

    registry.policy = AuthorizationPolicy(
        enabled_permissions=permissions + (["wallet.sign", "transaction.submit"] if value_moving_enabled else []),
        allowed_networks=known_networks(),
        allow_web_interaction=True,
        allow_transaction_submission=value_moving_enabled,
        allow_wallet_signing=value_moving_enabled,
    )
    _bootstrapped = True
    logger.info(
        "Registered %s tools (telegram user client: %s, value-moving: %s)",
        len(registry._tools),
        "ENABLED" if telegram_user_enabled else "disabled",
        "ENABLED" if value_moving_enabled else "disabled (no signing/submission permission)",
    )


def _value_moving_enabled() -> bool:
    """Whether the operator opted this deployment into signing and broadcasting.

    Deliberately opt-in and off by default. Reading this from the environment rather
    than hardcoding it keeps "value moving is off unless an operator turns it on" a
    property of the deployment instead of a property of the source.
    """
    from backend.app.core.config import settings

    raw = str(getattr(settings, "ALLOW_VALUE_MOVING", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")
