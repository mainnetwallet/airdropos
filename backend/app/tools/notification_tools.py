"""Notification & Reporting Tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from backend.app.notifications.dispatcher import Channel, Severity, dispatcher
from backend.app.tools.base import BaseTool, SideEffect, ToolResult


class SendNotificationTool(BaseTool):
    name = "send_notification"
    version = "1.0.0"
    description = "Send an important notification to the operator via Telegram and the dashboard."
    required_permissions = ["notify.send"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "body": {"type": "string", "maxLength": 3000},
            "severity": {"type": "string", "enum": ["info", "success", "warning", "error", "critical"]},
            "task_id": {"type": "string", "maxLength": 64},
        },
        "required": ["title", "body"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        record = await dispatcher.notify(
            title=params["title"],
            body=params["body"],
            severity=Severity(params.get("severity", "info")),
            task_id=params.get("task_id"),
        )
        return ToolResult.success(
            data=record,
            evidence={"authoritative": record.get("results", {}).get("telegram", {}).get("status") == "ok"},
        )


class GenerateReportTool(BaseTool):
    name = "generate_report"
    version = "1.0.0"
    description = "Render an evidence-based summary report from structured findings. Never fabricates figures."
    required_permissions = ["report.generate"]
    side_effect = SideEffect.NONE
    timeout_seconds = 15
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "sections": {
                "type": "array",
                "description": "Named sections of the report",
                "items": {"type": "object"},
            },
        },
        "required": ["title", "sections"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        lines = [f"# {params['title']}", f"_Generated {datetime.now(timezone.utc).isoformat()}_", ""]
        for section in params.get("sections") or []:
            heading = section.get("heading", "Section")
            body = section.get("body", "")
            lines.append(f"## {heading}")
            lines.append(str(body))
            lines.append("")

        markdown = "\n".join(lines)
        return ToolResult.success(
            data={
                "report_markdown": markdown,
                "section_count": len(params.get("sections") or []),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": "Figures are reproduced verbatim from the supplied sections. None were inferred.",
            }
        )


def register_notification_tools(registry) -> None:
    registry.register_or_replace_many([SendNotificationTool(), GenerateReportTool()])
