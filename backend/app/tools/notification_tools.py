"""Notification & Reporting Tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from backend.app.notifications.dispatcher import Severity, dispatcher
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


_BODY_KEYS = ("body", "content", "text")


def _render_body(body: Any) -> str:
    """Render a section body as Markdown text. Lists become bullets, dicts become 'key: value' bullets."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        rows = [f"- {k}: {_render_inline(v)}" for k, v in body.items() if _render_inline(v)]
        return "\n".join(rows)
    if isinstance(body, (list, tuple)):
        rows = [f"- {_render_inline(v)}" for v in body if _render_inline(v)]
        return "\n".join(rows)
    return str(body).strip()


def _render_inline(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "" if value is None else str(value).strip()


def _normalise_section(index: int, section: Any):
    """Return (heading, body_markdown). A bare string is treated as the body."""
    if isinstance(section, str):
        return f"Section {index}", section.strip()
    if not isinstance(section, dict):
        return f"Section {index}", ""
    heading = str(section.get("heading") or section.get("title") or f"Section {index}").strip()
    for key in _BODY_KEYS:
        rendered = _render_body(section.get(key))
        if rendered:
            return heading, rendered
    return heading, ""


class GenerateReportTool(BaseTool):
    name = "generate_report"
    version = "1.1.0"
    description = (
        "Render an evidence-based summary report from structured findings. Never fabricates figures. "
        "Each section needs a heading and a non-empty body; a section with no body fails the report."
    )
    required_permissions = ["report.generate"]
    side_effect = SideEffect.NONE
    timeout_seconds = 15
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "sections": {
                "type": "array",
                "description": "Named sections of the report. Each: {heading, body}.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "maxLength": 200},
                        "body": {
                            "description": "Section text. A string, a list of strings (bullets) or an object (key: value bullets).",
                            "anyOf": [
                                {"type": "string"},
                                {"type": "array", "items": {}},
                                {"type": "object"},
                            ],
                        },
                    },
                    "required": ["heading", "body"],
                },
            },
        },
        "required": ["title", "sections"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        raw_sections = params.get("sections") or []
        if not raw_sections:
            return ToolResult.failure(
                "No sections supplied. Pass at least one {heading, body} section.",
                category="INVALID_INPUT",
            )

        generated_at = datetime.now(timezone.utc).isoformat()
        lines = [f"# {params['title']}", f"_Generated {generated_at}_", ""]
        rendered, empty = [], []
        for index, section in enumerate(raw_sections, start=1):
            heading, body = _normalise_section(index, section)
            if not body:
                empty.append(heading)
                continue
            rendered.append({"heading": heading, "body_chars": len(body)})
            lines += [f"## {heading}", body, ""]

        if empty:
            # Rendering an empty section and calling it success is exactly the defect this guards.
            return ToolResult.failure(
                "Report not generated: these sections have no body text: "
                + ", ".join(f"'{h}'" for h in empty)
                + ". Put the section text in 'body' (a string, a list of strings, or an object).",
                category="INVALID_INPUT",
            )

        return ToolResult.success(
            data={
                "report_markdown": "\n".join(lines),
                "section_count": len(rendered),
                "sections": rendered,
                "generated_at": generated_at,
                "note": "Figures are reproduced verbatim from the supplied sections. None were inferred.",
            }
        )


def register_notification_tools(registry) -> None:
    registry.register_or_replace_many([SendNotificationTool(), GenerateReportTool()])
