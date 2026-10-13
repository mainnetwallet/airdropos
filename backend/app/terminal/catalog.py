"""Tool inventory and categories for the dashboard.

The registry is the authority on what exists. ``bootstrap_tools()`` is the same
call the API and the worker make, so the inventory shown here is the inventory
those processes run with - not a hand-maintained list that can drift.

Categories are derived from each tool's declared ``required_permissions``,
which is the registry's own authorisation vocabulary rather than an invention
of this module. Every tool declares at least one permission (the registry
refuses to authorise a side-effecting tool that declares none), so every tool
lands in exactly one category and the counts sum to the registered total. The
only exception is a deliberately harmless tool that declares none, which is
reported under "Others" rather than being hidden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

logger = logging.getLogger("airdropos.tools")


@dataclass(frozen=True)
class ToolCategory:
    """One row of the REGISTERED TOOLS panel."""

    label: str
    icon: str
    count: int
    badge: str = ""
    badge_style: str = ""


@dataclass
class ToolInventory:
    total: int = 0
    categories: List[ToolCategory] = field(default_factory=list)
    loaded: bool = False
    detail: str = ""


# Ordered exactly as the reference panel reads, top to bottom.
_CATEGORY_ORDER: Sequence[Tuple[str, str]] = (
    ("Browser Automation", "▭"),
    ("Telegram User Client", "✈"),
    ("Blockchain / Wallet", "⬡"),
    ("Research & Discovery", "◉"),
    ("Task Management", "≡"),
    ("Reporting & Analysis", "▤"),
    ("System & Diagnostics", "⚙"),
    ("Others", "⋯"),
)


def classify(permissions: Sequence[str]) -> str:
    """Map a tool's declared permissions onto a display category."""
    perms = {str(p) for p in permissions}
    if any(p.startswith("browser.") for p in perms):
        return "Browser Automation"
    if any(p.startswith("telegram.") for p in perms):
        return "Telegram User Client"
    # Signing and submitting are chain operations on a wallet, so they belong
    # with the other wallet capabilities rather than in an overflow bucket.
    if perms & {
        "blockchain.read",
        "wallet.read",
        "wallet.write",
        "wallet.sign",
        "transaction.submit",
    }:
        return "Blockchain / Wallet"
    if perms & {"web.read", "airdrop.read"}:
        return "Research & Discovery"
    if any(p.startswith("task.") for p in perms):
        return "Task Management"
    if perms & {"report.generate", "notify.send", "transaction.read", "transaction.approve"}:
        return "Reporting & Analysis"
    if perms & {"system.read", "tool.read"}:
        return "System & Diagnostics"
    return "Others"


def _telegram_configured() -> Tuple[bool, str]:
    """Whether the Telegram user client is switched on, and why not if it is off.

    Read from the same settings the bootstrap reads, so the badge in the panel is
    the deployment's real configuration rather than a guess.
    """
    try:
        from backend.app.core.config import settings
    except Exception:  # pragma: no cover - settings import failure
        return False, ""
    try:
        if bool(settings.TELEGRAM_USER_ENABLED):
            return True, "ENABLED"
        return False, "DISABLED"
    except Exception:  # pragma: no cover
        return False, ""


def build_inventory() -> ToolInventory:
    """Load the registry in this process and summarise it.

    The bootstrap log line is suppressed here: the API and the worker already
    report their own inventory, and a third copy would put a duplicate "Registered
    N tools" line in the activity feed that names a process the operator did not
    start. The inventory itself is unchanged.
    """
    from backend.app.tools.bootstrap import bootstrap_tools
    from backend.app.tools.registry import registry

    previous_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        bootstrap_tools()
        tools = list(registry._tools.values())
    except Exception as exc:
        logger.setLevel(previous_level)
        return ToolInventory(loaded=False, detail=f"{type(exc).__name__}: {exc}")
    logger.setLevel(previous_level)

    counts: Dict[str, int] = {label: 0 for label, _ in _CATEGORY_ORDER}
    for tool in tools:
        counts[classify(tool.required_permissions)] = counts.get(
            classify(tool.required_permissions), 0
        ) + 1

    telegram_enabled, telegram_badge = _telegram_configured()
    categories: List[ToolCategory] = []
    for label, icon in _CATEGORY_ORDER:
        count = counts.get(label, 0)
        # The overflow row only appears when the registry actually holds
        # something outside every named category. Showing "0 tools" would imply a
        # category that does not exist.
        if count == 0 and label == "Others":
            continue
        badge = ""
        badge_style = ""
        if label == "Telegram User Client":
            badge = telegram_badge or ("ENABLED" if telegram_enabled else "")
            badge_style = "bold green" if telegram_enabled else "bold yellow"
        categories.append(
            ToolCategory(label=label, icon=icon, count=count, badge=badge, badge_style=badge_style)
        )

    return ToolInventory(total=len(tools), categories=categories, loaded=True)