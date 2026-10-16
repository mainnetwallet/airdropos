"""Central Tool Registry & Invocation Layer.

The LLM may only select tools that are:
  1. Registered here.
  2. Enabled (development_status permits production execution).
  3. Permitted by the user's authorization policy.
  4. Not blocked by the emergency stop.

There is deliberately NO unrestricted shell, SQL, filesystem, or raw network
access exposed to the LLM. Capability is expressed solely as registered tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from backend.app.blockchain.chains import known_networks
from backend.app.core.config import settings
from backend.app.security.emergency_stop import StopPolicy, emergency_stop
from backend.app.tools.context import task_context
from backend.app.tools.timeouts import history as latency_history
from backend.app.tools.timeouts import resolve_timeout
from backend.app.tools.base import (
    BaseTool,
    SideEffect,
    ToolResult,
    ToolResultStatus,
    ToolValidationError,
)

logger = logging.getLogger(__name__)


class AuthorizationPolicy:
    """Persistent user authorization, configured once, enforced everywhere.

    This is deterministic code, not an LLM instruction. The agent cannot grant
    itself new permissions.
    """

    def __init__(
        self,
        enabled_permissions: Optional[List[str]] = None,
        allowed_networks: Optional[List[str]] = None,
        max_tx_value_usd: Optional[float] = None,
        max_daily_spend_usd: Optional[float] = None,
        allowed_wallet_ids: Optional[List[str]] = None,
        allow_web_interaction: bool = True,
        allow_transaction_submission: bool = True,
        allow_wallet_signing: bool = True,
    ) -> None:
        self.enabled_permissions = set(enabled_permissions or [])
        # `None` means "not configured" and takes the supported-chain default. An
        # explicit empty list is an operator decision to allow NO network; it must
        # not silently widen to every network.
        self.allowed_networks = {
            str(n).strip().lower()
            for n in (known_networks() if allowed_networks is None else allowed_networks)
            if str(n).strip()
        }
        self.max_tx_value_usd = (
            max_tx_value_usd if max_tx_value_usd is not None else settings.MAX_TRANSACTION_VALUE_USD
        )
        self.max_daily_spend_usd = (
            max_daily_spend_usd if max_daily_spend_usd is not None else settings.MAX_DAILY_GAS_SPEND_USD
        )
        # Both are Optional: the platform ships with no fixed application-level amount or
        # daily-spend cap, so `None` is a normal, truthful state here rather than a gap.
        # Reported by `describe()` for the operator UI; enforcement, where configured, is
        # done atomically by the spend ledger rather than here.
        # Deny-by-default: no wallet id is permitted unless explicitly listed.
        self.allowed_wallet_ids = {str(w).strip() for w in (allowed_wallet_ids or []) if str(w).strip()}
        self.allow_web_interaction = allow_web_interaction
        self.allow_transaction_submission = allow_transaction_submission
        self.allow_wallet_signing = allow_wallet_signing

    def permits(self, tool: BaseTool) -> tuple[bool, str]:
        required = set(tool.required_permissions)
        # Fail closed: a tool that declares NO permissions would otherwise be allowed by every
        # policy, including the deny-all default, because the empty set is a subset of anything.
        # Only a pure, side-effect-free tool may omit them.
        if not required and tool.side_effect is not SideEffect.NONE:
            return False, (
                f"Tool '{tool.name}' declares no required permissions but has side effect "
                f"'{tool.side_effect.value}'; refusing."
            )
        if not required.issubset(self.enabled_permissions):
            missing = sorted(required - self.enabled_permissions)
            return False, f"Permission(s) not granted: {', '.join(missing)}"

        if tool.side_effect is SideEffect.WEB_INTERACTION and not self.allow_web_interaction:
            return False, "Web interaction is disabled by authorization policy."
        if tool.side_effect is SideEffect.TRANSACTION_SUBMISSION and not self.allow_transaction_submission:
            return False, "Transaction submission is disabled by authorization policy."
        if tool.side_effect is SideEffect.WALLET_SIGNING and not self.allow_wallet_signing:
            return False, "Wallet signing is disabled by authorization policy."

        return True, ""

    def permits_network(self, network: Any) -> tuple[bool, str]:
        """Fail closed: a missing, blank or non-string network is never permitted."""
        if not isinstance(network, str) or not network.strip():
            return False, "A network must be specified and be on the allowed network list."
        if network.strip().lower() not in self.allowed_networks:
            return False, f"Network '{network}' is not in the allowed network list."
        return True, ""

    def permits_wallet(self, wallet_id: Any) -> tuple[bool, str]:
        """Fail closed: only explicitly allowed wallet ids pass."""
        if not isinstance(wallet_id, str) or not wallet_id.strip():
            return False, "A wallet id must be specified and be on the allowed wallet list."
        if wallet_id.strip() not in self.allowed_wallet_ids:
            return False, f"Wallet '{wallet_id}' is not in the allowed wallet list."
        return True, ""

    def permits_arguments(self, tool: BaseTool, params: Dict[str, Any]) -> tuple[bool, str]:
        """Enforce the network and wallet allowlists on a tool call's arguments.

        Applies to any tool whose schema declares a `network`, `wallet_id` or
        `wallet_ids` parameter, so a new tool cannot opt out by forgetting a check.

        An OPTIONAL parameter is only checked when the caller actually supplied it.
        Treating presence in the schema as presence in the call was a real defect: it
        refused `get_wallet_address` with no `network` (a perfectly valid lookup of a
        locally stored address) with "A network must be specified", and would have
        refused every optional-network tool the same way.
        """
        props = (tool.input_schema or {}).get("properties", {})
        required = set((tool.input_schema or {}).get("required", []))

        if "network" in props and params.get("network") is not None:
            ok, reason = self.permits_network(params.get("network"))
            if not ok:
                return False, reason
        if "wallet_id" in props and params.get("wallet_id") is not None:
            ok, reason = self.permits_wallet(params.get("wallet_id"))
            if not ok:
                return False, reason
        if "wallet_ids" in props:
            ids = params.get("wallet_ids")
            if ids is None:
                if "wallet_ids" in required:
                    return False, "At least one wallet id must be specified."
            else:
                if not isinstance(ids, (list, tuple)) or not ids:
                    return False, "At least one wallet id must be specified."
                for wallet_id in ids:
                    ok, reason = self.permits_wallet(wallet_id)
                    if not ok:
                        return False, reason
        return True, ""

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled_permissions": sorted(self.enabled_permissions),
            "allowed_networks": sorted(self.allowed_networks),
            "allowed_wallet_ids": sorted(self.allowed_wallet_ids),
            "max_transaction_value_usd": self.max_tx_value_usd,
            "max_daily_spend_usd": self.max_daily_spend_usd,
            "web_interaction_allowed": self.allow_web_interaction,
            "transaction_submission_allowed": self.allow_transaction_submission,
            "wallet_signing_allowed": self.allow_wallet_signing,
        }


DEFAULT_POLICY = AuthorizationPolicy(
    enabled_permissions=[],
    allow_web_interaction=False,
    allow_transaction_submission=False,
    allow_wallet_signing=False,
)


class ToolRegistry:
    """Holds every tool and mediates all invocations."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}
        self._disabled: Dict[str, str] = {}
        self._metrics: Dict[str, Dict[str, float]] = {}
        self._inflight: Dict[str, int] = {}
        self._tools_lock = asyncio.Lock()
        # Deny-by-default. Application startup replaces this with the operator's
        # policy (see `bootstrap_tools`). It is declared here rather than assigned
        # ad hoc by callers so that "no policy was ever configured" stays a real,
        # enforceable state instead of an attribute that silently does not exist.
        self.policy: AuthorizationPolicy = DEFAULT_POLICY

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        self._metrics.setdefault(tool.name, {"success": 0, "failure": 0, "total_ms": 0.0, "calls": 0})
        # DEBUG, not INFO: the API and the worker each hold their own registry, so an
        # INFO line here printed the whole inventory twice on every start and buried
        # the startup banner. The inventory is logged once instead, by bootstrap.
        logger.debug("Registered tool: %s v%s (%s)", tool.name, tool.version, tool.side_effect.value)

    def register_many(self, tools: List[BaseTool]) -> None:
        for tool in tools:
            self.register(tool)

    def register_or_replace(self, tool: BaseTool) -> None:
        """Idempotent registration used by application bootstrap.

        `register` stays strict on purpose — two modules silently claiming the
        same tool name is a defect worth failing on. Startup is different: the
        registry is a process-global, and bootstrap may legitimately run more
        than once (reload, multiple workers sharing a registry, test fixtures).
        Re-registering the same built-in tool is therefore a no-op, not an error.
        """
        if tool.name in self._tools:
            self._tools[tool.name] = tool
            self._metrics.setdefault(
                tool.name, {"success": 0, "failure": 0, "total_ms": 0.0, "calls": 0}
            )
            return
        self.register(tool)

    def register_or_replace_many(self, tools: List[BaseTool]) -> None:
        """Idempotent bulk form of :meth:`register_or_replace`."""
        for tool in tools:
            self.register_or_replace(tool)

    def disable(self, name: str, reason: str) -> None:
        self._disabled[name] = reason
        logger.warning("Tool '%s' disabled: %s", name, reason)

    def enable(self, name: str) -> None:
        self._disabled.pop(name, None)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def is_available(self, name: str, policy: Optional[AuthorizationPolicy] = None) -> bool:
        tool = self._tools.get(name)
        if tool is None or name in self._disabled:
            return False
        if policy is None:
            return True
        permitted, _ = policy.permits(tool)
        return permitted

    def list_tools(self, policy: Optional[AuthorizationPolicy] = None) -> List[Dict[str, Any]]:
        descriptors = []
        for name, tool in self._tools.items():
            descriptor = tool.describe()
            descriptor["enabled"] = name not in self._disabled
            if name in self._disabled:
                descriptor["disabled_reason"] = self._disabled[name]
            if policy is not None:
                permitted, reason = policy.permits(tool)
                descriptor["authorized"] = permitted
                if not permitted:
                    descriptor["authorization_reason"] = reason
            metrics = self._metrics.get(name, {})
            total = metrics.get("calls", 0)
            descriptor["metrics"] = {
                "total_calls": int(total),
                "successes": int(metrics.get("success", 0)),
                "failures": int(metrics.get("failure", 0)),
                "success_rate": round(metrics["success"] / total, 4) if total else None,
                "avg_duration_ms": round(metrics["total_ms"] / total, 2) if total else None,
            }
            descriptors.append(descriptor)
        return descriptors

    def catalog_for_llm(self, policy: Optional[AuthorizationPolicy] = None) -> List[Dict[str, Any]]:
        """Filtered tool catalog. Unavailable tools are omitted entirely so the
        LLM cannot waste turns selecting something it is not allowed to run."""
        catalog = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if policy is not None:
                permitted, _ = policy.permits(tool)
                if not permitted:
                    continue
            catalog.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "required_permissions": list(tool.required_permissions),
                    "side_effect": tool.side_effect.value,
                }
            )
        return catalog

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------
    async def invoke(
        self,
        name: str,
        params: Optional[Dict[str, Any]] = None,
        policy: Optional[AuthorizationPolicy] = None,
        task_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        remaining_budget: Optional[float] = None,
    ) -> ToolResult:
        """Execute a registered tool with full validation, authorization, and timing.

        `timeout_seconds` is the orchestrator's justified request for a longer window on a
        slow operation. It is a REQUEST, not a setting: `resolve_timeout` clamps it to the
        tool class's ceiling and the hard ceiling, so neither the model nor a caller can
        turn this into an unbounded wait. It is a parameter of the dispatcher and not of
        the tool arguments, which is what keeps it out of the schema the model can fill in.
        """
        started = time.perf_counter()

        tool = self._tools.get(name)
        if tool is None:
            logger.warning("TOOL unknown name=%s task=%s", name, task_id)
            return ToolResult.failure(
                f"No tool named '{name}' is registered.",
                category="UNSUPPORTED_OPERATION",
                retryable=False,
            )

        if name in self._disabled:
            logger.warning(
                "TOOL disabled name=%s task=%s reason=%s", name, task_id, self._disabled[name]
            )
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE,
                error=f"Tool '{name}' is unavailable: {self._disabled[name]}",
                error_category="TOOL_UNAVAILABLE",
                retryable=False,
            )

        # Fall back to the registry's configured policy, not the deny-all default.
        # The agent loop passes policy=None; without this the operator's startup
        # policy would never be consulted and every invocation would be refused.
        effective_policy = policy or self.policy
        permitted, reason = effective_policy.permits(tool)
        if not permitted:
            logger.warning("Tool '%s' refused by authorization policy: %s", name, reason)
            return ToolResult(
                status=ToolResultStatus.NOT_AUTHORIZED,
                error=reason,
                error_category="MISSING_PERMISSION",
                retryable=False,
            )

        try:
            validated = tool.validate_params(params or {})
        except ToolValidationError as exc:
            logger.warning(
                "TOOL invalid_input name=%s task=%s params=%s error=%s",
                name, task_id, sorted((params or {}).keys()), str(exc)[:300],
            )
            return ToolResult.failure(str(exc), category="INVALID_INPUT", retryable=False)

        # Network / wallet allowlists apply to the arguments, not just the tool.
        arguments_ok, arguments_reason = effective_policy.permits_arguments(tool, validated)
        if not arguments_ok:
            logger.warning("Tool '%s' refused by authorization policy: %s", name, arguments_reason)
            return ToolResult(
                status=ToolResultStatus.NOT_AUTHORIZED,
                error=arguments_reason,
                error_category="NOT_AUTHORIZED",
                retryable=False,
            )

        # Deterministic pre-flight guard for irreversible operations.
        try:
            if tool.side_effect in (SideEffect.WALLET_SIGNING, SideEffect.TRANSACTION_SUBMISSION):
                # A submission tool is gated on BOTH policies: a stop that halts
                # only submissions must not be bypassable through a tool whose
                # side effect is TRANSACTION_SUBMISSION.
                blocked = not emergency_stop.can_sign()
                if tool.side_effect is SideEffect.TRANSACTION_SUBMISSION:
                    blocked = blocked or not emergency_stop.can_submit_transactions()
                if blocked:
                    from backend.app.security.emergency_stop import EmergencyStopError

                    raise EmergencyStopError(
                        f"{tool.name}:{task_id}",
                        "Emergency stop is active; signing and submission are blocked.",
                    )
            elif tool.side_effect in (SideEffect.WEB_INTERACTION, SideEffect.STATE_MUTATION):
                # STATE_MUTATION tools act outside the process too (e.g. send_notification posts to
                # Telegram), so they stop with task execution. Pure reads (NONE, EXTERNAL_READ) stay
                # available for diagnostics.
                if not emergency_stop.can_execute_tasks():
                    from backend.app.security.emergency_stop import EmergencyStopError

                    raise EmergencyStopError(
                        f"{tool.name}:{task_id}",
                        "Emergency stop is active; web interaction and state changes are blocked.",
                    )
        except Exception as exc:  # EmergencyStopError and any guard failure
            logger.warning("TOOL blocked name=%s task=%s reason=%s", name, task_id, str(exc)[:300])
            return ToolResult(
                status=ToolResultStatus.NOT_AUTHORIZED,
                error=str(exc),
                error_category="EMERGENCY_STOP",
                retryable=False,
            )

        current = self._inflight.get(name, 0)
        self._inflight[name] = current + 1
        try:
            # Adaptive, bounded, and always the backend's decision: the tool's declared
            # default adjusted by class policy and observed latency, then capped by the
            # caller's remaining window budget. The number actually enforced is this one,
            # not `tool.timeout_seconds`.
            #
            # Computed INSIDE the try so that a raise here still releases `_inflight` in
            # the finally below. Outside the try, a policy failure left the in-flight
            # counter permanently incremented for the life of the process.
            effective_timeout, why = resolve_timeout(
                tool, requested=timeout_seconds, remaining_budget=remaining_budget
            )
            # Publish the task id BEFORE wait_for: it wraps the coroutine in a task that
            # copies the current context, so tools see the task they are running for.
            with task_context(task_id):
                result = await asyncio.wait_for(
                    tool.execute(validated), timeout=effective_timeout
                )
        except asyncio.TimeoutError:
            # A timeout does NOT prove the external operation failed, and must never be
            # reported as though it had. Stated explicitly in the result so the caller
            # inspects the actual state of the far side before retrying something whose
            # outcome is unknown.
            logger.warning(
                "TOOL timeout name=%s task=%s limit_s=%s reason=%s",
                name, task_id, effective_timeout, why,
            )
            result = ToolResult.failure(
                f"Tool '{name}' exceeded its {effective_timeout:g}s timeout ({why}). The "
                f"outcome of this operation is UNKNOWN: it may still have completed on the "
                f"other side. Inspect the actual state before retrying.",
                category="TIMEOUT",
                retryable=True,
            )
        except Exception as exc:  # Tools should not raise; if they do, contain it.
            logger.exception(
                "TOOL crashed name=%s task=%s type=%s", name, task_id, type(exc).__name__
            )
            result = ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                category="TOOL_FAILURE",
                retryable=False,
            )
        finally:
            self._inflight[name] = max(0, self._inflight.get(name, 1) - 1)
            # Observed for the NEXT invocation of this tool. Timeouts are recorded too:
            # they are real information about how long the tool takes, and excluding them
            # would bias every slow tool's estimate downwards.
            latency_history.observe(name, time.perf_counter() - started)

        duration_ms = int((time.perf_counter() - started) * 1000)
        result.duration_ms = duration_ms

        metrics = self._metrics.setdefault(
            name, {"success": 0, "failure": 0, "total_ms": 0.0, "calls": 0}
        )
        metrics["calls"] += 1
        metrics["total_ms"] += duration_ms
        if result.status is ToolResultStatus.SUCCESS:
            metrics["success"] += 1
        else:
            metrics["failure"] += 1

        # INFO on success, WARNING otherwise, so a failing tool is visible in the live log
        # without raising the log level. Only the outcome is logged, never parameters.
        (logger.info if result.status is ToolResultStatus.SUCCESS else logger.warning)(
            "TOOL done name=%s task=%s status=%s category=%s ms=%s",
            name, task_id, result.status.value, result.error_category, duration_ms,
        )
        return result

    def health_snapshot(self) -> List[Dict[str, Any]]:
        rows = []
        for name in self._tools:
            metrics = self._metrics.get(name, {})
            total = metrics.get("calls", 0)
            failures = metrics.get("failure", 0)
            rows.append(
                {
                    "tool": name,
                    "enabled": name not in self._disabled,
                    "total_calls": int(total),
                    "success_rate": round(metrics["success"] / total, 4) if total else None,
                    "failure_rate": round(failures / total, 4) if total else None,
                    "avg_duration_ms": round(metrics["total_ms"] / total, 2) if total else None,
                }
            )
        return rows


registry = ToolRegistry()
policy = DEFAULT_POLICY
