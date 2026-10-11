"""Checkpoint Manager: resumable state across process restarts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Task, TaskCheckpoint

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Persists enough state to resume an interrupted task without redoing work."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def save(
        self,
        task: Task,
        checkpoint_name: str,
        state: Dict[str, Any],
        iteration: int = 0,
    ) -> TaskCheckpoint:
        checkpoint = TaskCheckpoint(
            task_id=task.id,
            checkpoint_name=checkpoint_name,
            state=state,
            iteration=iteration,
        )
        self.db.add(checkpoint)
        await self.db.flush()
        logger.info("Saved checkpoint '%s' for task %s", checkpoint_name, task.task_id)
        return checkpoint

    async def load_latest(self, task: Task) -> Optional[TaskCheckpoint]:
        return await self.db.scalar(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.task_id == task.id)
            .order_by(desc(TaskCheckpoint.iteration), desc(TaskCheckpoint.created_at))
            .limit(1)
        )

    async def list_all(self, task: Task) -> List[TaskCheckpoint]:
        return list(
            (
                await self.db.execute(
                    select(TaskCheckpoint)
                    .where(TaskCheckpoint.task_id == task.id)
                    .order_by(TaskCheckpoint.iteration)
                )
            ).scalars().all()
        )

    @staticmethod
    def execution_snapshot(
        iteration: int,
        completed_action_ids: List[str],
        pending_actions: List[Dict[str, Any]],
        observations: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Canonical shape of a resumable execution snapshot."""
        return {
            "iteration": iteration,
            "completed_action_ids": completed_action_ids,
            "pending_actions": pending_actions,
            "observations": observations,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def should_resume(status: str) -> bool:
        """A task parked in IN_PROGRESS after a crash is resumable."""
        return status in ("IN_PROGRESS", "RETRYING", "WAITING", "READY")
