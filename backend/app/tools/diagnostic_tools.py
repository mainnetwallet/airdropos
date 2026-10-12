"""Diagnostic tools: what the platform's own internals are actually doing.

Every one of these reports measured state. None of them invent a "healthy" result:
the RPC health tool returns whatever each endpoint answered, the watchdog tool runs
the real probe sweep, and the tool-health tool reads the registry's own counters.
When a subsystem is broken, that is what the agent is told, so it can diagnose and
report the real fault instead of asking the operator to guess.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.database.session import session_scope
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)


class SystemDiagnosticsTool(BaseTool):
    name = "system_diagnostics"
    version = "1.0.0"
    description = (
        "Run the real diagnostics sweep and report each subsystem's measured status "
        "(database, LLM, browser, Telegram, tools, emergency stop and every RPC endpoint). "
        "Use this to answer 'why did this fail' or 'what is broken'."
    )
    required_permissions = ["system.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 60
    input_schema = {
        "type": "object",
        "properties": {
            "only_failing": {"type": "boolean", "description": "Return just the unhealthy subsystems"},
            "notify": {"type": "boolean", "description": "Send operator notifications for anomalies"},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.diagnostics.watchdog import watchdog

        async with session_scope() as db:
            report = await watchdog.sweep(db, notify=bool(params.get("notify", False)))

        payload = report.to_dict()
        if params.get("only_failing"):
            payload["subsystems"] = {
                name: value
                for name, value in payload["subsystems"].items()
                if value["status"] not in ("HEALTHY", "ok")
            }

        return ToolResult.success(
            data=payload,
            evidence={
                "authoritative": True,
                "kind": "diagnostic_sweep",
                "overall": report.overall,
                "checked_at": report.started_at.isoformat(),
            },
        )


class ListRpcHealthTool(BaseTool):
    name = "check_rpc_networks"
    version = "1.0.0"
    description = (
        "Check every supported network's RPC endpoint and report the real result, including "
        "which setting is missing when a network is not configured and why a configured one fails."
    )
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    input_schema = {
        "type": "object",
        "properties": {
            "networks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Networks to probe. Defaults to every supported network.",
            }
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.blockchain.chains import CHAIN_CATALOG, ChainRegistry

        wanted = {str(n).strip().lower() for n in (params.get("networks") or [])}
        chains = [c for c in CHAIN_CATALOG if not wanted or c.name in wanted]
        if wanted:
            unknown = sorted(wanted - {c.name for c in CHAIN_CATALOG})
            if unknown:
                return ToolResult.failure(
                    f"Unknown network(s): {', '.join(unknown)}.", category="UNKNOWN_NETWORK"
                )

        probe = ChainRegistry()
        try:
            sweep = await probe.health_sweep()
        finally:
            await probe.aclose()

        rows: List[Dict[str, Any]] = []
        for chain in chains:
            result = sweep.get(chain.name) or {"status": "UNAVAILABLE", "detail": "not probed"}
            # The setting name is operator-facing and safe; the endpoint URL is not,
            # because it normally carries the provider API key.
            rows.append(
                {
                    "network": chain.name,
                    "chain_id": chain.chain_id,
                    "native_symbol": chain.native_symbol,
                    "rpc_setting": chain.rpc_setting,
                    "status": result.get("status"),
                    "block": result.get("block"),
                    "detail": result.get("detail"),
                    "category": result.get("category"),
                }
            )

        configured = [r for r in rows if r["status"] not in ("NOT_CONFIGURED",)]
        healthy = [r for r in rows if r["status"] == "HEALTHY"]
        return ToolResult.success(
            data={
                "networks": rows,
                "configured": len(configured),
                "healthy": len(healthy),
                "total": len(rows),
                "note": (
                    "A network with no configured endpoint is reported as NOT_CONFIGURED, which is "
                    "a configuration gap and not an outage. NO_RPC responses mean this platform "
                    "cannot reach that chain until an endpoint is set."
                ),
            },
            evidence={
                "authoritative": True,
                "kind": "diagnostic_sweep",
                "healthy": len(healthy),
                "total": len(rows),
            },
        )


class ListToolHealthTool(BaseTool):
    name = "list_tool_health"
    version = "1.0.0"
    description = (
        "List every registered tool with whether it is enabled, whether the current policy "
        "authorizes it, and why a denial happened. Use this to explain a refusal."
    )
    required_permissions = ["tool.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "only_unavailable": {"type": "boolean"},
            "tool_name": {"type": "string", "maxLength": 64},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.tools.registry import registry

        descriptors = registry.list_tools(policy=registry.policy)
        if params.get("tool_name"):
            descriptors = [d for d in descriptors if d["name"] == params["tool_name"]]
        if params.get("only_unavailable"):
            descriptors = [
                d
                for d in descriptors
                if not d.get("enabled") or not d.get("authorized", True)
            ]

        return ToolResult.success(
            data={
                "tools": descriptors,
                "count": len(descriptors),
                "policy": registry.policy.describe(),
            },
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "tool",
                "count": len(descriptors),
            },
        )


class ListFailureRecordsTool(BaseTool):
    name = "list_task_failures"
    version = "1.0.0"
    description = (
        "List recent task failures with their error category, message, retry count and "
        "recovery strategy. Use this to diagnose why a task failed."
    )
    required_permissions = ["task.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "maxLength": 40},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from sqlalchemy import select

        from backend.app.models import FailureRecord

        limit = int(params.get("limit") or 25)
        async with session_scope() as db:
            query = select(FailureRecord).order_by(FailureRecord.created_at.desc()).limit(limit)
            if params.get("task_id"):
                from backend.app.models import Task

                task = await db.scalar(select(Task).where(Task.task_id == str(params["task_id"]).upper()))
                if task is None:
                    return ToolResult.needs_user(f"No task with id '{params['task_id']}'.")
                query = query.where(FailureRecord.task_id == task.id)
            rows = list((await db.execute(query)).scalars().all())
            items = [
                {
                    "task_id": str(r.task_id) if r.task_id else None,
                    "error_category": r.error_category,
                    "error_message": (r.error_message or "")[:1000],
                    "is_retryable": r.is_retryable,
                    "retry_count": r.retry_count,
                    "recovery_strategy": r.recovery_strategy,
                    "final_outcome": r.final_outcome,
                    "at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

        return ToolResult.success(
            data={"failures": items, "count": len(items)},
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "failure",
                "count": len(items),
            },
        )


class WorkerStatusTool(BaseTool):
    name = "worker_status"
    version = "1.0.0"
    description = (
        "Report whether background workers are alive and recent, and the emergency-stop state. "
        "Reads the shared worker heartbeat, so a dead worker shows as dead."
    )
    required_permissions = ["system.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from sqlalchemy import select

        from backend.app.models import IntegrationHealth
        from backend.app.scheduler.worker_status import STALE_AFTER_SECONDS, WORKER_SUBSYSTEM
        from backend.app.security.emergency_stop import emergency_stop

        async with session_scope() as db:
            row = await db.scalar(
                select(IntegrationHealth).where(IntegrationHealth.subsystem == WORKER_SUBSYSTEM)
            )

        if row is None:
            worker: Dict[str, Any] = {
                "alive": False,
                "reason": "No worker has ever published a heartbeat.",
            }
        else:
            age = _heartbeat_age(row)
            alive = age <= STALE_AFTER_SECONDS
            worker = {
                "alive": alive,
                "heartbeat_age_seconds": None if age == float("inf") else round(age, 1),
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "note": (row.detail or {}).get("note"),
                "current_task": (row.detail or {}).get("current_task"),
                "last_task": (row.detail or {}).get("last_task"),
                "last_error": (row.detail or {}).get("last_error"),
                "tool_count": (row.detail or {}).get("tool_count"),
                "claimed": (row.detail or {}).get("claimed"),
                "finished": (row.detail or {}).get("finished"),
            }

        return ToolResult.success(
            data={
                "worker": worker,
                "emergency_stop": emergency_stop.status(),
                "can_execute_tasks": emergency_stop.can_execute_tasks(),
                "can_sign": emergency_stop.can_sign(),
                "can_submit_transactions": emergency_stop.can_submit_transactions(),
            },
            evidence={"authoritative": True, "kind": "diagnostic_read", "source": "worker_heartbeat"},
        )


def _heartbeat_age(row) -> float:
    """Seconds since the worker last reported. Infinite when it never did."""
    from datetime import datetime, timezone

    stamp = row.last_checked_at
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def register_diagnostic_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            SystemDiagnosticsTool(),
            ListRpcHealthTool(),
            ListToolHealthTool(),
            ListFailureRecordsTool(),
            WorkerStatusTool(),
        ]
    )