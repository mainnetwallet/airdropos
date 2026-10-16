"""Task State Machine (Specification Part 7.3).

Transitions are enforced in the backend. A task may only reach COMPLETED when its
completion conditions have been VERIFIED. Uncertainty is represented explicitly
with UNVERIFIED rather than being silently folded into success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

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
        # Yielding an execution window back to the queue. NOT a failure: the run spent its
        # per-run iteration budget, the plan, evidence and checkpoints are persisted, and
        # the worker re-claims the task from READY and continues from the latest
        # checkpoint. This is what lets a long objective span many execution windows
        # without a human having to press Retry, and without any arbitrary small limit
        # deciding the task's fate.
        TaskStatus.READY,
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


@dataclass(frozen=True)
class CompletionProof:
    """What the system could actually prove about a finished task.

    `verified` and `evidence_count` on their own are two scalars a caller asserts about
    itself, and a caller that wanted COMPLETED could assert anything. This carries the
    substance instead: how many persisted observations independently confirmed the
    outcome, whether anything is still unaccounted for, and what is missing when the proof
    is not sufficient.

    The state machine checks the *shape* of the proof; `TaskManager` additionally checks
    that the confirmations really exist in this task's persisted observations. Together
    they enforce the same invariant the controller decides on, so a task cannot reach
    COMPLETED by asserting that it verified itself.
    """

    authoritative_confirmations: int = 0
    outstanding_requirements: int = 0
    unverified_steps: Tuple[str, ...] = ()
    failed_steps: Tuple[str, ...] = ()
    blocked_steps: Tuple[str, ...] = ()
    # Steps the current policy refused. These are gaps too: a plan whose value-moving
    # step was denied by the authorization policy has not had that action carried out,
    # and evidence belonging to some *other* step must not stand in for it.
    skipped_steps: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    # The plan revision this proof was assembled against. Without it a proof stays valid
    # for ever: a task verifies every step of revision 3, replans to revision 4 which adds
    # two mandatory steps, and the revision-3 proof would still authorize COMPLETED over a
    # plan whose new requirements were never attempted. `TaskManager` compares this with
    # the persisted current revision and refuses the transition on any mismatch, so a
    # stale proof cannot authorize a completion it was not built to justify.
    plan_revision: Optional[int] = None

    @property
    def is_sufficient(self) -> bool:
        return (
            self.authoritative_confirmations > 0
            and self.outstanding_requirements == 0
            and not self.unverified_steps
            and not self.failed_steps
            and not self.blocked_steps
            and not self.skipped_steps
        )

    def explain(self) -> str:
        """Why this proof is not sufficient, in the operator's terms. Empty when it is."""
        gaps: List[str] = []
        if self.authoritative_confirmations <= 0:
            gaps.append(
                "no observation in this task's history authoritatively confirmed any outcome"
            )
        if self.outstanding_requirements:
            gaps.append(
                f"{self.outstanding_requirements} discovered requirement(s) were never "
                f"satisfied ({'; '.join(self.missing[:3]) or 'no detail recorded'})"
            )
        for label, items in (
            ("ran but could not be proven", self.unverified_steps),
            ("failed", self.failed_steps),
            ("blocked", self.blocked_steps),
            ("were skipped, so the action they describe was not carried out", self.skipped_steps),
        ):
            if items:
                gaps.append(f"{len(items)} step(s) {label}: {'; '.join(items[:3])}")
        return "; ".join(gaps)


def assert_completion_verified(
    status: TaskStatus,
    verified: bool,
    evidence_count: int = 0,
    proof: Optional[CompletionProof] = None,
) -> None:
    """Guard the COMPLETED transition.

    A task is COMPLETED only when its completion conditions were verified against
    evidence. Otherwise the caller must use PARTIALLY_COMPLETED or UNVERIFIED.

    When a `CompletionProof` is supplied it must also be sufficient on its own terms. That
    is what stops "the model said complete" plus a floored-up evidence count from being
    enough: the proof has to name real confirmations and must not conceal an unverified,
    failed or blocked step, or an outstanding requirement nobody satisfied.
    """
    if status is not TaskStatus.COMPLETED:
        return
    if not verified:
        raise InvalidTransition(TaskStatus.COMPLETED, TaskStatus.UNVERIFIED)
    if evidence_count <= 0:
        raise InvalidTransition(TaskStatus.COMPLETED, TaskStatus.UNVERIFIED)
    if proof is not None and not proof.is_sufficient:
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
