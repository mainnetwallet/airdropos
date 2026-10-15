"""Task tools: what the operator's work actually is, and how to steer it.

These wrap the existing `TaskManager` and the task state machine rather than
re-implementing lifecycle rules. In particular:

* Resuming a task goes through the validated `TaskStatus` transition, so an
  illegal jump is refused by the same code that refuses it everywhere else.
* Cancel is not the same as "stop right now": it only applies to a task that is not
  mid-execution, because cancelling a running task while the worker holds its lease
  would leave the lease behind.
* "Continue my previous task" resolves a real task id from real database state,
  never from conversation text.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.app.database.session import session_scope
from backend.app.models import Task
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)

# Statuses a worker will claim, and which of them the state machine allows to go
# back to READY. FAILED is deliberately absent: VALID_TRANSITIONS has no
# FAILED -> READY edge (a failed task is retried via RETRYING or replanned via
# PLANNING), so "resume" has to route through the edge that actually exists.
RESUMABLE_FROM = {
    TaskStatus.PARTIALLY_COMPLETED.value,
    TaskStatus.WAITING.value,
    TaskStatus.NEEDS_USER_ACTION.value,
    TaskStatus.RETRYING.value,
    TaskStatus.CREATED.value,
}
ALREADY_QUEUED = {TaskStatus.READY.value}

TERMINAL_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.UNVERIFIED.value,
}


def _summary(task: Task) -> Dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "progress": task.progress,
        "network": task.network,
        "category": task.category,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "next_planned_action": task.next_planned_action,
        "error_details": task.error_details or {},
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


class ListTasksTool(BaseTool):
    name = "list_tasks"
    version = "1.0.0"
    description = "List tasks with their real status, so the agent can answer 'what is running'."
    required_permissions = ["task.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "maxLength": 40, "description": "Filter by exact status"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        limit = int(params.get("limit") or 25)
        async with session_scope() as db:
            query = select(Task).order_by(Task.updated_at.desc()).limit(limit)
            if params.get("status"):
                query = query.where(Task.status == str(params["status"]).upper())
            rows = list((await db.execute(query)).scalars().all())
            tasks = [_summary(t) for t in rows]

        return ToolResult.success(
            data={"tasks": tasks, "count": len(tasks)},
            evidence={"authoritative": True, "kind": "record_read", "entity": "task", "count": len(tasks)},
        )


class GetTaskTool(BaseTool):
    name = "get_task"
    version = "1.0.0"
    description = (
        "Full status report for one task: executions, checkpoints, evidence count, "
        "blocker and transaction history. Read from the database, never from chat history."
    )
    required_permissions = ["task.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 25
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "maxLength": 40}},
        "required": ["task_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        task_id = str(params["task_id"]).strip().upper()
        async with session_scope() as db:
            report = await TaskManager(db).get_status_report(task_id)

        if report is None:
            return ToolResult.failure(
                f"No task with id '{task_id}'. Use list_tasks to see the real ids.",
                category="TASK_NOT_FOUND",
            )
        return ToolResult.success(
            data=report,
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "task",
                "task_id": task_id,
                "status": report.get("status"),
            },
        )


class CreateTaskTool(BaseTool):
    name = "create_agent_task"
    version = "1.0.0"
    description = (
        "Create a persistent task the background worker will execute, and return its "
        "real AIR-YYYY-MM-DD-NNNNNN id. Use this for any request that takes more than one reply."
    )
    required_permissions = ["task.write"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 30
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "description": {"type": "string", "maxLength": 6000},
            "category": {"type": "string", "maxLength": 100},
            "network": {"type": "string", "maxLength": 50},
            "priority": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["title"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        title = str(params["title"]).strip()
        if not title:
            return ToolResult.failure("A task title is required.", category="INVALID_INPUT")

        async with session_scope() as db:
            manager = TaskManager(db)
            duplicates = await manager.find_duplicates(None, title)
            if duplicates:
                # Duplicates are reported, never silently merged: the operator may
                # genuinely want two runs of the same thing on two wallets.
                existing = _summary(duplicates[0])
                return ToolResult.success(
                    data={
                        "task_id": existing["task_id"],
                        "status": existing["status"],
                        "title": existing["title"],
                        "created": False,
                        "note": (
                            f"A similar task already exists ({existing['task_id']}, "
                            f"{existing['status']}). Nothing new was created."
                        ),
                    },
                    evidence={
                        "authoritative": True,
                        "kind": "record_read",
                        "entity": "task",
                        "task_id": existing["task_id"],
                        "created": False,
                    },
                )

            priority = int(params.get("priority") or 5)
            task = await manager.create_task(
                title=title,
                description=str(params.get("description") or ""),
                category=params.get("category"),
                network=params.get("network"),
                priority=max(1, min(10, priority)),
            )
            await manager.transition(
                task, TaskStatus.READY, reason="Created by the agent; queued for the worker"
            )
            result = _summary(task)

        return ToolResult.success(
            data={**result, "created": True},
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "task",
                "task_id": result["task_id"],
                "created": True,
            },
        )


class ResumeTaskTool(BaseTool):
    name = "resume_task"
    version = "1.0.0"
    description = (
        "Resume an eligible task ('continue my previous task'). The task returns to "
        "READY and the worker picks it up from its last checkpoint. Refuses completed "
        "and cancelled tasks rather than silently starting them again."
    )
    required_permissions = ["task.write"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 25
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "maxLength": 40}},
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            manager = TaskManager(db)
            task, reason = await _resolve_task(db, params.get("task_id"))
            if task is None:
                return ToolResult.needs_user(reason or "Which task should be resumed?")

            if task.status == TaskStatus.COMPLETED.value:
                return ToolResult.needs_user(
                    f"{task.task_id} is already COMPLETED. There is nothing to resume; "
                    f"ask for new work instead."
                )
            if task.status == TaskStatus.CANCELLED.value:
                return ToolResult.needs_user(
                    f"{task.task_id} was CANCELLED and is not restarted by resuming. "
                    f"Create a new task if the work is still wanted."
                )
            if task.status == TaskStatus.IN_PROGRESS.value:
                return ToolResult.success(
                    data={
                        "task_id": task.task_id,
                        "status": task.status,
                        "resumed": False,
                        "note": "Already running. No action was taken.",
                    },
                    evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                              "task_id": task.task_id, "resumed": False},
                )
            if task.status in ALREADY_QUEUED:
                return ToolResult.success(
                    data={
                        "task_id": task.task_id,
                        "status": task.status,
                        "resumed": False,
                        "note": "Already queued for the worker. No action was taken.",
                    },
                    evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                              "task_id": task.task_id, "resumed": False},
                )
            if task.status == TaskStatus.FAILED.value:
                # VALID_TRANSITIONS allows FAILED -> RETRYING, not FAILED -> READY.
                # Routing through RETRYING is what the existing recovery code does,
                # and it also schedules the backoff the retry policy decided on.
                await manager.transition(
                    task,
                    TaskStatus.RETRYING,
                    reason="Resumed by the operator through the agent",
                )
                result = _summary(task)
                return ToolResult.success(
                    data={
                        **result,
                        "resumed": True,
                        "note": "Retrying; the worker will pick it up again.",
                    },
                    evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                              "task_id": result["task_id"], "resumed": True},
                )
            if task.status not in RESUMABLE_FROM:
                return ToolResult.failure(
                    f"{task.task_id} is {task.status} and cannot be resumed.",
                    category="INVALID_TRANSITION",
                )

            try:
                await manager.transition(
                    task,
                    TaskStatus.READY,
                    reason="Resumed by the operator through the agent",
                )
            except Exception as exc:
                return ToolResult.failure(
                    f"Could not resume {task.task_id}: {exc}", category="INVALID_TRANSITION"
                )
            result = _summary(task)

        return ToolResult.success(
            data={
                **result,
                "resumed": True,
                "note": "Queued for the worker, which will continue from the last checkpoint.",
            },
            evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                      "task_id": result["task_id"], "resumed": True},
        )


class CancelTaskTool(BaseTool):
    name = "cancel_task"
    version = "1.0.0"
    description = (
        "Cancel a task that is queued, blocked or partially completed. A task the "
        "worker is actively running is not cancelled, because that would abandon the "
        "execution lease."
    )
    required_permissions = ["task.write"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 25
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "maxLength": 40}},
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            manager = TaskManager(db)
            task, reason = await _resolve_task(db, params.get("task_id"))
            if task is None:
                return ToolResult.needs_user(reason or "Which task should be stopped?")

            if task.status == TaskStatus.IN_PROGRESS.value:
                return ToolResult.needs_user(
                    f"{task.task_id} is being executed right now. Engage the emergency stop "
                    f"or wait for the current step to finish; cancelling mid-run would "
                    f"strand its lease."
                )
            if task.status == TaskStatus.CANCELLED.value:
                return ToolResult.success(
                    data={"task_id": task.task_id, "status": task.status, "cancelled": False,
                          "note": "Already cancelled."},
                    evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                              "task_id": task.task_id, "cancelled": False},
                )

            try:
                await manager.transition(
                    task, TaskStatus.CANCELLED, reason="Cancelled by the operator through the agent"
                )
            except Exception as exc:
                return ToolResult.failure(
                    f"Could not cancel {task.task_id}: {exc}", category="INVALID_TRANSITION"
                )
            result = _summary(task)

        return ToolResult.success(
            data={**result, "cancelled": True},
            evidence={"authoritative": True, "kind": "record_read", "entity": "task",
                      "task_id": result["task_id"], "cancelled": True},
        )


async def _resolve_task(db, task_id: Optional[str]) -> tuple[Optional[Task], Optional[str]]:
    """Find the task the operator means.

    With no id, the most recently updated non-terminal task is taken, which is what
    "the previous task" means in a conversation. When that is still ambiguous the
    caller is asked rather than told.
    """
    manager = TaskManager(db)
    if task_id:
        task = await manager.get_by_task_id(str(task_id).strip())
        if task is None:
            return None, f"No task with id '{task_id}'."
        return task, None

    rows: List[Task] = list(
        (
            await db.execute(
                select(Task)
                .where(Task.status.notin_(sorted(TERMINAL_STATUSES)))
                .order_by(Task.updated_at.desc())
                .limit(2)
            )
        ).scalars().all()
    )
    if not rows:
        return None, "There is no unfinished task to act on."
    if len(rows) > 1:
        options = ", ".join(f"{r.task_id} ({r.status}: {r.title})" for r in rows)
        return None, f"Several unfinished tasks exist. Which one? {options}"
    return rows[0], None


def register_task_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            ListTasksTool(),
            GetTaskTool(),
            CreateTaskTool(),
            ResumeTaskTool(),
            CancelTaskTool(),
        ]
    )