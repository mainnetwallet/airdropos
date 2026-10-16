"""Outcome-Based Learning Engine.

Analyzes execution history to propose improvements. Learning records are kept
SEPARATE from authoritative task records so analysis never corrupts ground truth.
Security controls are never disabled to improve a success rate (Part 27).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import LearningRecord, Task, TaskEvent, ToolExecution
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

# Minimum sample size before a pattern is considered reliable. One success is
# never treated as proof that a workflow is universally correct.
MIN_SAMPLE_SIZE = 5


@dataclass
class LearningInsight:
    topic: str
    pattern_type: str
    observation: str
    sample_size: int
    recommendation: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "pattern_type": self.pattern_type,
            "observation": self.observation,
            "sample_size": self.sample_size,
            "recommendation": self.recommendation,
            "confidence": round(self.confidence, 3),
        }


class LearningEngine:
    """Derives insights from execution outcomes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def analyze_tool_reliability(self, min_samples: int = MIN_SAMPLE_SIZE) -> List[LearningInsight]:
        insights: List[LearningInsight] = []
        rows = (
            await self.db.execute(
                select(
                    ToolExecution.task_id,
                    ToolExecution.duration_ms,
                    ToolExecution.success,
                    ToolExecution.error_message,
                )
            )
        ).all()
        if not rows:
            return insights

        grouped: Dict[str, List] = {}
        # Tool identity is tracked via the tool's primary key on the parent row;
        # we approximate by error/success profile when that is unavailable.
        for task_id, duration, success, error in rows:
            key = str(error)[:80] if error else "success"
            grouped.setdefault(key, []).append((duration, success))

        for key, entries in grouped.items():
            successes = sum(1 for _, ok in entries if ok)
            rate = successes / len(entries)
            avg_ms = sum(d or 0 for d, _ in entries) / len(entries)
            insights.append(
                LearningInsight(
                    topic=key,
                    pattern_type="TOOL_PERFORMANCE",
                    observation=f"Success rate {rate:.0%} over {len(entries)} executions, avg {avg_ms:.0f}ms",
                    sample_size=len(entries),
                    recommendation=(
                        "Keep current configuration."
                        if rate >= 0.9
                        else "Investigate retries, timeouts, or alternative tooling."
                    ),
                    confidence=min(1.0, len(entries) / (min_samples * 4)),
                )
            )
        return insights

    async def analyze_task_outcomes(self) -> List[LearningInsight]:
        counts = (
            await self.db.execute(
                select(Task.status, func.count(Task.id)).group_by(Task.status)
            )
        ).all()
        if not counts:
            return []

        total = sum(count for _, count in counts)
        insights: List[LearningInsight] = []

        for status, count in counts:
            rate = count / total if total else 0
            if status == TaskStatus.FAILED.value and rate > 0.25:
                insights.append(
                    LearningInsight(
                        topic="task_failure_rate",
                        pattern_type="FAILURE",
                        observation=f"{rate:.0%} of tasks ended FAILED ({count}/{total}).",
                        sample_size=total,
                        recommendation="Review failure records for a recurring category and add a replanning rule.",
                        confidence=0.6 if total >= MIN_SAMPLE_SIZE else 0.3,
                    )
                )
            elif status == TaskStatus.UNVERIFIED.value and rate > 0.15:
                insights.append(
                    LearningInsight(
                        topic="verification_gap",
                        pattern_type="VERIFICATION",
                        observation=f"{rate:.0%} of tasks ended UNVERIFIED ({count}/{total}).",
                        sample_size=total,
                        recommendation=(
                            "Actions are succeeding without authoritative evidence. "
                            "Add verification tools or tighten completion criteria."
                        ),
                        confidence=0.6 if total >= MIN_SAMPLE_SIZE else 0.3,
                    )
                )
            elif status == TaskStatus.NEEDS_USER_ACTION.value and count > 0:
                insights.append(
                    LearningInsight(
                        topic="human_blockers",
                        pattern_type="BLOCKER",
                        observation=f"{count} task(s) are blocked on human action.",
                        sample_size=total,
                        recommendation="Review whether these can be resolved by configuration rather than manual intervention.",
                        confidence=0.5,
                    )
                )
        return insights

    async def analyze_repeated_errors(self, window_hours: int = 168) -> List[LearningInsight]:
        """Find error categories that keep recurring across tasks."""
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        rows = (
            await self.db.execute(
                select(TaskEvent.event_type, TaskEvent.severity, func.count(TaskEvent.id))
                .where(TaskEvent.severity == "ERROR")
                .where(TaskEvent.created_at >= since)
                .group_by(TaskEvent.event_type, TaskEvent.severity)
            )
        ).all()

        insights = []
        for event_type, _, count in rows:
            if count >= 3:
                insights.append(
                    LearningInsight(
                        topic=event_type,
                        pattern_type="REPEATED_ERROR",
                        observation=f"{count} error event(s) of type '{event_type}' in the last {window_hours}h.",
                        sample_size=count,
                        recommendation="Add a targeted recovery strategy for this failure category.",
                        confidence=min(0.9, count / 10),
                    )
                )
        return insights

    async def persist(self, insights: List[LearningInsight]) -> int:
        stored = 0
        for insight in insights:
            if insight.sample_size < 1:
                continue
            self.db.add(
                LearningRecord(
                    topic=insight.topic,
                    pattern_type=insight.pattern_type,
                    observation=insight.observation,
                    sample_size=insight.sample_size,
                    recommendation=insight.recommendation,
                    confidence=insight.confidence,
                    metrics={"avg_duration_ms": None},
                )
            )
            stored += 1
        await self.db.flush()
        return stored

    async def run_full_analysis(self) -> Dict[str, Any]:
        """Run every analyzer and persist the results."""
        insights: List[LearningInsight] = []
        insights.extend(await self.analyze_task_outcomes())
        insights.extend(await self.analyze_repeated_errors())
        insights.extend(await self.analyze_tool_reliability())

        # Never recommend weakening a security control.
        for insight in insights:
            if any(
                phrase in insight.recommendation.lower()
                for phrase in ("disable", "bypass", "lower the limit", "skip verification")
            ):
                insight.recommendation = (
                    "Recommendation rejected by policy: security controls are never weakened "
                    "to improve a success rate."
                )
                insight.confidence = 0.0

        stored = await self.persist(insights)
        return {
            "insight_count": len(insights),
            "persisted": stored,
            "insights": [i.to_dict() for i in insights],
        }
