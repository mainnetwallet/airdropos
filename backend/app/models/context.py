"""Persistent Execution Context (Task Context / Campaign Context).

The task engine already persists plans, executions, checkpoints and evidence, but
none of that answers the question the executor has to answer before every action:

    * what was the goal, and what does "done" mean for it?
    * what has already been attempted, and what did each attempt actually return?
    * what did the last action teach us that the plan did not know?
    * which requirements are still outstanding?
    * which external resource (page, chat, session, wallet) is the relevant one now?
    * why is the next action the next action?

Those answers live only in the LLM context window today, so a restart, a replan or a
switch between integrations loses them. This module stores them as structured rows so
they survive every one of those events and can be queried, tested and replayed.

Three levels, deliberately separate so one task can never inherit another's history:

* `TaskContext` - exactly one row per task. The isolated execution context.
* `TaskObservation` - the action history of that one task: what was attempted, what
  came back, what state was observed, and whether the outcome is proven.
* `TaskRequirement` - requirements discovered while executing, each tied to the
  observation that produced it and, where applicable, marked campaign-scoped (safe to
  reuse for another task on the same campaign) or account/wallet-specific (never).

Campaign-wide facts that several tasks may legitimately share live on the campaign row
and in scope-tagged memory (`MemoryRecord.scope`), not here: this module only holds
the link to the campaign.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


# Outcomes recorded on an observation. Deliberately finer-grained than a step status,
# because "attempted" and "confirmed" are different claims and conflating them is how
# an unproven action gets reported as done.
OUTCOME_ATTEMPTED = "attempted"
OUTCOME_EXECUTED = "executed"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_UNVERIFIED = "unverified"
OUTCOME_FAILED = "failed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_SKIPPED = "skipped"
# Recorded when a repeat of an already-confirmed action is suppressed rather than
# re-run. It is history, not a new outcome: nothing was attempted this time.
OUTCOME_DUPLICATE_SUPPRESSED = "duplicate_suppressed"

OBSERVATION_OUTCOMES = frozenset(
    {
        OUTCOME_ATTEMPTED,
        OUTCOME_EXECUTED,
        OUTCOME_CONFIRMED,
        OUTCOME_UNVERIFIED,
        OUTCOME_FAILED,
        OUTCOME_BLOCKED,
        OUTCOME_SKIPPED,
        OUTCOME_DUPLICATE_SUPPRESSED,
    }
)

# An observation is authoritative only if it proves an external state change.
OUTCOMES_PROVING_COMPLETION = frozenset({OUTCOME_CONFIRMED})


REQUIREMENT_DISCOVERED = "discovered"
REQUIREMENT_SATISFIED = "satisfied"
REQUIREMENT_WAIVED = "waived"
REQUIREMENT_UNVERIFIABLE = "unverifiable"


class TaskContext(Base):
    """The isolated, independently persisted execution context of exactly one task.

    One row per task, created with the task's first execution and updated after every
    meaningful action. It holds the goal, the success criteria, the outstanding
    requirements, the external resource references, the last observed external state,
    the reason the current next step was chosen, and the human-intervention and
    authorization state.

    Everything here is task-scoped. Nothing in this row may be read by another task
    except through an explicit campaign link, which carries only campaign-wide facts.
    """

    __tablename__ = "task_contexts"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )
    # Set only when this task was verified to belong to a known campaign. A link here
    # authorises reading campaign-wide facts; it never transfers account- or
    # wallet-specific state, which stays in this task's own rows.
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("airdrop_campaigns.id", ondelete="SET NULL"),
        index=True, nullable=True,
    )

    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    normalized_objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success_criteria: Mapped[Optional[List[Any]]] = mapped_column(JSONB, default=list)

    # Denormalised view of the outstanding `task_requirements` rows, so a single read
    # answers "what is still missing" without a join. `task_requirements` remains the
    # authoritative record; this list is kept in step with it.
    outstanding_requirements: Mapped[Optional[List[Any]]] = mapped_column(JSONB, default=list)
    unresolved_questions: Mapped[Optional[List[Any]]] = mapped_column(JSONB, default=list)

    # Page URL, chat id, browser session key, mini-app URL, contract address... The
    # references that let a later step in a different integration pick up where this
    # one left off. Values are references only; nothing secret may be stored here.
    resource_refs: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    # The last observed external state, as opposed to the last tool return. "The bot
    # says the join is required" is an observation; "the join tool returned 200" is a
    # result. They are kept apart so an attempt is never mistaken for a fact.
    external_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    next_step_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # What the task is waiting on a human for, and what has already been authorised.
    human_intervention: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    authorization_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    retry_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Bumped on every write. A task resuming after a crash compares this against the
    # plan revision it thought it had, so a context written by a superseded plan is
    # never trusted as current.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    task: Mapped["Task"] = relationship("Task", back_populates="execution_context")
    observations: Mapped[List["TaskObservation"]] = relationship(
        "TaskObservation", back_populates="context", cascade="all, delete-orphan",
        order_by="TaskObservation.id",
    )
    requirements: Mapped[List["TaskRequirement"]] = relationship(
        "TaskRequirement", back_populates="context", cascade="all, delete-orphan",
        order_by="TaskRequirement.id",
    )


class TaskObservation(Base):
    """One entry in a task's action history.

    Kept separate from `TaskExecution` (which is the loop's own audit trail) and from
    `TaskEvidence` (which is a captured artifact) because the thing the executor needs
    between steps is the *meaning* of what happened: which action, on which arguments,
    what came back, what state was observed, and whether the outcome is proven.

    `fingerprint` is a digest of the tool plus its canonical arguments. It is what lets
    a resumed or replanned run recognise an action it has already confirmed instead of
    repeating it.
    """

    __tablename__ = "task_observations"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # See TaskObservation.context_id: the explicit link to the owning context row, so the
    # parent/child relationship is a real foreign key and not two paths to `tasks` that
    # SQLAlchemy cannot resolve into a single join.
    context_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_contexts.id", ondelete="CASCADE"),
        index=True, nullable=True,
    )
    iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    action: Mapped[str] = mapped_column(String(500), nullable=False)
    tool: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    # Digest of the arguments. The arguments themselves are not kept: they can contain
    # typed text or values that should not be replayed into a later prompt verbatim.
    arguments_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    outcome: Mapped[str] = mapped_column(String(40), default=OUTCOME_ATTEMPTED, index=True, nullable=False)
    evidence_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_authoritative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # What the external system actually looked like after the action. This is the
    # "observed external state" that an attempt alone does not establish.
    observed_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    # Bounded excerpt of what the tool returned, kept for explanation only.
    result_digest: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    integration_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_idempotent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    context: Mapped["TaskContext"] = relationship("TaskContext", back_populates="observations")

    __table_args__ = (
        Index("ix_task_observations_task_fingerprint", "task_id", "fingerprint"),
        Index("ix_task_observations_task_step", "task_id", "step_index"),
    )


class TaskRequirement(Base):
    """A requirement discovered while executing, and whether it is satisfied.

    Requirements are discovered from what external systems actually said - a bot's
    instructions, a page's requirements list, a campaign's eligibility rules - not from
    anything the model asserts. Each one records where it came from and which
    observation satisfied it, so "complete all eligible tasks" is answerable from the
    record instead of from a claim.

    `is_account_specific` marks a requirement that belongs to one account or wallet
    (membership of a group *this* account joined, eligibility of *this* wallet). Such a
    requirement is never reusable by another task even when both are on one campaign.
    """

    __tablename__ = "task_requirements"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # See TaskObservation.context_id: the explicit link to the owning context row.
    context_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_contexts.id", ondelete="CASCADE"),
        index=True, nullable=True,
    )
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Where it was learned: a tool result, an operator instruction, a campaign record.
    origin: Mapped[str] = mapped_column(String(50), default="tool_result", nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(
        String(30), default=REQUIREMENT_DISCOVERED, index=True, nullable=False
    )
    satisfied_by_observation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_observations.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    # A campaign-scoped requirement describes the campaign itself and may inform
    # another task on the same campaign. An account-scoped one never does.
    is_campaign_scoped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_account_specific: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The account or wallet a specific requirement applies to. Read only together with
    # this task; never inherited by another task.
    account_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    context: Mapped["TaskContext"] = relationship("TaskContext", back_populates="requirements")