"""Autonomous Execution Policy: what the agent may do without asking the owner.

This is the authorization source for unattended operation. It is *configuration*, not
code: an operator edits `.env` (or a JSON file it points at) to decide which operation
classes the agent may execute on its own. Nothing here has to be recompiled.

Three properties matter more than the feature itself:

1. **Deny by default.** With no policy configured, every category is refused. Adding
   autonomy is always an explicit, visible act.

2. **The policy is the authorization, never the model.** `evaluate()` takes facts the
   caller already had (amount, network, destination, balance) and returns a decision.
   The LLM cannot reach it: it never supplies a decision, an identity, or a limit. The
   only thing the model contributes is the operation itself.

3. **Every autonomous action is attributable.** A decision names the rule that made it,
   and that rule id is written onto the approval and into the audit log, so "why did
   this send money without asking" has a precise, checkable answer.

The Emergency Stop is not part of this module and is not overridable here. It is
checked by the caller *before* the policy, so no policy can re-enable execution while
the platform is halted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class Category:
    """The vocabulary of autonomous operation classes.

    A closed set on purpose. A category the code does not recognise is refused, so a
    typo in configuration cannot silently widen authority.
    """

    # --- Read / research -------------------------------------------------
    WEB_FETCH = "web.fetch"
    CAMPAIGN_DISCOVER = "campaign.discover"
    CAMPAIGN_RESEARCH = "campaign.research"
    ELIGIBILITY_EVALUATE = "eligibility.evaluate"

    # --- Browser ---------------------------------------------------------
    BROWSER_NAVIGATE = "browser.navigate"
    BROWSER_INTERACT = "browser.interact"

    # --- Task queue ------------------------------------------------------
    TASK_CREATE = "task.create"
    TASK_RESUME = "task.resume"
    TASK_CANCEL = "task.cancel"

    # --- Platform --------------------------------------------------------
    DIAGNOSTICS_RUN = "diagnostics.run"
    EVIDENCE_COLLECT = "evidence.collect"

    # --- Value moving ----------------------------------------------------
    TRANSACTION_PREPARE = "transaction.prepare"
    TRANSACTION_APPROVE = "transaction.approve"
    TRANSACTION_SUBMIT = "transaction.submit"
    TX_NATIVE_TRANSFER = "tx.native_transfer"
    TX_TOKEN_TRANSFER = "tx.token_transfer"


ALL_CATEGORIES = frozenset(
    v for k, v in vars(Category).items() if not k.startswith("_") and isinstance(v, str)
)

# Categories that move value. Separated so a reader can see at a glance which rules
# are the dangerous ones, and so the settings screen can group them.
VALUE_CATEGORIES = frozenset({Category.TX_NATIVE_TRANSFER, Category.TX_TOKEN_TRANSFER})

ACTION_ALLOW = "allow"
ACTION_DENY = "deny"


class PolicyError(Exception):
    """The configured policy is malformed."""


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating one operation against the policy."""

    allowed: bool
    category: str
    reason: str
    rule_id: Optional[str] = None
    # The ceiling that applied, echoed for the audit log and the operator's UI.
    max_value_usd: Optional[float] = None

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "category": self.category,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "max_value_usd": self.max_value_usd,
        }


@dataclass
class Rule:
    """One configured rule: a category, whether it is allowed, and its ceilings."""

    rule_id: str
    category: str
    action: str = ACTION_ALLOW
    # Ceilings. None means "defer to the platform's existing configured limit"
    # (settings.MAX_TRANSACTION_VALUE_USD / MAX_DAILY_GAS_SPEND_USD), never a
    # number invented here.
    max_value_usd: Optional[float] = None
    max_value_native: Optional[str] = None
    allowed_networks: Optional[List[str]] = None
    allowed_tokens: Optional[List[str]] = None
    allowed_destinations: Optional[List[str]] = None
    allowed_source_wallets: Optional[List[str]] = None
    # Wait for and verify a receipt before reporting the operation done.
    require_receipt: bool = True
    # Extra free-form notes, surfaced in the UI. Not interpreted.
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "action": self.action,
            "max_value_usd": self.max_value_usd,
            "max_value_native": self.max_value_native,
            "allowed_networks": self.allowed_networks,
            "allowed_tokens": self.allowed_tokens,
            "allowed_destinations": self.allowed_destinations,
            "allowed_source_wallets": self.allowed_source_wallets,
            "require_receipt": self.require_receipt,
            "description": self.description,
        }


def _normalise_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return [p.lower() for p in parts if p] or None
    if isinstance(value, (list, tuple)):
        out = [str(v).strip().lower() for v in value if str(v).strip()]
        return out or None
    raise PolicyError(f"Expected a list or comma-separated string, got {type(value).__name__}")


def _as_float(value: Any, field_name: str) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{field_name} must be a number, got {value!r}") from exc


def _parse_rule(raw: Mapping[str, Any], index: int) -> Rule:
    category = str(raw.get("category") or "").strip().lower()
    if not category:
        raise PolicyError(f"rules[{index}] has no 'category'")
    if category not in ALL_CATEGORIES:
        raise PolicyError(
            f"rules[{index}] has unknown category {category!r}. "
            f"Known categories: {', '.join(sorted(ALL_CATEGORIES))}"
        )

    action = str(raw.get("action") or ACTION_ALLOW).strip().lower()
    if action not in (ACTION_ALLOW, ACTION_DENY):
        raise PolicyError(f"rules[{index}] action must be 'allow' or 'deny', got {action!r}")

    rule_id = str(raw.get("id") or f"{category}#{index}").strip()
    return Rule(
        rule_id=rule_id,
        category=category,
        action=action,
        max_value_usd=_as_float(raw.get("max_value_usd"), f"rules[{index}].max_value_usd"),
        max_value_native=(
            str(raw["max_value_native"]) if raw.get("max_value_native") is not None else None
        ),
        allowed_networks=_normalise_list(raw.get("allowed_networks")),
        allowed_tokens=_normalise_list(raw.get("allowed_tokens")),
        allowed_destinations=_normalise_list(raw.get("allowed_destinations")),
        allowed_source_wallets=_normalise_list(raw.get("allowed_source_wallets")),
        require_receipt=bool(raw.get("require_receipt", True)),
        description=str(raw.get("description") or ""),
    )


@dataclass
class AutonomousPolicy:
    """The parsed policy. Immutable after construction."""

    enabled: bool = False
    rules: List[Rule] = field(default_factory=list)
    source: str = "defaults"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_raw(cls, raw: Any, source: str = "config") -> "AutonomousPolicy":
        """Parse a policy from a mapping, a JSON string, or None.

        Malformed configuration is an error, never a silent default: an operator who
        typed a bad policy must find out, rather than believing the agent has more
        authority than it does (or, worse, less).
        """
        if raw is None or raw == "":
            return cls(enabled=False, rules=[], source=f"{source}:unset")

        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PolicyError(f"{source} is not valid JSON: {exc}") from exc

        if not isinstance(raw, Mapping):
            raise PolicyError(f"{source} must be a JSON object")

        rules_raw = raw.get("rules") or []
        if not isinstance(rules_raw, list):
            raise PolicyError(f"{source}: 'rules' must be a list")

        rules = [_parse_rule(item, i) for i, item in enumerate(rules_raw) if isinstance(item, Mapping)]

        enabled = raw.get("enabled")
        if enabled is None:
            # A policy with rules but no explicit flag is treated as a decision to
            # turn autonomy on; an empty rule list is treated as off.
            enabled = bool(rules)

        return cls(enabled=bool(enabled), rules=rules, source=source)

    @classmethod
    def load(cls) -> "AutonomousPolicy":
        """Read the policy from `AUTONOMOUS_POLICY_FILE` or `AUTONOMOUS_POLICY`.

        A file is preferred when present: multi-line JSON in a `.env` file is a
        reliable source of escaping mistakes on Windows, and a typo there should be
        an obvious file the operator can open and fix.
        """
        path = str(getattr(settings, "AUTONOMOUS_POLICY_FILE", "") or "").strip()
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return cls.from_raw(handle.read(), source=path)

        inline = getattr(settings, "AUTONOMOUS_POLICY", None)
        return cls.from_raw(inline, source="AUTONOMOUS_POLICY")

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def rule_for(self, category: str) -> Optional[Rule]:
        """First matching rule wins; later rules for the same category are shadowed."""
        wanted = str(category or "").strip().lower()
        for rule in self.rules:
            if rule.category == wanted:
                return rule
        return None

    def evaluate(
        self,
        category: str,
        *,
        network: Optional[str] = None,
        wallet: Optional[str] = None,
        destination: Optional[str] = None,
        token: Optional[str] = None,
        value_usd: Optional[float] = None,
        value_native: Optional[str] = None,
    ) -> Decision:
        """Decide whether this operation may run unattended.

        Every parameter is a fact about the operation that the caller had before
        asking. None of them come from the model, and the decision itself is computed
        here, in code.
        """
        name = str(category or "").strip().lower()

        if name not in ALL_CATEGORIES:
            return Decision(
                False, name, f"'{name}' is not a known operation category, so it cannot be authorized."
            )

        if not self.enabled:
            return Decision(False, name, "Autonomous execution is disabled (AUTONOMOUS_POLICY not enabled).")

        rule = self.rule_for(name)
        if rule is None:
            return Decision(
                False, name, f"No autonomous rule for '{name}'. The policy is deny-by-default."
            )
        if rule.action == ACTION_DENY:
            return Decision(False, name, f"Rule '{rule.rule_id}' explicitly denies '{name}'.", rule.rule_id)

        verdict = self._check_constraints(rule, network, wallet, destination, token, value_usd, value_native)
        if verdict is not None:
            return Decision(False, name, verdict, rule.rule_id)

        return Decision(
            True,
            name,
            f"Authorized by autonomous rule '{rule.rule_id}'.",
            rule.rule_id,
            max_value_usd=rule.max_value_usd,
        )

    def _check_constraints(
        self,
        rule: Rule,
        network: Optional[str],
        wallet: Optional[str],
        destination: Optional[str],
        token: Optional[str],
        value_usd: Optional[float],
        value_native: Optional[str],
    ) -> Optional[str]:
        """Return a refusal reason, or None when every constraint is satisfied."""

        if rule.allowed_networks is not None:
            if not network:
                return f"Rule '{rule.rule_id}' restricts '{rule.category}' to {', '.join(rule.allowed_networks)}, but no network was stated."
            if str(network).strip().lower() not in rule.allowed_networks:
                return (
                    f"Network '{network}' is not in the autonomous rule's allowed networks "
                    f"({', '.join(rule.allowed_networks)})."
                )

        if rule.allowed_source_wallets is not None:
            if not wallet:
                return f"Rule '{rule.rule_id}' restricts '{rule.category}' to specific source wallets, but none was stated."
            if str(wallet).strip().lower() not in rule.allowed_source_wallets:
                return f"Source wallet {wallet} is not permitted by rule '{rule.rule_id}'."

        if rule.allowed_destinations is not None:
            if not destination:
                return f"Rule '{rule.rule_id}' restricts '{rule.category}' to specific destinations, but none was stated."
            if str(destination).strip().lower() not in rule.allowed_destinations:
                return (
                    f"Destination {destination} is not in the autonomous rule's allowed list "
                    f"({len(rule.allowed_destinations)} entr{'y' if len(rule.allowed_destinations) == 1 else 'ies'})."
                )

        if rule.allowed_tokens is not None:
            if not token:
                return f"Rule '{rule.rule_id}' restricts '{rule.category}' to specific tokens, but none was stated."
            if str(token).strip().lower() not in rule.allowed_tokens:
                return (
                    f"Token '{token}' is not in the autonomous rule's allowed tokens "
                    f"({', '.join(rule.allowed_tokens)})."
                )

        # Value ceilings. An unset ceiling defers to the platform's own configured
        # limits rather than inventing one here.
        if value_native is not None and rule.max_value_native is not None:
            try:
                ceiling = Decimal(str(rule.max_value_native))
                actual = Decimal(str(value_native))
            except (InvalidOperation, ValueError):
                return (
                    f"Rule '{rule.rule_id}' cannot compare the amount: "
                    f"'{value_native}' is not a decimal number."
                )
            if actual > ceiling:
                return (
                    f"Amount {value_native} exceeds the autonomous limit of "
                    f"{rule.max_value_native} set by rule '{rule.rule_id}'."
                )

        if value_usd is not None:
            ceiling = rule.max_value_usd
            if ceiling is None:
                ceiling = float(settings.MAX_TRANSACTION_VALUE_USD)
            if value_usd > ceiling:
                source = (
                    f"rule '{rule.rule_id}'"
                    if rule.max_value_usd is not None
                    else "MAX_TRANSACTION_VALUE_USD"
                )
                return (
                    f"Value ${value_usd:.2f} exceeds the ${ceiling:.2f} limit from {source}."
                )

        return None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """The whole policy, for the operator UI and for diagnostics."""
        return {
            "enabled": self.enabled,
            "source": self.source,
            "rules": [r.to_dict() for r in self.rules],
            "value_moving": self.enabled and any(
                r.category in VALUE_CATEGORIES and r.action == ACTION_ALLOW for r in self.rules
            ),
            "categories_known": sorted(ALL_CATEGORIES),
            "note": (
                "Autonomy is deny-by-default. A category with no allow rule is refused. "
                "The Emergency Stop overrides everything here and cannot be re-enabled by a policy."
            ),
        }


_cached: Optional[AutonomousPolicy] = None


def get_policy() -> AutonomousPolicy:
    """The process-wide policy, parsed once."""
    global _cached
    if _cached is None:
        _cached = AutonomousPolicy.load()
        logger.info(
            "Autonomous policy loaded from %s: enabled=%s, %s rule(s)",
            _cached.source, _cached.enabled, len(_cached.rules),
        )
    return _cached


def reset_policy_cache() -> None:
    """Drop the parsed policy so the next call re-reads configuration."""
    global _cached
    _cached = None


# The identity recorded on approvals the policy authorized. Deliberately not a person
# and deliberately not something the model can supply: the verifier re-evaluates the
# policy live against the actual transaction, so naming this string in a tool call
# achieves nothing on its own.
POLICY_APPROVER_PREFIX = "policy:autonomous"


def policy_approver_identity(rule_id: str) -> str:
    return f"{POLICY_APPROVER_PREFIX}:{rule_id}"


def is_policy_approver(identity: Optional[str]) -> bool:
    return bool(identity) and str(identity).strip().lower().startswith(POLICY_APPROVER_PREFIX)


def rule_id_from_approver(identity: Optional[str]) -> Optional[str]:
    if not is_policy_approver(identity):
        return None
    return str(identity).split(":", 2)[-1] or None


EXAMPLE = """{
  "enabled": true,
  "rules": [
    {"category": "web.fetch", "action": "allow"},
    {"category": "campaign.discover", "action": "allow"},
    {"category": "campaign.research", "action": "allow"},
    {"category": "eligibility.evaluate", "action": "allow"},
    {"category": "task.create", "action": "allow"},
    {"category": "task.resume", "action": "allow"},
    {"category": "diagnostics.run", "action": "allow"},
    {"category": "evidence.collect", "action": "allow"},
    {"category": "browser.navigate", "action": "allow"},
    {
      "id": "eth-transfers",
      "category": "tx.native_transfer",
      "action": "allow",
      "allowed_networks": ["ethereum", "base"],
      "max_value_native": "0.05",
      "require_receipt": true,
      "description": "Small ETH transfers on mainnet and Base."
    }
  ]
}"""