"""Task Manager: persistent CRUD, state transitions, and atomic claiming.

The database is the source of truth. Conversation history is never consulted to
infer task progress (Specification Part 7.4).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    BlockchainTransaction,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TaskEvidence,
    TaskExecution,
)
from backend.app.tasks.id_generator import format_task_id, next_sequence, parse_task_id
from backend.app.tasks.state_machine import (
    TaskStatus,
    assert_completion_verified,
    assert_transition,
    progress_for_status,
)

logger = logging.getLogger(__name__)

# TaskEvidence.source / uri_reference are String(512); descriptions are capped like create_agent_task.
EVIDENCE_URI_MAX = 512
DESCRIPTION_MAX = 6000
EVIDENCE_LIST_LIMIT = 50

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
    async def update_progress(
        self,
        task: Task,
        *,
        progress: Optional[float] = None,
        next_planned_action: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Task:
        """Record progress on a task that is already running, WITHOUT changing its status.

        A worker claims a task straight into IN_PROGRESS, and the state machine (rightly) forbids
        IN_PROGRESS -> IN_PROGRESS, so per-step bookkeeping must not go through ``transition``.
        This never alters ``task.status`` and is refused for tasks that are not IN_PROGRESS, so it
        cannot be used to sidestep transition validation.
        """
        current = TaskStatus(task.status)
        if current is not TaskStatus.IN_PROGRESS:
            raise ValueError(
                f"update_progress only applies to IN_PROGRESS tasks; {task.task_id} is {current.value}"
            )
        if progress is not None:
            task.progress = max(0.0, min(1.0, progress))
        if next_planned_action is not None:
            task.next_planned_action = next_planned_action
        await self.db.flush()
        if note:
            await self._record_event(
                task, "PROGRESS_UPDATE", "INFO", {"reason": note, "progress": task.progress}
            )
        return task

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
    # Evidence
    # ------------------------------------------------------------------
    async def record_evidence(
        self,
        task: Task,
        *,
        evidence_type: str,
        source: str,
        uri_reference: str,
        meta: Dict[str, Any],
    ) -> TaskEvidence:
        """Persist one evidence record linked to ``task`` (its internal UUID).

        ``meta`` (label, sha256, size, capture time, ...) is stored as JSON in ``content``
        together with the task's PUBLIC id, so a record stays auditable on its own. Raises
        ValueError rather than truncating a path: a clipped path would silently break the link
        between the record and the file it proves.
        """
        if not uri_reference:
            raise ValueError("uri_reference is required for evidence")
        if len(uri_reference) > EVIDENCE_URI_MAX:
            raise ValueError(f"Evidence path is {len(uri_reference)} characters; the limit is {EVIDENCE_URI_MAX}")
        if len(source) > EVIDENCE_URI_MAX:
            raise ValueError("Evidence source is too long")

        record = TaskEvidence(
            task_id=task.id,
            evidence_type=evidence_type,
            source=source,
            uri_reference=uri_reference,
            content=json.dumps({"task_public_id": task.task_id, **meta}, sort_keys=True),
        )
        self.db.add(record)
        await self.db.flush()
        await self._record_event(
            task,
            "EVIDENCE_CAPTURED",
            "INFO",
            {
                "reason": f"{evidence_type.title()} evidence recorded ({meta.get('label') or 'no label'})",
                "evidence_id": str(record.id),
                "sha256": meta.get("sha256"),
            },
        )
        return record

    @staticmethod
    def evidence_view(record: TaskEvidence, public_task_id: str) -> Dict[str, Any]:
        """Auditable, read-only view of one evidence record."""
        try:
            meta = json.loads(record.content) if record.content else {}
            if not isinstance(meta, dict):
                meta = {}
        except (TypeError, ValueError):
            meta = {}  # legacy / free-text content: still listed, just without parsed metadata
        return {
            "id": str(record.id),
            "task_id": public_task_id,
            "type": record.evidence_type,
            "source": record.source,
            "uri": record.uri_reference,
            "label": meta.get("label"),
            "sha256": meta.get("sha256"),
            "size_bytes": meta.get("size_bytes"),
            "captured_at": meta.get("captured_at"),
            "recorded_at": record.created_at.isoformat() if record.created_at else None,
            "is_verified": bool(record.is_verified),
        }

    # ------------------------------------------------------------------
    # Claiming (concurrency safety)
    # ------------------------------------------------------------------
    async def claim_next_task(self, worker_id: str) -> Optional[Task]:
        """Atomically claim the highest-priority runnable task.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so two workers never claim the
        same task, and so a crashed worker's row is released by Postgres.
        """
        from backend.app.core.config import settings

        # Only tasks that are executing occupy a slot. A RETRYING task is parked until its backoff
        # elapses and then needs a slot to run: counting it here meant that as many waiting retries
        # as the limit allows blocked every claim, including the retries themselves.
        active_count = await self.db.scalar(
            select(func.count(Task.id)).where(Task.status == TaskStatus.IN_PROGRESS.value)
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

        # Compare-and-set: FOR UPDATE SKIP LOCKED is a no-op on SQLite, so two workers could both
        # SELECT the same READY row. The guarded UPDATE succeeds for exactly one of them.
        claimed = await self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status.in_([TaskStatus.READY.value, TaskStatus.RETRYING.value]),
            )
            .values(
                status=TaskStatus.IN_PROGRESS.value,
                execution_started_at=func.coalesce(
                    Task.execution_started_at, datetime.now(timezone.utc)
                ),
            )
            .execution_options(synchronize_session="fetch")
        )
        if (claimed.rowcount or 0) != 1:
            logger.info("Task %s was claimed by another worker first", task.task_id)
            return None
        await self.db.refresh(task)
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

        # Explicit queries: lazy relationship access raises MissingGreenlet in an
        # async session, which surfaced as a 500 on the Task Detail panel.
        evidence_count = await self.db.scalar(
            select(func.count(TaskEvidence.id)).where(TaskEvidence.task_id == task.id)
        ) or 0
        evidence_rows = (
            await self.db.execute(
                select(TaskEvidence)
                .where(TaskEvidence.task_id == task.id)
                .order_by(TaskEvidence.created_at.desc(), TaskEvidence.id)
                .limit(EVIDENCE_LIST_LIMIT)
            )
        ).scalars().all()
        checkpoint_count = await self.db.scalar(
            select(func.count(TaskCheckpoint.id)).where(TaskCheckpoint.task_id == task.id)
        ) or 0
        tx_rows = (
            await self.db.execute(
                select(BlockchainTransaction).where(BlockchainTransaction.task_id == task.id)
            )
        ).scalars().all()

        # Why the task is parked. The reason is stored on the status-transition
        # event; surface it so the operator can see what they must do.
        blocker: Optional[str] = None
        if task.status == TaskStatus.NEEDS_USER_ACTION.value:
            for e in events:  # newest first
                payload = e.payload or {}
                if e.event_type == "STATUS_TRANSITION" and payload.get("to") == task.status:
                    blocker = str(payload.get("reason") or "") or None
                    break

        pending = [
            a for a in (task.planned_actions or [])
            if not any(c.get("id") == a.get("id") for c in (task.completed_actions or []))
        ]

        return {
            "task_id": task.task_id,
            "title": task.title,
            "description": (task.description or "")[:DESCRIPTION_MAX],
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
            "checkpoints": checkpoint_count,
            "evidence_count": evidence_count,
            "evidence": [self.evidence_view(r, task.task_id) for r in evidence_rows],
            "error_details": task.error_details or {},
            "next_planned_action": task.next_planned_action,
            "blocker": blocker,
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
                {
                    "type": e.event_type,
                    "severity": e.severity,
                    "reason": str((e.payload or {}).get("reason") or "")[:500] or None,
                    "at": e.created_at.isoformat(),
                }
                for e in events
            ],
            "transactions": [
                {
                    "hash": t.tx_hash,
                    "status": t.status,
                    "network": t.network,
                    # Column names, not the shorter aliases used here before. The
                    # aliases do not exist on the model, so any task that had
                    # actually sent a transaction raised AttributeError here and the
                    # whole status report - the "what happened to my task" endpoint -
                    # returned a 500 exactly when there was something to report.
                    "value_usd": t.usd_value_transacted,
                    "confirmations": t.confirmation_count,
                }
                for t in tx_rows
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
