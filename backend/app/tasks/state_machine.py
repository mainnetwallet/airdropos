"""Task State Machine (Specification Part 7.3).

Transitions are enforced in the backend. A task may only reach COMPLETED when its
completion conditions have been VERIFIED. Uncertainty is represented explicitly
with UNVERIFIED rather than being silently folded into success.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, FrozenSet, Optional, Set

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    RESEARCHING = "RESEARCHING"
    PLANNING = "PLANNING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    RETRYING = "RETRYING"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNVERIFIED = "UNVERIFIED"


# Terminal states: no further automatic transition.
TERMINAL_STATES: FrozenSet[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# States a worker may pick up and execute.
EXECUTABLE_STATES: FrozenSet[TaskStatus] = frozenset(
    {TaskStatus.READY, TaskStatus.RETRYING, TaskStatus.IN_PROGRESS}
)

# States requiring explicit human intervention.
BLOCKED_STATES: FrozenSet[TaskStatus] = frozenset({TaskStatus.NEEDS_USER_ACTION})

# States that consume an execution slot.
ACTIVE_STATES: FrozenSet[TaskStatus] = frozenset(
    {TaskStatus.IN_PROGRESS, TaskStatus.RETRYING}
)


VALID_TRANSITIONS: Dict[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.CREATED: {
        TaskStatus.RESEARCHING,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.RESEARCHING: {
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.UNVERIFIED,
        TaskStatus.WAITING,
        TaskStatus.NEEDS_USER_ACTION,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PLANNING: {
        TaskStatus.READY,
        TaskStatus.RESEARCHING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.NEEDS_USER_ACTION,
    },
    TaskStatus.READY: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.RETRYING,
        TaskStatus.WAITING,
        TaskStatus.PLANNING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.COMPLETED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.RETRYING,
        TaskStatus.WAITING,
        TaskStatus.NEEDS_USER_ACTION,
        TaskStatus.UNVERIFIED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.WAITING: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
        TaskStatus.NEEDS_USER_ACTION,
    },
    TaskStatus.NEEDS_USER_ACTION: {
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.RETRYING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
        TaskStatus.UNVERIFIED,
    },
    TaskStatus.RETRYING: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.READY,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.NEEDS_USER_ACTION,
        TaskStatus.UNVERIFIED,
    },
    TaskStatus.PARTIALLY_COMPLETED: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.READY,
        TaskStatus.RETRYING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.WAITING,
    },
    TaskStatus.COMPLETED: {
        # Reopening requires an explicit admin action; the agent cannot self-verify
        # a completion it previously declared.
        TaskStatus.RETRYING,
    },
    TaskStatus.FAILED: {
        TaskStatus.RETRYING,
        TaskStatus.PLANNING,
        TaskStatus.CANCELLED,
    },
    TaskStatus.CANCELLED: set(),
    TaskStatus.UNVERIFIED: {
        TaskStatus.RESEARCHING,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.RETRYING,
        TaskStatus.FAILED,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    },
}


class InvalidTransition(Exception):
    """Raised when a forbidden state transition is attempted."""

    def __init__(self, current: TaskStatus, requested: TaskStatus):
        super().__init__(
            f"Invalid task transition: {current.value} -> {requested.value}. "
            f"Allowed from {current.value}: "
            f"{sorted(s.value for s in VALID_TRANSITIONS.get(current, set())) or 'none (terminal)'}"
        )
        self.current = current
        self.requested = requested


def can_transition(current: TaskStatus, requested: TaskStatus) -> bool:
    return requested in VALID_TRANSITIONS.get(current, set())


def assert_transition(current: TaskStatus, requested: TaskStatus) -> None:
    if not can_transition(current, requested):
        raise InvalidTransition(current, requested)


def assert_completion_verified(status: TaskStatus, verified: bool, evidence_count: int = 0) -> None:
    """Guard the COMPLETED transition.

    A task is COMPLETED only when its completion conditions were verified against
    evidence. Otherwise the caller must use PARTIALLY_COMPLETED or UNVERIFIED.
    """
    if status is not TaskStatus.COMPLETED:
        return
    if not verified:
        raise InvalidTransition(TaskStatus.COMPLETED, TaskStatus.UNVERIFIED)
    if evidence_count == 0:
        raise InvalidTransition(TaskStatus.COMPLETED, TaskStatus.UNVERIFIED)


def progress_for_status(status: TaskStatus) -> float:
    """Default progress fraction used when a caller does not supply one."""
    return {
        TaskStatus.CREATED: 0.0,
        TaskStatus.RESEARCHING: 0.1,
        TaskStatus.PLANNING: 0.2,
        TaskStatus.READY: 0.25,
        TaskStatus.IN_PROGRESS: 0.5,
        TaskStatus.WAITING: 0.5,
        TaskStatus.NEEDS_USER_ACTION: 0.5,
        TaskStatus.RETRYING: 0.5,
        TaskStatus.PARTIALLY_COMPLETED: 0.75,
        TaskStatus.UNVERIFIED: 0.75,
        TaskStatus.COMPLETED: 1.0,
        TaskStatus.FAILED: 0.0,
        TaskStatus.CANCELLED: 0.0,
    }.get(status, 0.0)


def is_claimable(status: TaskStatus) -> bool:
    """Whether a worker may atomically claim this task for execution."""
    return status in EXECUTABLE_STATES
