"""Task Manager: persistent CRUD, state transitions, and atomic claiming.

The database is the source of truth. Conversation history is never consulted to
infer task progress (Specification Part 7.4).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Task, TaskEvent, TaskExecution
from backend.app.tasks.id_generator import format_task_id, next_sequence, parse_task_id
from backend.app.tasks.state_machine import (
    TaskStatus,
    assert_completion_verified,
    assert_transition,
    progress_for_status,
)

logger = logging.getLogger(__name__)

SEVERITY_BY_STATUS = {
    TaskStatus.COMPLETED: "INFO",
    TaskStatus.FAILED: "ERROR",
    TaskStatus.NEEDS_USER_ACTION: "WARN",
    TaskStatus.UNVERIFIED: "WARN",
    TaskStatus.CANCELLED: "WARN",
    TaskStatus.RETRYING: "WARN",
}


class TaskManager:
    """All task persistence operations flow through here."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def create_task(
        self,
        title: str,
        description: str = "",
        *,
        goal_id: Optional[str] = None,
        project_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        priority: int = 5,
        status: TaskStatus = TaskStatus.CREATED,
        dependencies: Optional[List[str]] = None,
        planned_actions: Optional[List[Dict[str, Any]]] = None,
        estimated_cost_usd: float = 0.0,
        deadline_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Task:
        """Allocate a collision-free AIR-YYYY-NNNNNN ID and persist a new task."""
        if priority < 1 or priority > 10:
            raise ValueError("priority must be between 1 (highest) and 10 (lowest)")

        year = datetime.now(timezone.utc).year
        max_seq = await self.db.scalar(
            select(func.max(Task.id)).where(Task.task_id.like(f"AIR-{year}-%"))
        )
        # Max sequence parsed from existing IDs of this year.
        existing = await self.db.execute(
            select(Task.task_id).where(Task.task_id.like(f"AIR-{year}-%"))
        )
        highest = 0
        for (tid,) in existing.all():
            try:
                _, seq = parse_task_id(tid)
                highest = max(highest, seq)
            except ValueError:
                continue

        async def is_taken(candidate: str) -> bool:
            found = await self.db.scalar(select(Task.id).where(Task.task_id == candidate))
            return found is not None

        sequence = await next_sequence(max_sequence=highest, year=year, is_taken=is_taken)
        task_id = format_task_id(sequence, year)

        task = Task(
            task_id=task_id,
            title=title,
            description=description,
            category=category,
            status=status.value,
            priority=priority,
            goal_id=goal_id,
            project_id=project_id,
            parent_task_id=parent_task_id,
            network=network,
            dependencies=dependencies or [],
            planned_actions=planned_actions or [],
            progress=progress_for_status(status),
            estimated_cost_usd=estimated_cost_usd,
            deadline_at=deadline_at,
            max_retries=max_retries,
            next_planned_action=planned_actions[0].get("description") if planned_actions else None,
        )
        self.db.add(task)
        await self.db.flush()
        await self._record_event(task, "TASK_CREATED", "INFO", {"title": title, "priority": priority})
        logger.info("Created task %s: %s", task_id, title)
        return task

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    async def transition(
        self,
        task: Task,
        new_status: TaskStatus,
        *,
        reason: str = "",
        verified: bool = False,
        evidence_count: int = 0,
        progress: Optional[float] = None,
        error_details: Optional[Dict[str, Any]] = None,
        next_planned_action: Optional[str] = None,
    ) -> Task:
        """Apply a validated state transition and log the event."""
        current = TaskStatus(task.status)
        assert_transition(current, new_status)

        if new_status is TaskStatus.COMPLETED:
            # Enforces that completion was verified against evidence.
            assert_completion_verified(new_status, verified, evidence_count)
            task.completed_at = datetime.now(timezone.utc)
            task.last_verification_at = datetime.now(timezone.utc)
            task.progress = 1.0
        elif new_status is TaskStatus.FAILED:
            task.error_details = error_details or task.error_details
            if task.progress >= 1.0:
                task.progress = 0.9
        elif new_status is TaskStatus.IN_PROGRESS:
            if task.execution_started_at is None:
                task.execution_started_at = datetime.now(timezone.utc)
        elif new_status is TaskStatus.RETRYING:
            task.retry_count += 1
        elif new_status is TaskStatus.NEEDS_USER_ACTION:
            task.next_execution_at = None  # Parked until the human intervenes.

        task.status = new_status.value
        if progress is not None:
            task.progress = max(0.0, min(1.0, progress))
        elif new_status not in (TaskStatus.COMPLETED,):
            task.progress = progress_for_status(new_status)

        if next_planned_action is not None:
            task.next_planned_action = next_planned_action
        if error_details is not None:
            task.error_details = error_details

        await self.db.flush()
        await self._record_event(
            task,
            "STATUS_TRANSITION",
            SEVERITY_BY_STATUS.get(new_status, "INFO"),
            {"from": current.value, "to": new_status.value, "reason": reason, "verified": verified},
        )
        return task

    # ------------------------------------------------------------------
    # Progress recording
    # ------------------------------------------------------------------
    async def record_action(
        self,
        task: Task,
        action: Dict[str, Any],
        outcome: str,
        *,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append to completed_actions or failed_actions for the audit trail."""
        entry = {**action, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(), "result": result or {}}
        if outcome == "completed":
            task.completed_actions = [*(task.completed_actions or []), entry]
            planned = task.planned_actions or []
            task.planned_actions = [p for p in planned if p.get("id") != action.get("id")]
        elif outcome == "failed":
            task.failed_actions = [*(task.failed_actions or []), entry]
        await self.db.flush()

    async def save_execution(
        self,
        task: Task,
        *,
        iteration: int,
        plan: Dict[str, Any],
        observation: Optional[Dict[str, Any]] = None,
        reasoning: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        status: str = "SUCCESS",
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> TaskExecution:
        execution = TaskExecution(
            task_id=task.id,
            iteration=iteration,
            plan=plan,
            observation=observation or {},
            reasoning=reasoning,
            result=result or {},
            status=status,
            duration_ms=duration_ms,
            error_message=error_message,
        )
        self.db.add(execution)
        await self.db.flush()
        return execution

    # ------------------------------------------------------------------
    # Claiming (concurrency safety)
    # ------------------------------------------------------------------
    async def claim_next_task(self, worker_id: str) -> Optional[Task]:
        """Atomically claim the highest-priority runnable task.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so two workers never claim the
        same task, and so a crashed worker's row is released by Postgres.
        """
        from backend.app.core.config import settings

        active_count = await self.db.scalar(
            select(func.count(Task.id)).where(Task.status.in_([s.value for s in (TaskStatus.IN_PROGRESS, TaskStatus.RETRYING)]))
        )
        if (active_count or 0) >= settings.MAX_CONCURRENT_TASKS:
            logger.debug("Concurrency limit reached (%s/%s)", active_count, settings.MAX_CONCURRENT_TASKS)
            return None

        stmt = (
            select(Task)
            .where(Task.status.in_([TaskStatus.READY.value, TaskStatus.RETRYING.value]))
            .where(Task.retry_count <= Task.max_retries)
            .where(
                (Task.next_execution_at.is_(None)) | (Task.next_execution_at <= func.now())
            )
            .order_by(Task.priority.asc(), Task.deadline_at.asc().nulls_last(), Task.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task = await self.db.scalar(stmt)
        if task is None:
            return None

        task.status = TaskStatus.IN_PROGRESS.value
        if task.execution_started_at is None:
            task.execution_started_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self._record_event(
            task, "TASK_CLAIMED", "INFO", {"worker_id": worker_id, "priority": task.priority}
        )
        logger.info("Worker %s claimed task %s", worker_id, task.task_id)
        return task

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    async def get_by_task_id(self, task_id: str) -> Optional[Task]:
        return await self.db.scalar(select(Task).where(Task.task_id == task_id.upper()))

    async def get_status_report(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Build the authoritative status report required by Part 7.4.

        Reads the real record, execution history, checkpoints, and evidence.
        Never infers progress from conversation history.
        """
        task = await self.get_by_task_id(task_id)
        if task is None:
            return None

        executions = (
            await self.db.execute(
                select(TaskExecution)
                .where(TaskExecution.task_id == task.id)
                .order_by(TaskExecution.iteration.desc())
                .limit(10)
            )
        ).scalars().all()

        events = (
            await self.db.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.created_at.desc())
                .limit(25)
            )
        ).scalars().all()

        evidence = (
            await self.db.execute(
                select(Task.evidence).where(Task.id == task.id)
            )
        ).scalars().first()

        pending = [
            a for a in (task.planned_actions or [])
            if not any(c.get("id") == a.get("id") for c in (task.completed_actions or []))
        ]

        return {
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "progress": task.progress,
            "network": task.network,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "dependencies": task.dependencies or [],
            "planned_actions": task.planned_actions or [],
            "pending_actions": pending,
            "completed_actions": task.completed_actions or [],
            "failed_actions": task.failed_actions or [],
            "checkpoints": len(task.checkpoints or []),
            "evidence_count": len(evidence or []),
            "error_details": task.error_details or {},
            "next_planned_action": task.next_planned_action,
            "deadline_at": task.deadline_at.isoformat() if task.deadline_at else None,
            "next_execution_at": task.next_execution_at.isoformat() if task.next_execution_at else None,
            "last_updated": task.updated_at.isoformat() if task.updated_at else None,
            "last_verification_at": task.last_verification_at.isoformat() if task.last_verification_at else None,
            "recent_executions": [
                {
                    "iteration": e.iteration,
                    "status": e.status,
                    "duration_ms": e.duration_ms,
                    "reasoning": (e.reasoning or "")[:500],
                    "error": e.error_message,
                    "at": e.created_at.isoformat(),
                }
                for e in executions
            ],
            "recent_events": [
                {"type": e.event_type, "severity": e.severity, "at": e.created_at.isoformat()}
                for e in events
            ],
            "transactions": [
                {
                    "hash": t.tx_hash,
                    "status": t.status,
                    "network": t.network,
                    "value_usd": t.value_usd,
                    "confirmations": t.confirmations,
                }
                for t in (task.transactions or [])
            ],
        }

    async def find_duplicates(self, goal_id: Optional[str], title_fragment: str) -> List[Task]:
        """Detect near-duplicate tasks so the agent does not repeat itself."""
        stmt = select(Task).where(Task.title.ilike(f"%{title_fragment}%"))
        if goal_id:
            stmt = stmt.where(Task.goal_id == goal_id)
        stmt = stmt.where(Task.status.notin_([TaskStatus.CANCELLED.value, TaskStatus.COMPLETED.value]))
        return list((await self.db.execute(stmt.limit(10))).scalars().all())

    # ------------------------------------------------------------------
    async def _record_event(
        self, task: Task, event_type: str, severity: str, payload: Dict[str, Any]
    ) -> None:
        self.db.add(
            TaskEvent(task_id=task.id, event_type=event_type, severity=severity, payload=payload)
        )
        await self.db.flush()
