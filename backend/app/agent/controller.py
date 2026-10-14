"""Central Autonomous Agent Controller.

Implements the reusable Observe -> Plan -> Act -> Verify loop (Specification Part 6).

Non-negotiable invariants enforced here:
  * The agent must not assume its intended action succeeded.
  * A click is not proof of submission. An HTTP 200 is not proof of a mined
    transaction. A mined transaction is not proof a reward was received.
  * Every step is persisted. Iteration counts, timeouts, and budgets are bounded
    so no loop can run forever.
  * The LLM proposes; deterministic code disposes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.context import TaskContextService, action_fingerprint
from backend.app.agent.discovery import DiscoveryBudget, extract_from_result
from backend.app.core.config import settings
from backend.app.llm.client import LLMError, Message, llm
from backend.app.models import Task
from backend.app.models.context import (
    OUTCOME_ATTEMPTED,
    OUTCOME_BLOCKED,
    OUTCOME_CONFIRMED,
    OUTCOME_DUPLICATE_SUPPRESSED,
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_UNVERIFIED,
)
from backend.app.planning.planner import (
    ExecutionPlanSpec,
    PlanConstraints,
    PlanningError,
    Planner,
    ReplanTrigger,
    StepKind,
)
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.checkpoints import CheckpointManager
from backend.app.tasks.concurrency import LeaseManager
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.recovery import FailureDiagnosisEngine, next_retry_at
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools.base import SideEffect, ToolResultStatus
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry

logger = logging.getLogger(__name__)

MAX_REPLANS_PER_TASK = 2

# Verification methods that make no specific claim about WHAT was proven. Any
# authoritative evidence satisfies them. `unspecified` is deliberately NOT here:
# the planner's own default means "nobody said how this is proven", and treating
# that as satisfied is how unverified work gets reported as done.
GENERIC_EVIDENCE_METHODS = frozenset({"", "tool_evidence", "evidence_present"})

# Cross-integration evidence equivalence. Some verification methods describe a FACT
# rather than an integration ("the remote state now says X"). Reading a Telegram bot's
# reply IS an authoritative observation of remote state, exactly like a page
# observation, so either may satisfy the other's method.
#
# Without this, a planner that wrote "page_observation" for a Telegram read step caused
# a genuinely successful action to be recorded as FAILED. Its dependents then never
# became ready and the workflow stopped right after that action.
EQUIVALENT_EVIDENCE_KINDS: Dict[str, frozenset] = {
    "page_observation": frozenset(
        {"page_observation", "post_action_observation", "message_observed"}
    ),
    "post_action_observation": frozenset(
        {"post_action_observation", "page_observation", "message_observed"}
    ),
    "visible_text": frozenset(
        {"post_action_observation", "page_observation", "message_observed"}
    ),
}

# Which `ToolResult.evidence["kind"]` values can satisfy a concrete verification
# method. A step is verified only when the tool produced evidence of the declared
# kind, so a simulation can never stand in for a chain receipt, and a screenshot
# can never stand in for either. Methods absent from this table are free text the
# model invented; those fall back to "any authoritative evidence" (see _verify_step).
EVIDENCE_KINDS: Dict[str, frozenset] = {
    # On-chain finality. Only a real receipt proves a transaction executed.
    "chain_receipt": frozenset({"chain_receipt", "tx_receipt", "transfer_receipt"}),
    "receipt": frozenset({"chain_receipt", "tx_receipt", "transfer_receipt"}),
    "tx_hash": frozenset({"chain_receipt", "tx_receipt", "transfer_receipt"}),
    # A dry run proves nothing was executed; it cannot satisfy a receipt method.
    "simulation": frozenset({"eth_call_simulation", "simulation"}),
    "eth_call_simulation": frozenset({"eth_call_simulation", "simulation"}),
    # Browser / page state.
    "page_observation": frozenset({"page_observation"}),
    "page_state": frozenset({"page_observation", "post_action_observation"}),
    "post_action_observation": frozenset({"post_action_observation"}),
    "screenshot": frozenset({"screenshot"}),
    "visible_text": frozenset({"post_action_observation", "page_observation"}),
    # Sources and notifications.
    "http_status": frozenset({"http_status"}),
    "source_scan": frozenset({"source_scan"}),
    "notification_delivered": frozenset({"notification_delivery"}),
    # A prepared transfer is a verified *plan*, not a verified broadcast. It gets
    # its own kind so a `chain_receipt` method can never be satisfied by one.
    "prepared_transfer": frozenset({"prepared_transfer"}),
    "transfer_prepared": frozenset({"prepared_transfer"}),
    # Contract reads.
    "contract_read": frozenset({"contract_read"}),
    # A wallet address read straight from the wallet service.
    "wallet_record": frozenset({"wallet_record"}),
    # Database-backed reads.
    "record_read": frozenset({"record_read"}),
    # ------------------------------------------------------------------
    # Telegram (telegram_user_tools / telegram_continuation_tools).
    #
    # These were entirely absent. A Telegram tool's evidence therefore matched no
    # declared method, and any step whose planner-written method happened to be one
    # of the mapped browser/chain methods (a natural choice for "read the bot's
    # reply", e.g. page_observation) was recorded as FAILED even though the action
    # had genuinely succeeded. Its dependents never became ready and the task stopped
    # right after that action -- the reported "joins the group, then stops" defect.
    # ------------------------------------------------------------------
    "bot_started": frozenset({"bot_started"}),
    "group_joined": frozenset({"group_joined", "membership_verified"}),
    "joined": frozenset({"group_joined", "membership_verified"}),
    "membership_verified": frozenset({"membership_verified"}),
    "membership": frozenset({"membership_verified"}),
    "message_observed": frozenset({"message_observed", "message_sent", "bot_started"}),
    "message_read": frozenset({"message_observed"}),
    "bot_reply": frozenset({"message_observed", "bot_started"}),
    "button_clicked": frozenset({"button_clicked"}),
    "message_sent": frozenset({"message_sent"}),
    "dialog_listed": frozenset({"dialog_listed"}),
    "chat_resolved": frozenset({"chat_resolved", "membership_verified"}),
    # A Telegram read is genuinely an observation of remote state, so it also
    # satisfies the generic page-observation methods the planner tends to write.
    "miniapp_opened": frozenset({"miniapp_launch_detected"}),
    "miniapp_state_observed": frozenset({"miniapp_state_observed", "page_observation", "post_action_observation"}),
    "browser_state_observed": frozenset({"page_observation", "post_action_observation", "browser_state_observed"}),
    "form_submitted": frozenset({"form_submitted", "post_action_observation"}),
    "arithmetic_computed": frozenset({"arithmetic_computed"}),
    "human_intervention_requested": frozenset({"human_intervention_requested"}),
    "verification_completed": frozenset({"verification_completed", "membership_verified", "post_action_observation"}),
    "task_resumed": frozenset({"task_resumed"}),
}

from backend.app.agent.prompts import AGENT_SYSTEM_PROMPT as SYSTEM_PROMPT  # noqa: E402
from backend.app.inbox.raise_intervention import park_for_intervention  # noqa: E402
from backend.app.inbox.service import (  # noqa: E402
    AMBIGUOUS_INSTRUCTION,
    HUMAN_VERIFICATION,
    OPERATOR_FALLBACK_EMAIL,
    OPERATOR_PAUSE,
)


class LoopOutcome(str, Enum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    NEEDS_USER_ACTION = "needs_user_action"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STOPPED = "stopped"


@dataclass
class StepResult:
    """Outcome of a single loop iteration."""

    iteration: int
    action_description: str
    tool_name: Optional[str]
    tool_result: Optional[Dict[str, Any]]
    verified: bool
    notes: str
    duration_ms: int


@dataclass
class LoopReport:
    """Evidence-based report returned to the caller (and relayed to Telegram/UI)."""

    task_id: str
    outcome: LoopOutcome
    steps: List[StepResult] = field(default_factory=list)
    verified_completions: List[str] = field(default_factory=list)
    unverified_claims: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_summary(self) -> str:
        """Human-readable summary for notifications. Never overstates progress."""
        lines = [f"Task {self.task_id}: {self.outcome.value.upper()}"]
        if self.verified_completions:
            lines.append(f"Verified: {len(self.verified_completions)} item(s)")
        if self.unverified_claims:
            lines.append(f"Unverified: {len(self.unverified_claims)} item(s)")
        if self.failures:
            lines.append(f"Failures: {len(self.failures)}")
        if self.blockers:
            lines.append(f"Blockers: {'; '.join(self.blockers[:3])}")
        if self.next_actions:
            lines.append(f"Next: {'; '.join(self.next_actions[:3])}")
        return "\n".join(lines)


class AgentController:
    """The single coordinator for all autonomous reasoning and execution.

    Planning is delegated to `Planner`; failure handling to
    `FailureDiagnosisEngine`. This class coordinates them and owns the loop.
    """

    def __init__(
        self,
        db: AsyncSession,
        registry: Optional[ToolRegistry] = None,
        policy: Optional[AuthorizationPolicy] = None,
    ) -> None:
        self.db = db
        self.tasks = TaskManager(db)
        self.checkpoints = CheckpointManager(db)
        self.planner = Planner(db, registry)
        self.leases = LeaseManager(db)
        self.diagnosis = FailureDiagnosisEngine()
        self.registry = registry
        self.policy = policy
        # The task's own persistent execution context (§5). Every observation, discovered
        # requirement, external reference and next-step rationale passes through here, so
        # they survive a restart, a replan and a switch between integrations.
        self.context = TaskContextService(db)
        # Per-run ceilings on the two things that cost an LLM call mid-execution:
        # reading requirements out of untrusted prose, and revising the plan because
        # executing it revealed something new.
        self.discovery_budget = DiscoveryBudget()
        self.discovery_replans = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def execute_task(self, task: Task, max_iterations: Optional[int] = None) -> LoopReport:
        """Run the Observe-Plan-Act-Verify loop until the task ends or a limit hits."""
        started = time.perf_counter()
        budget = max_iterations or settings.MAX_EXECUTION_LOOP_ITERATIONS
        report = LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED)

        if emergency_stop.is_stopped and not emergency_stop.can_execute_tasks():
            report.outcome = LoopOutcome.STOPPED
            report.blockers.append("Emergency stop is active; execution not started.")
            await self._park_for_emergency_stop(task)
            return report

        # Take an execution lease so a dead worker's task is recoverable and a
        # live duplicate cannot execute the same task concurrently.
        lease = None
        worker_id = f"controller-{id(self):x}"
        try:
            lease = await self.leases.acquire(task, worker_id)
            await self.db.commit()  # publish the lease; do not hold the DB write lock across the run
        except Exception as exc:
            logger.warning("Could not acquire lease for %s: %s", task.task_id, exc)
            report.outcome = LoopOutcome.STOPPED
            report.blockers.append(f"Another worker holds this task: {exc}")
            return report

        try:
            plan_spec = await self._ensure_plan(task, report)
            await self.db.commit()
            known_revision = await self._plan_revision(task)
            iteration = await self._resume_iteration(task)
            if iteration:
                report.steps.append(
                    StepResult(iteration, "resumed from checkpoint", None, None, True,
                               "Restored prior state", 0)
                )

            # The budget bounds THIS run. Iteration numbers keep counting up across runs (they
            # order checkpoints and executions), so an absolute cap would leave a resumed task
            # with no iterations left and it could never get past a budget exhaustion.
            run_limit = iteration + budget
            while iteration < run_limit:
                iteration += 1
                iteration_started = time.perf_counter()

                try:
                    step, is_terminal, terminal_outcome = await self._run_iteration(
                        task, iteration, report, plan_spec
                    )
                except asyncio.CancelledError:
                    await self._checkpoint(task, iteration, "cancelled")
                    report.outcome = LoopOutcome.STOPPED
                    report.blockers.append("Execution cancelled.")
                    raise
                except LLMError as exc:
                    logger.error("Fixed LLM failed during task %s: %s", task.task_id, exc)
                    report.failures.append(f"LLM error ({exc.category}): {exc}")
                    report.outcome = LoopOutcome.FAILED
                    await self._checkpoint(task, iteration, "llm_error")
                    break
                except PlanningError as exc:
                    logger.error("Planning failed for task %s: %s", task.task_id, exc)
                    report.failures.append(f"Planning error: {exc}")
                    report.outcome = LoopOutcome.FAILED
                    await self._checkpoint(task, iteration, "planning_error")
                    break

                # A replan persists a NEW revision. Keep iterating over the persisted truth, not
                # the superseded in-memory plan, or removed/changed steps would still be executed.
                revision = await self._plan_revision(task)
                if revision != known_revision:
                    refreshed = await self.planner.load_plan_spec(task)
                    if refreshed is not None and refreshed.steps:
                        plan_spec = refreshed
                    known_revision = revision

                step.duration_ms = int((time.perf_counter() - iteration_started) * 1000)
                report.steps.append(step)
                await self.tasks.save_execution(
                    task,
                    iteration=iteration,
                    plan={"action": step.action_description, "tool": step.tool_name},
                    observation={"verified": step.verified, "notes": step.notes},
                    reasoning=step.notes,
                    result=step.tool_result or {},
                    status="SUCCESS" if step.verified else "UNVERIFIED",
                    duration_ms=step.duration_ms,
                )
                await self.leases.renew(task, lease.lease_token)
                lease.last_iteration = iteration

                if is_terminal:
                    report.outcome = terminal_outcome
                    break

                await self._checkpoint(task, iteration, f"iteration_{iteration}")
                # Commit per iteration: progress, evidence and checkpoints survive a crash or a
                # later failure, and SQLite's single write lock is released between steps.
                await self.db.commit()
            else:
                # Loop exhausted its budget without reaching a terminal state.
                await self._settle_budget_exhausted(task, iteration, budget, report)

            report.duration_ms = int((time.perf_counter() - started) * 1000)
            await self._apply_final_status(task, report)
            return report
        finally:
            await self.leases.release(task, lease.lease_token)

    # ------------------------------------------------------------------
    async def _settle_budget_exhausted(
        self, task: Task, iteration: int, budget: int, report: LoopReport
    ) -> None:
        """Give a task whose run hit the iteration budget an honest, non-stuck status.

        Without this the task stayed IN_PROGRESS: not claimable (workers only claim READY or
        RETRYING), not terminal, and invisible to everything except a worker restart.

        * If the persisted plan is in fact fully processed (finalization simply needs one more
          iteration than the steps themselves), finalize it. That path verifies against evidence,
          so it yields COMPLETED only when every step was proven, otherwise PARTIALLY_COMPLETED.
        * Otherwise work remains: PARTIALLY_COMPLETED ("some actions succeeded; more remain").
          This is deliberately not RETRYING: hitting the budget is not a failure, so it must not
          consume the retry budget or schedule automatic re-runs. An operator resumes it
          (PARTIALLY_COMPLETED -> READY/RETRYING). Completed steps, checkpoints, executions and
          evidence are left untouched; the plan's persisted step statuses drive the resume.
        """
        # Read the persisted plan: the in-memory spec can be stale after a replan.
        plan = await self.planner.load_plan_spec(task)

        if plan is not None and plan.steps and plan.is_complete():
            final_iteration = iteration + 1
            step, _terminal, outcome = await self._finalize_plan(task, final_iteration, plan, report)
            report.steps.append(step)
            await self.tasks.save_execution(
                task,
                iteration=final_iteration,
                plan={"action": step.action_description, "tool": None},
                observation={"verified": step.verified, "notes": step.notes},
                reasoning=step.notes,
                result=step.tool_result or {},
                status="SUCCESS" if step.verified else "UNVERIFIED",
                duration_ms=0,
            )
            report.outcome = outcome
            await self._checkpoint(task, final_iteration, "plan_finalized")
            return

        report.outcome = LoopOutcome.BUDGET_EXHAUSTED
        report.blockers.append(
            f"Reached the {budget}-iteration limit without completing. "
            "Work is checkpointed and resumable."
        )
        await self._checkpoint(task, iteration, "budget_exhausted")

        if task.status != TaskStatus.IN_PROGRESS.value:
            return  # something already moved the task to a deliberate status; keep it
        remaining = plan.ready_steps() if plan is not None else []
        await self.tasks.transition(
            task,
            TaskStatus.PARTIALLY_COMPLETED,
            reason=(
                f"Iteration budget of {budget} exhausted with work remaining; "
                "progress is checkpointed and the task can be resumed."
            ),
            progress=self._plan_progress(plan) if plan is not None else task.progress,
            next_planned_action=(
                remaining[0].description if remaining else task.next_planned_action
            ),
        )

    async def _park_for_emergency_stop(self, task: Task) -> None:
        """Do not leave a halted task IN_PROGRESS.

        IN_PROGRESS is not claimable and the dashboard shows it as running, so a task halted by
        the emergency stop would look live until the next worker restart. The stop is checked
        before a step starts, so nothing is in flight. NEEDS_USER_ACTION is the lifecycle's
        "an operator must decide" state; completed work and checkpoints are untouched, no retry
        is consumed, and the task is never restarted by an informational reply -- lifting the
        stop is an explicit operator action.
        """
        if task.status != TaskStatus.IN_PROGRESS.value:
            return
        await self._request_operator_input(
            task,
            iteration=0,
            error_text=(
                "Emergency stop engaged; execution halted before the next step. "
                "Resume after review."
            ),
            force_reason_code=OPERATOR_PAUSE,
            blocking_step=task.next_planned_action or "Execution halted by the emergency stop",
        )

    # ------------------------------------------------------------------
    async def _request_operator_input(
        self,
        task: Task,
        *,
        iteration: int,
        error_text: str,
        error_category: Optional[str] = None,
        blocking_step: Optional[str] = None,
        integration_ref: Optional[str] = None,
        force_reason_code: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist an intervention request and tell the operator what to do.

        Replaces a silent transition into NEEDS_USER_ACTION: the task now checkpoints, opens
        (or reuses) its Agent Inbox conversation, and posts a message that names the real
        blocker. If any of that fails the task is deliberately left running-failed rather than
        parked with nobody able to unblock it.
        """
        try:
            result = await park_for_intervention(
                self.db,
                task,
                owner_email=task.owner_email or OPERATOR_FALLBACK_EMAIL,
                error_text=error_text,
                error_category=error_category,
                blocking_step=blocking_step,
                integration_ref=integration_ref,
                iteration=iteration,
                force_reason_code=force_reason_code,
            )
            # The context records what is being waited on, so "waiting for a human" is a
            # durable fact about this task rather than something only the inbox knows.
            await self.context.record_human_intervention(
                task,
                required=result is not None,
                reason=error_text,
                instruction=blocking_step,
            )
            if result is None:
                logger.error(
                    "Could not raise an intervention for %s; leaving it in %s",
                    task.task_id, task.status,
                )
            return result
        except Exception as exc:
            # A failure to notify must never be worse than the failure being reported.
            logger.error("Intervention request failed for %s: %s", task.task_id, exc)
            return None

    @staticmethod
    def _integration_ref(tool_result: Any) -> Optional[str]:
        """Best-effort session reference from a tool result, e.g. 'browser:default'."""
        data = getattr(tool_result, "data", None)
        if isinstance(data, dict):
            for key in ("session_id", "live_session", "session_key"):
                value = data.get(key)
                if value:
                    return str(value)[:255]
        return None

    async def _plan_revision(self, task: Task) -> Optional[int]:
        current = await self.planner.current_plan(task)
        return current.revision if current is not None else None

    async def _record_progress(
        self,
        task: Task,
        *,
        reason: str,
        progress: Optional[float] = None,
        next_planned_action: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Record per-step progress without a redundant status transition.

        A claimed task is already IN_PROGRESS and the state machine forbids
        IN_PROGRESS -> IN_PROGRESS, so only progress and the next planned action are updated.
        A task that is not IN_PROGRESS yet (not how the worker calls us) still goes through the
        validated transition, so validation is never bypassed.
        """
        if task.status == TaskStatus.IN_PROGRESS.value:
            await self.tasks.update_progress(
                task, progress=progress, next_planned_action=next_planned_action, note=note
            )
        else:
            await self.tasks.transition(
                task,
                TaskStatus.IN_PROGRESS,
                reason=reason,
                progress=progress,
                next_planned_action=next_planned_action,
            )

    # ------------------------------------------------------------------
    async def _ensure_plan(self, task: Task, report: LoopReport) -> ExecutionPlanSpec:
        """Load the persisted plan, or decompose one if none exists.

        Operator clarifications supplied through the Agent Inbox are folded in here, on the
        worker, rather than in the API process -- planning has exactly one home and the LLM
        is not called from a request handler. Instructions are cleared before the planner is
        invoked so a replan can never be retried forever on the same note.
        """
        instructions = self._take_operator_instructions(task)
        existing = await self.planner.load_plan_spec(task)

        # The context is the first thing established, before any decision is taken from
        # it: the goal, and what "done" means, are recorded so every later decision can
        # be read against them.
        await self.context.set_goal(
            task,
            goal=task.title or None,
            normalized_objective=task.description or task.title or None,
        )

        if existing is not None and existing.steps:
            if not instructions:
                logger.info(
                    "Task %s resuming with plan revision (%s step(s))",
                    task.task_id, len(existing.steps),
                )
                # Resuming is not a fresh start: record that this run continues an
                # interrupted one, so the context shows the history is being picked up
                # rather than rebuilt.
                await self.context.mark_resumed(task)
                return existing

            completed_irreversible = [
                s for s in existing.steps if s.kind.is_irreversible and s.status == "completed"
            ]
            if completed_irreversible:
                # Value already moved under the old plan. Changing direction now could repeat
                # or contradict it, so the clarification is left for the operator instead.
                report.blockers.append(
                    "Operator guidance received, but this task has already completed an "
                    "irreversible step; the plan was not revised automatically."
                )
                task.operator_instructions = instructions
                await self.db.flush()
                return existing

            reason = "Operator guidance received in the Agent Inbox: " + "; ".join(instructions)
            try:
                revised = await self.planner.replan(
                    task,
                    existing,
                    trigger=ReplanTrigger.OPERATOR_REQUEST,
                    reason=reason,
                )
            except PlanningError as exc:
                logger.warning(
                    "Could not apply operator guidance to task %s: %s", task.task_id, exc
                )
                report.failures.append(f"Operator guidance could not be planned: {exc}")
                task.operator_instructions = instructions
                await self.db.flush()
                return existing

            report.steps.append(
                StepResult(0, "replanned (operator request)", None, {"steps": len(revised.steps)},
                           True, reason[:300], 0)
            )
            await self._checkpoint(task, 0, "replanned_operator_request")
            return revised

        constraints = PlanConstraints(
            spent_already_usd=task.actual_cost_usd,
            deadline=task.deadline_at,
            allow_transactions=self._transactions_allowed(),
        )
        objective = task.description or task.title
        if instructions:
            objective = (
                f"{objective}\n\nOperator guidance given for this task: " + "; ".join(instructions)
            )
        spec = await self.planner.decompose(
            objective=objective,
            task=task,
            constraints=constraints,
        )
        await self.planner.persist_plan(task, spec)
        report.steps.append(
            StepResult(0, "plan created", None, {"steps": len(spec.steps)}, True,
                       spec.rationale[:300], 0)
        )
        return spec

    @staticmethod
    def _take_operator_instructions(task: Task) -> List[str]:
        """Read and clear the pending operator clarifications for this task.

        Cleared before planning runs so the same note cannot drive an unbounded number of
        replans. If planning fails the caller puts them back.
        """
        records = list(task.operator_instructions or [])
        task.operator_instructions = []
        texts: List[str] = []
        for record in records:
            text = record.get("instruction") if isinstance(record, dict) else record
            if text:
                texts.append(str(text)[:300])
        return texts

    def _transactions_allowed(self) -> bool:
        if self.policy is not None:
            return bool(self.policy.allow_transaction_submission)
        return False

    async def _resume_iteration(self, task: Task) -> int:
        latest = await self.checkpoints.load_latest(task)
        if latest is None or not CheckpointManager.should_resume(task.status):
            return 0
        return int(latest.state.get("iteration", 0))

    # ------------------------------------------------------------------
    # One iteration: execute the next ready step of the persisted plan.
    # ------------------------------------------------------------------
    async def _run_iteration(
        self, task: Task, iteration: int, report: LoopReport, plan: "ExecutionPlanSpec"
    ) -> tuple[StepResult, bool, LoopOutcome]:
        """Execute one step of the plan.

        Step selection, prerequisites, and completion are decided by the
        planner's persisted plan plus deterministic checks, not by a fresh
        ad-hoc LLM decision every iteration.
        """
        if emergency_stop.is_stopped and not emergency_stop.can_execute_tasks():
            report.blockers.append("Emergency stop engaged mid-execution.")
            await self._park_for_emergency_stop(task)
            return (
                StepResult(iteration, "halted", None, None, False, "Emergency stop", 0),
                True,
                LoopOutcome.STOPPED,
            )

        if plan.is_complete():
            return await self._finalize_plan(task, iteration, plan, report)

        # --- RECONSILE: has executing this plan revealed work it does not contain? ---
        # Checked before a step is picked, not after one finishes, so the very next action
        # already accounts for what the previous ones discovered.
        revised = await self._maybe_replan_for_discovery(task, iteration, report, plan)
        if revised is not None:
            plan = revised
            if plan.is_complete():
                return await self._finalize_plan(task, iteration, plan, report)

        ready = plan.ready_steps()
        if not ready:
            blocked = plan.blocked_steps()
            detail = "; ".join(f"step {s.index} awaits {s.depends_on}" for s in blocked[:3])
            return await self._replan(
                task,
                iteration,
                report,
                plan,
                ReplanTrigger.DEPENDENCY_UNBLOCKED,
                f"All remaining steps are dependency-blocked: {detail}",
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        step = ready[0]
        action = step.description
        reasoning = f"Plan step {step.index} ({step.kind.value})"

        # --- OBSERVE (context): why is this step the next step? ----------
        # The rationale is chosen against the whole task, not the last tool return: what
        # is still outstanding, what is already proven, and what the previous action
        # actually found. It is persisted so a resumed run, a replan or an operator can
        # see the reasoning rather than infer it.
        await self.context.set_next_step(
            task,
            rationale=await self._next_step_rationale(task, plan, step),
            step_index=step.index,
        )

        # --- OBSERVE: are this step's prerequisites actually met? --------
        satisfied, unmet = await self.planner.evaluate_prerequisites(task, step)
        if not satisfied:
            step.status = "blocked"
            step.failure_reason = f"Prerequisite not met: {unmet}"
            await self.planner.update_step_status(
                task, step.index, "blocked", failure_reason=step.failure_reason
            )
            report.blockers.append(f"Step {step.index} prerequisite not met: {unmet}")
            await self.tasks.transition(
                task,
                TaskStatus.WAITING,
                reason=f"Step {step.index} awaiting prerequisite: {unmet}",
                next_planned_action=f"Re-evaluate step {step.index} when the condition holds",
            )
            return (
                StepResult(iteration, action, step.tool, None, False, f"Prerequisite unmet: {unmet}", 0),
                True,
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        step.status = "running"
        await self.planner.update_step_status(task, step.index, "running")

        # --- ACT ---------------------------------------------------------
        if not step.tool:
            if not step.kind.is_passive:
                # No catalog tool can perform this step. It must NOT be reported as
                # done: hand it to the operator and PAUSE, so one unsupported action
                # cannot be mistaken for a completed one.
                human_gated = step.kind.requires_human
                reason = (
                    f"No catalog tool can perform this {step.kind.value} step"
                    + (" (it requires a human: CAPTCHA, OTP, login or approval). " if human_gated else "; ")
                    + f"the operator must do it manually: {action}"
                )
                step.status = "blocked"
                step.failure_reason = reason
                await self.planner.update_step_status(
                    task, step.index, "blocked", failure_reason=reason
                )
                report.blockers.append(reason)
                await self._request_operator_input(
                    task,
                    iteration=iteration,
                    error_text=reason,
                    blocking_step=f"Step {step.index} ({step.kind.value}): {action}",
                    force_reason_code=(
                        HUMAN_VERIFICATION if human_gated
                        else (AMBIGUOUS_INSTRUCTION if step.kind is StepKind.INTERACT else None)
                    ),
                )
                return (
                    StepResult(iteration, action, None, None, False, reason, 0),
                    True,
                    LoopOutcome.NEEDS_USER_ACTION,
                )

            # analyze / notify / report / wait steps need no tool, but they produce no
            # authoritative evidence either, so they are never counted as verified.
            step.status = "completed"
            await self.planner.update_step_status(
                task, step.index, "completed", result={"note": "Passive step; no evidence produced"}
            )
            await self.tasks.record_action(
                task, {"id": f"step-{step.index}", "description": action}, "completed"
            )
            return (
                StepResult(iteration, action, None, {"note": "no tool"}, False, reasoning, 0),
                False,
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        if self.registry is None:
            report.failures.append("No tool registry bound to this controller.")
            return (
                StepResult(iteration, action, step.tool, None, False, "Tool registry unavailable", 0),
                True,
                LoopOutcome.FAILED,
            )

        if step.kind is StepKind.TRANSACT and not self._transactions_allowed():
            reason = "Transaction submission is not authorized by the current policy."
            step.status = "skipped"
            step.failure_reason = reason
            await self.planner.update_step_status(task, step.index, "skipped", failure_reason=reason)
            report.failures.append(f"{action}: {reason}")
            await self.tasks.record_action(
                task, {"id": f"step-{step.index}", "description": action}, "failed", result={"error": reason}
            )
            return (
                StepResult(iteration, action, step.tool, None, False, reason, 0),
                False,
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        # --- OBSERVE (history): has this exact action already been proven? ---
        # A replan can produce a step describing work that is already done, and a resumed
        # run re-reads a plan whose steps it has executed. For an action that is not
        # idempotent - a claim, a registration, a submitted form, a transaction - running
        # it twice is a real-world consequence, not a wasted call.
        #
        # Only an authoritative confirmation suppresses the step. An attempt whose
        # outcome was never proven does not: an uncertain action is investigated, not
        # skipped and not blindly repeated.
        tool_def = self.registry.get(step.tool) if self.registry else None
        if tool_def is not None and not tool_def.is_idempotent:
            confirmed = await self.context.confirmed_fingerprint(
                task, step.tool, step.parameters or {}
            )
            if confirmed is not None:
                note = (
                    f"Already completed and verified earlier in this task "
                    f"(observation {confirmed.id}); not repeating a non-idempotent action."
                )
                logger.info("Task %s step %s: %s", task.task_id, step.index, note)
                step.status = "completed"
                step.failure_reason = None
                await self.planner.update_step_status(
                    task, step.index, "completed",
                    result={"evidence_kind": confirmed.evidence_kind, "reused_observation": str(confirmed.id)},
                )
                await self.context.record_observation(
                    task,
                    action=action,
                    outcome=OUTCOME_DUPLICATE_SUPPRESSED,
                    tool=step.tool,
                    parameters=step.parameters or {},
                    step_index=step.index,
                    iteration=iteration,
                    evidence={"authoritative": True, "kind": confirmed.evidence_kind},
                    observed_state={"suppressed": True, "original_observation": str(confirmed.id)},
                    is_idempotent=False,
                )
                await self.tasks.record_action(
                    task,
                    {"id": f"step-{step.index}", "tool": step.tool, "description": action},
                    "completed",
                    result={"skipped": "already verified", "observation": str(confirmed.id)},
                )
                report.verified_completions.append(action)
                upcoming = plan.ready_steps()
                report.next_actions = [s.description for s in upcoming[:3]]
                await self._record_progress(
                    task,
                    reason="Repeated action suppressed by confirmed history",
                    progress=self._plan_progress(plan),
                    next_planned_action=(
                        upcoming[0].description if upcoming else "All plan steps processed"
                    ),
                )
                return (
                    StepResult(iteration, action, step.tool, {"already_verified": True}, True, note, 0),
                    False,
                    LoopOutcome.PARTIALLY_COMPLETED,
                )

        tool_result = await self.registry.invoke(
            step.tool, step.parameters, policy=self.policy, task_id=task.task_id
        )

        if tool_result.status is not ToolResultStatus.SUCCESS:
            await self.context.record_observation(
                task,
                action=action,
                outcome=(
                    OUTCOME_BLOCKED
                    if tool_result.status is ToolResultStatus.NEEDS_USER_ACTION
                    else OUTCOME_FAILED
                ),
                tool=step.tool,
                parameters=step.parameters or {},
                step_index=step.index,
                iteration=iteration,
                evidence=tool_result.evidence,
                result_digest={"error": tool_result.error, "category": tool_result.error_category},
                integration_ref=self._integration_ref(tool_result),
                is_idempotent=bool(getattr(tool_def, "is_idempotent", True)),
                error=tool_result.error,
            )
            diagnosis = self.diagnosis.diagnose(
                tool_result.error or "Tool returned a non-success status.",
                error_category=tool_result.error_category,
                attempt=task.retry_count,
            )
            await self.diagnosis.record(
                self.db,
                task,
                diagnosis,
                diagnostics={"tool": step.tool, "status": tool_result.status.value},
                retry_count=task.retry_count,
            )
            return await self._handle_tool_failure(
                task, iteration, report, plan, step, tool_result, diagnosis
            )

        # --- VERIFY: an intent to verify is not verification --------------
        verified, evidence = await self._verify_step(task, step, tool_result)

        # --- RECORD: what did this action actually teach us? ---------------
        # Before choosing the next action, the result is written into the task's context:
        # what the external system now looks like, and which requirements it revealed.
        # The next action is then chosen against this, not against the raw tool return.
        await self._absorb_result(
            task, step, tool_result, verified=verified, iteration=iteration, plan=plan
        )

        if verified:
            step.status = "completed"
            await self.planner.update_step_status(task, step.index, "completed", result=tool_result.data)
            await self.tasks.record_action(
                task,
                {"id": f"step-{step.index}", "tool": step.tool, "description": action},
                "completed",
                result=tool_result.data,
            )
            report.verified_completions.append(action)
            upcoming = plan.ready_steps()
            report.next_actions = [s.description for s in upcoming[:3]]
        else:
            # The tool SUCCEEDED but its evidence did not satisfy the declared method.
            # Marking this "failed" is exactly what stranded the workflow: `ready_steps`
            # counts only completed/skipped as done, so every dependent step stayed
            # blocked forever and the run ended after this single action -- the reported
            # "joins the group, then stops" defect.
            #
            # The honest classification is UNVERIFIED, not FAILED: the action ran, we
            # simply cannot prove it. The step stays re-runnable, and the loop CONTINUES
            # so a following verification step can still establish the fact independently.
            step.status = "unverified"
            step.failure_reason = (
                "The tool succeeded but its evidence did not satisfy the declared "
                "verification method, so the outcome is UNVERIFIED (not proven, not failed)."
            )
            await self.planner.update_step_status(
                task, step.index, "unverified", result=tool_result.data,
                failure_reason=step.failure_reason,
            )
            report.unverified_claims.append(action)
            logger.info(
                "Task %s step %s: the action succeeded but is UNVERIFIED. Continuing the plan "
                "so a later step can prove it, instead of stranding every dependent step.",
                task.task_id, step.index,
            )

        task.actual_cost_usd = round(task.actual_cost_usd + step.estimated_cost_usd, 6)

        upcoming = plan.ready_steps()
        await self._record_progress(
            task,
            reason="Plan step processed",
            progress=self._plan_progress(plan),
            next_planned_action=upcoming[0].description if upcoming else "All plan steps processed",
        )

        return (
            StepResult(iteration, action, step.tool, tool_result.data, verified, reasoning, 0),
            False,
            LoopOutcome.PARTIALLY_COMPLETED,
        )

    # ------------------------------------------------------------------
    # Context: absorbing what an action taught us, and choosing the next one from it
    # ------------------------------------------------------------------
    async def _absorb_result(
        self,
        task: Task,
        step,
        tool_result,
        *,
        verified: bool,
        iteration: int,
        plan: "ExecutionPlanSpec",
    ) -> List[str]:
        """Write one action's outcome into the task context and return new requirements.

        This is the update the specification requires after every meaningful action
        (§5.2). It separates three things that are easy to conflate and expensive to
        conflate:

        * the tool result - what the call returned,
        * the observed external state - what the system looks like as a result,
        * the requirements revealed - what the system is now saying must be done.

        Only the third can change the plan, and only after it has been checked against
        what the plan already covers.
        """
        tool_def = self.registry.get(step.tool) if self.registry else None
        is_idempotent = bool(getattr(tool_def, "is_idempotent", True))

        outcome = OUTCOME_CONFIRMED if verified else OUTCOME_UNVERIFIED
        extracted = await extract_from_result(
            tool_result.data or {},
            budget=self.discovery_budget,
            context_hint=f"{task.title or ''} {step.description}",
        )

        observation = await self.context.record_observation(
            task,
            action=step.description,
            outcome=outcome,
            tool=step.tool,
            parameters=step.parameters or {},
            step_index=step.index,
            iteration=iteration,
            evidence=tool_result.evidence,
            observed_state=extracted.observed_state,
            result_digest=tool_result.data,
            integration_ref=self._integration_ref(tool_result),
            is_idempotent=is_idempotent,
        )

        if extracted.reference_values:
            await self.context.merge_resource_refs(task, extracted.reference_values)

        if extracted.suspicious_content:
            # External content tried to instruct the agent. It is recorded as a fact
            # about the environment and as an outstanding question, and it is never
            # turned into a step.
            await self.context.add_question(
                task,
                "External content contained an instruction aimed at the agent; "
                "it was not acted on. Review before continuing.",
            )
            logger.warning(
                "Task %s: external content attempted to instruct the agent: %s",
                task.task_id, extracted.suspicious_content[:200],
            )

        discovered: List[str] = []
        for text in extracted.requirements:
            requirement = await self.context.record_requirement(
                task,
                text,
                origin="tool_result",
                source_ref=f"{step.tool or 'step'}#{step.index}",
            )
            if requirement is not None and requirement.status != "satisfied":
                discovered.append(requirement.text)

        # Newly discovered requirements are only worth a replan when the current plan has
        # nothing aimed at them. Otherwise the next ready step is already the right one and
        # spending a model call to rediscover that would be waste.
        if discovered:
            unaddressed = await self.context.unaddressed_requirements(
                task, [s.description for s in plan.steps]
            )
            if unaddressed:
                logger.info(
                    "Task %s: executing step %s revealed %d requirement(s) the plan does "
                    "not address: %s",
                    task.task_id, step.index, len(unaddressed),
                    "; ".join(r.text[:80] for r in unaddressed[:3]),
                )
        return discovered

    async def _maybe_replan_for_discovery(
        self,
        task: Task,
        iteration: int,
        report: LoopReport,
        plan: "ExecutionPlanSpec",
    ) -> Optional[ExecutionPlanSpec]:
        """Revise the plan when executing it revealed requirements nothing addresses.

        Budgeted separately from failure replanning: discovering that a bot wants a
        channel joined is the system working, not the system struggling, and must not
        consume the retry allowance or trip the failure-replan ceiling.
        """
        if self.discovery_replans >= settings.MAX_DISCOVERY_REPLANS_PER_RUN:
            return None

        outstanding = await self.context.requirements(task, only_outstanding=True)
        if not outstanding:
            return None

        unaddressed = await self.context.unaddressed_requirements(
            task, [s.description for s in plan.steps]
        )
        if not unaddressed:
            return None  # the plan already covers everything outstanding

        confirmed = [
            {"action": o.action, "outcome": o.outcome}
            for o in await self.context.observations(task, limit=None)
            if o.proved_completion
        ]

        try:
            revised = await self.planner.replan(
                task,
                plan,
                trigger=ReplanTrigger.DISCOVERY,
                reason=(
                    f"Executing the plan revealed {len(unaddressed)} requirement(s) that no "
                    f"current step addresses: "
                    + "; ".join(r.text[:120] for r in unaddressed[:5])
                ),
                context_notes=[r.text for r in unaddressed],
                evidence=confirmed,
            )
        except PlanningError as exc:
            logger.warning("Discovery replan for %s failed: %s", task.task_id, exc)
            return None

        self.discovery_replans += 1
        await self.context.set_next_step(
            task,
            rationale=(
                f"Revised the plan because executing it revealed requirements the plan did "
                f"not contain: {'; '.join(r.text[:120] for r in unaddressed[:3])}"
            ),
        )
        await self._checkpoint(task, iteration, "replanned_discovery")
        report.steps.append(
            StepResult(
                iteration, "replanned (discovery)", None,
                {"revision_steps": len(revised.steps)}, True,
                revised.rationale[:300], 0,
            )
        )
        logger.info(
            "Task %s: plan revision now covers %d newly discovered requirement(s)",
            task.task_id, len(unaddressed),
        )
        return revised

    # ------------------------------------------------------------------
    async def _count_recovery_replans(self, task: Task) -> int:
        """How many revisions of this plan were recovery replans rather than discoveries.

        Read from the persisted revision history rather than the in-memory counter so it
        survives a restart: a task that has already spent its recovery budget must not get
        a fresh one because the worker bounced.

        INITIAL is excluded: producing the first plan is not a replan. DISCOVERY is
        excluded for the reason above.
        """
        from sqlalchemy import func, select as sa_select

        from backend.app.models import PlanRevision

        recovery_triggers = [
            trigger.value
            for trigger in ReplanTrigger
            if trigger is not ReplanTrigger.DISCOVERY and trigger is not ReplanTrigger.INITIAL
        ]
        return int(
            await self.db.scalar(
                sa_select(func.count(PlanRevision.id)).where(
                    PlanRevision.task_id == task.id,
                    PlanRevision.trigger_category.in_(recovery_triggers),
                )
            )
            or 0
        )

    # ------------------------------------------------------------------
    async def _next_step_rationale(
        self, task: Task, plan: "ExecutionPlanSpec", step
    ) -> str:
        """Explain, from the whole task, why this step is next.

        Deliberately not derived from the last tool return alone: the choice has to be
        readable against the objective, what is already proven, and what is outstanding.
        """
        outstanding = await self.context.requirements(task, only_outstanding=True)
        proven = [
            o for o in await self.context.observations(task, limit=None) if o.proved_completion
        ]
        parts = [
            f"Step {step.index} of {len(plan.steps)} ({step.kind.value}) is the earliest "
            f"step whose dependencies are met."
        ]
        if step.depends_on:
            parts.append(f"It depends on step(s) {step.depends_on}, now satisfied.")
        if proven:
            parts.append(f"{len(proven)} action(s) already proven in this task.")
        if outstanding:
            parts.append(
                f"{len(outstanding)} requirement(s) still outstanding: "
                + "; ".join(r.text[:80] for r in outstanding[:3])
            )
        return " ".join(parts)[:1900]

    # ------------------------------------------------------------------
    @staticmethod
    def _plan_progress(plan: "ExecutionPlanSpec") -> float:
        if not plan.steps:
            return 0.0
        # An unverified step still consumed a real action, so it counts toward progress.
        # Counting only completed/skipped made progress stall at a flat value while the
        # agent was demonstrably working, which reads as "stuck" in the UI.
        done = sum(1 for s in plan.steps if s.status in ("completed", "skipped", "unverified"))
        return round(min(0.99, done / len(plan.steps)), 4)

    # ------------------------------------------------------------------
    async def _handle_tool_failure(
        self,
        task: Task,
        iteration: int,
        report: LoopReport,
        plan: "ExecutionPlanSpec",
        step,
        tool_result,
        diagnosis,
    ) -> tuple[StepResult, bool, LoopOutcome]:
        """Apply the deterministic recovery decision for a failed step."""
        action = step.description
        report.failures.append(f"{step.tool}: {tool_result.error}")

        await self.tasks.record_action(
            task,
            {"id": f"step-{step.index}", "tool": step.tool, "description": action},
            "failed",
            result={"error": tool_result.error, "category": diagnosis.category.value},
        )

        # A broken subsystem must stop receiving work.
        if tool_result.status is ToolResultStatus.UNAVAILABLE and self.registry:
            self.registry.disable(step.tool, tool_result.error or "unavailable")

        if tool_result.status is ToolResultStatus.NEEDS_USER_ACTION or diagnosis.requires_human:
            reason = tool_result.error or diagnosis.summary
            report.blockers.append(reason)
            step.status = "blocked"
            step.failure_reason = reason
            await self.planner.update_step_status(task, step.index, "blocked", failure_reason=reason)
            await self._request_operator_input(
                task,
                iteration=iteration,
                error_text=reason,
                error_category=tool_result.error_category or diagnosis.category.value,
                blocking_step=f"Step {step.index} ({step.tool}): {action}",
                integration_ref=self._integration_ref(tool_result),
            )
            return (
                StepResult(iteration, action, step.tool, tool_result.data, False, reason, 0),
                True,
                LoopOutcome.NEEDS_USER_ACTION,
            )

        if tool_result.status is ToolResultStatus.NOT_AUTHORIZED:
            step.status = "skipped"
            step.failure_reason = tool_result.error or "not authorized"
            # The reason a call was refused is part of the task's record: a later step, a
            # replan or an operator needs to know this was policy, not a broken tool.
            await self.context.record_authorization(
                task,
                permission=step.tool or "unknown",
                granted=False,
                detail=tool_result.error or "not authorized",
            )
            await self.planner.update_step_status(
                task, step.index, "skipped", failure_reason=step.failure_reason
            )
            return await self._replan(
                task,
                iteration,
                report,
                plan,
                ReplanTrigger.FAILURE,
                f"Step {step.index} was not authorized: {tool_result.error}",
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        # Retry is decided by the planner's deterministic policy, not the model.
        should_retry, retry_reason = self.planner.decide_retry(
            step, attempt=task.retry_count, failure_category=diagnosis.category.value
        )
        await self.context.record_failure(
            task,
            error=tool_result.error or diagnosis.summary,
            category=diagnosis.category.value,
            recovery=diagnosis.recovery_strategy,
        )
        within_budget = task.retry_count < min(task.max_retries, settings.MAX_TASK_RETRIES)

        if should_retry and within_budget:
            task.next_execution_at = next_retry_at(diagnosis.backoff_seconds or 5)
            await self.tasks.transition(
                task,
                TaskStatus.RETRYING,
                reason=diagnosis.summary,
                next_planned_action=f"Retry step {step.index}: {retry_reason}",
                error_details={
                    "error": tool_result.error,
                    "category": diagnosis.category.value,
                    "strategy": diagnosis.recovery_strategy,
                },
            )
            await self._checkpoint(task, iteration, "retry_scheduled")
            return (
                StepResult(
                    iteration, action, step.tool, tool_result.data, False,
                    f"{diagnosis.recovery_strategy} {retry_reason}", 0,
                ),
                True,
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        step.status = "failed"
        step.failure_reason = diagnosis.summary
        await self.planner.update_step_status(
            task, step.index, "failed", failure_reason=step.failure_reason
        )
        return await self._replan(
            task,
            iteration,
            report,
            plan,
            ReplanTrigger.FAILURE,
            f"Step {step.index} failed permanently: {diagnosis.summary}. {diagnosis.recovery_strategy}",
            LoopOutcome.PARTIALLY_COMPLETED,
        )

    # ------------------------------------------------------------------
    async def _replan(
        self,
        task: Task,
        iteration: int,
        report: LoopReport,
        plan: "ExecutionPlanSpec",
        trigger: ReplanTrigger,
        reason: str,
        outcome: LoopOutcome,
    ) -> tuple[StepResult, bool, LoopOutcome]:
        """Ask the planner for a new revision and continue with it."""
        # Every replan is an LLM call. Cap them so one task cannot burn calls forever.
        #
        # The cap counts RECOVERY replans only. Discovery replans
        # (`ReplanTrigger.DISCOVERY`) advance the same revision counter but are progress,
        # not recovery, and are bounded separately by
        # MAX_DISCOVERY_REPLANS_PER_RUN. Counting them here would mean a task that
        # legitimately discovered three new requirements could no longer replan after a
        # genuine failure - the budget for fixing things would be spent on learning about
        # them.
        recovery_replans = await self._count_recovery_replans(task)
        if recovery_replans >= MAX_REPLANS_PER_TASK:
            msg = (
                f"Replan limit ({MAX_REPLANS_PER_TASK}) reached. Stopping instead of "
                f"spending more LLM calls. Last problem: {reason[:200]}"
            )
            report.blockers.append(msg)
            await self.tasks.transition(
                task, TaskStatus.PARTIALLY_COMPLETED, reason=msg,
                next_planned_action="Operator review needed",
            )
            return (
                StepResult(iteration, "replan limit reached", None, None, False, msg, 0),
                True,
                LoopOutcome.PARTIALLY_COMPLETED,
            )

        # Budget pressure is a distinct trigger worth reporting accurately.
        if trigger is ReplanTrigger.FAILURE and plan.total_cost() > settings.MAX_TRANSACTION_VALUE_USD:
            trigger = ReplanTrigger.BUDGET

        try:
            await self.planner.replan(task, plan, trigger=trigger, reason=reason)
        except PlanningError as exc:
            logger.error("Replanning failed for %s: %s", task.task_id, exc)
            report.failures.append(f"Replanning failed: {exc}")
            await self.tasks.transition(
                task,
                TaskStatus.PARTIALLY_COMPLETED,
                reason=f"Replanning failed: {exc}",
                error_details={"replan_error": str(exc)},
            )
            return (
                StepResult(iteration, "replan", None, None, False, str(exc), 0),
                True,
                LoopOutcome.FAILED,
            )

        new_plan = await self.planner.load_plan_spec(task)
        report.steps.append(
            StepResult(
                iteration,
                f"replanned ({trigger.value})",
                None,
                {"revision_steps": len(new_plan.steps) if new_plan else 0},
                True,
                (new_plan.rationale if new_plan else reason)[:300],
                0,
            )
        )
        await self._checkpoint(task, iteration, f"replanned_{trigger.value.lower()}")
        upcoming = new_plan.ready_steps() if new_plan else []
        await self._record_progress(
            task,
            reason=f"Replanned: {reason[:300]}",
            next_planned_action=upcoming[0].description if upcoming else "No executable steps remain",
            note=f"Replanned: {reason[:300]}",
        )
        return (
            StepResult(iteration, f"replanned ({trigger.value})", None, None, False, reason[:300], 0),
            False,
            outcome,
        )

    # ------------------------------------------------------------------
    async def _finalize_plan(
        self, task: Task, iteration: int, plan: "ExecutionPlanSpec", report: LoopReport
    ) -> tuple[StepResult, bool, LoopOutcome]:
        """All plan steps are processed. Verify the whole objective honestly."""
        # `unverified` is a distinct outcome: the action ran but its evidence did not
        # prove it. It must never be reported as COMPLETED, and it must not be silently
        # ignored either.
        unverified = [s for s in plan.steps if s.status in ("failed", "unverified")]
        skipped = [s for s in plan.steps if s.status == "skipped"]
        blocked = [s for s in plan.steps if s.status == "blocked"]
        # Only steps that ran a real tool and passed verification count as evidence.
        verified_count = sum(1 for s in plan.steps if s.status == "completed" and s.tool)

        # Requirements discovered while executing that nothing proved. They are named in
        # the outcome because "the plan ran out of steps" and "the campaign still needs X"
        # are different situations, and only the second tells the operator what is left.
        outstanding = await self.context.requirements(task, only_outstanding=True)

        if not unverified and not skipped and not blocked and verified_count > 0:
            await self.tasks.transition(
                task,
                TaskStatus.COMPLETED,
                reason="All plan steps completed and verified",
                verified=True,
                evidence_count=verified_count,
                progress=1.0,
                next_planned_action=None,
            )
            return (
                StepResult(
                    iteration, "plan complete", None, {"verified": verified_count}, True,
                    "Every plan step produced authoritative evidence.", 0,
                ),
                True,
                LoopOutcome.COMPLETED,
            )

        if verified_count == 0 and not (unverified or skipped or blocked):
            report.unverified_claims.append(
                "No tool-backed step produced evidence, so nothing was actually verified."
            )

        # Anything skipped, blocked, or unverified means the objective is NOT complete.
        report.unverified_claims.extend(s.description for s in unverified + skipped)
        if skipped:
            report.failures.append(
                f"{len(skipped)} step(s) skipped by policy constraints: "
                + "; ".join(s.description for s in skipped[:3])
            )
        if blocked:
            report.blockers.extend(s.description for s in blocked[:3])
        if outstanding:
            report.blockers.extend(
                f"Outstanding requirement discovered while executing: {r.text}"
                for r in outstanding[:3]
            )

        reason = (
            f"{len(plan.steps)} step(s) processed but {len(unverified)} unverified, "
            f"{len(skipped)} skipped, {len(blocked)} blocked. The objective is not fully verified."
        )
        if outstanding:
            reason += (
                f" {len(outstanding)} discovered requirement(s) remain unsatisfied: "
                + "; ".join(r.text[:120] for r in outstanding[:3])
            )

        await self.tasks.transition(
            task,
            TaskStatus.PARTIALLY_COMPLETED,
            reason=reason,
            verified=False,
            evidence_count=verified_count,
            progress=self._plan_progress(plan),
            next_planned_action="Review skipped, blocked, and unverified steps; enable required permissions to proceed.",
        )
        return (
            StepResult(
                iteration, "plan finished incomplete", None,
                {"unverified": len(unverified), "skipped": len(skipped), "blocked": len(blocked)},
                False, "Plan processed but the objective could not be fully verified.", 0,
            ),
            True,
            LoopOutcome.PARTIALLY_COMPLETED,
        )

    # ------------------------------------------------------------------
    async def _verify_step(self, task: Task, step, tool_result) -> tuple[bool, int]:
        """Verify one plan step against its declared verification method.

        The planner declares how each step must be proven; this checks whether the
        evidence actually satisfies that declaration.

        The comparison that was missing: the declared `verification_method` was never
        matched against the kind of evidence the tool actually produced, so any
        `authoritative` evidence satisfied any method. A step promising
        `chain_receipt` that ran `simulate_transaction` (which reports
        `kind="eth_call_simulation"`) counted as verified, and a fully "verified"
        plan could then be marked COMPLETED on evidence that proves nothing happened.
        `EVIDENCE_KINDS` maps each concrete method to the evidence kinds that can
        satisfy it.
        """
        method = (step.verification_method or "").strip().lower()
        evidence = getattr(tool_result, "evidence", None)
        data = tool_result.data or {}

        if evidence and evidence.get("authoritative"):
            kind = str(evidence.get("kind") or "").strip().lower()
            if method in GENERIC_EVIDENCE_METHODS:
                # The step only asked for "the tool returned evidence".
                return True, 1
            if method == "unspecified":
                # The planner's default means nobody declared how this is proven.
                # Treating that as satisfied is exactly how unverified work gets
                # reported as done, so it is never auto-verified.
                return False, 0
            allowed = EVIDENCE_KINDS.get(method)
            if allowed is None:
                # An unrecognised method is free text the planner invented. Nobody has
                # said what it was trying to prove, so authoritative evidence of *some*
                # kind is not evidence of *this*.
                #
                # This used to return True, which meant a step promising
                # "transaction_confirmed" could be satisfied by a page observation. The
                # safe default is the same one `unspecified` already gets: unverified.
                # It costs a report that says "we could not prove this", and it prevents
                # a report that says "done" on the strength of unrelated evidence.
                logger.info(
                    "Task %s step %s: verification method %r is not a recognised one. "
                    "Treated as UNVERIFIED rather than assuming any authoritative evidence "
                    "satisfies it. Add it to EVIDENCE_KINDS if it is a real method.",
                    task.task_id, step.index, method,
                )
                return False, 0
            # Cross-integration equivalence: some methods describe a FACT rather than
            # an integration (e.g. "the remote state now says X"). A Telegram read and
            # a page observation are both authoritative observations of remote state,
            # so either may satisfy the other's method.
            allowed = allowed | EQUIVALENT_EVIDENCE_KINDS.get(method, frozenset())
            if kind in allowed:
                return True, 1
            logger.info(
                "Task %s step %s: declared verification method %r was NOT satisfied. "
                "The tool produced %r evidence, which proves something different.",
                task.task_id, step.index, method, kind or "(none)",
            )
            return False, 0

        # Tools that predate the evidence envelope still expose their proof in the
        # payload. Kept, but only for a method that actually names that proof.
        #
        # ONLY for a tool that returned no evidence block at all. A tool that DID
        # return one and set `authoritative: False` has explicitly said it cannot
        # prove the outcome -- typically because the transaction is submitted but
        # not yet mined, or the receipt could not be read. Falling through to the
        # payload fallback for those let a bare tx_hash mark a pending transfer
        # verified, which is the one thing this whole function exists to prevent.
        if not evidence:
            if data.get("tx_hash") and method in ("chain_receipt", "tx_hash", "receipt"):
                return True, 1
            if data.get("screenshot_path") and method in ("screenshot", "page_observation"):
                return True, 1
            if data.get("expected_text_seen") is True:
                return True, 1

        if method and method not in GENERIC_EVIDENCE_METHODS:
            logger.info(
                "Task %s step %s: expected evidence %r was not satisfied by the tool result.",
                task.task_id, step.index, method,
            )
        return False, 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _checkpoint(self, task: Task, iteration: int, name: str) -> None:
        try:
            plan = await self.planner.current_plan(task)
            await self.checkpoints.save(
                task,
                checkpoint_name=name,
                state=CheckpointManager.execution_snapshot(
                    iteration=iteration,
                    completed_action_ids=[
                        a.get("id") for a in (task.completed_actions or []) if a.get("id")
                    ],
                    pending_actions=task.planned_actions or [],
                    observations={
                        "last_status": task.status,
                        # Persisting plan position is what makes resume exact
                        # rather than approximate.
                        "plan_revision": plan.revision if plan else None,
                        "plan_steps": plan.steps if plan else [],
                    },
                ),
                iteration=iteration,
            )
        except Exception as exc:  # Checkpointing must never mask the real error.
            logger.error("Failed to checkpoint task %s: %s", task.task_id, exc)

    async def _apply_final_status(self, task: Task, report: LoopReport) -> None:
        try:
            if report.next_actions:
                task.next_planned_action = "; ".join(report.next_actions[:3])
            await self.db.flush()
        except Exception as exc:
            logger.error("Failed to finalize task %s: %s", task.task_id, exc)
        await self._report_to_inbox(task, report)

    # ------------------------------------------------------------------
    async def _report_to_inbox(self, task: Task, report: LoopReport) -> None:
        """Tell the task's conversation what this run actually did.

        Only reported for tasks that already have an Agent Inbox conversation, so ordinary
        tasks do not acquire one just because they ran. Nothing here claims more than the
        report proves: unverified work is named as unverified.
        """
        from backend.app.inbox import events
        from backend.app.inbox.service import ConversationService

        try:
            conversations = ConversationService(self.db)
            conversation = await conversations.get_for_task(task.id, task.owner_email or "")
            if conversation is None:
                return

            lines = [f"Task {task.task_id} run finished: {report.outcome.value.replace('_', ' ').lower()}."]
            if report.verified_completions:
                lines.append(f"Verified complete: {len(report.verified_completions)} item(s).")
            if report.unverified_claims:
                lines.append(
                    f"Not verified: {len(report.unverified_claims)} claim(s) produced no evidence."
                )
            if report.failures:
                lines.append(f"Failures: {report.failures[0]}")
            if report.blockers:
                lines.append(f"Still blocked: {report.blockers[0]}")
            if report.next_actions:
                lines.append(f"Next: {report.next_actions[0]}")

            message = await conversations.append_message(
                conversation,
                role="assistant",
                content="\n".join(lines),
                kind="AGENT",
            )
            await self.db.flush()

            event = {
                LoopOutcome.COMPLETED: events.EV_TASK_COMPLETED,
                LoopOutcome.FAILED: events.EV_TASK_FAILED,
                LoopOutcome.NEEDS_USER_ACTION: events.EV_INTERVENTION_CREATED,
            }.get(report.outcome, events.EV_TASK_PROGRESS)
            await events.publish(
                event,
                task_id=task.task_id,
                conversation_id=str(conversation.id),
                message_id=str(message.id),
                summary=lines[0],
                status=task.status,
            )
        except Exception as exc:  # never let reporting mask the run's own result
            logger.debug("Could not report run outcome to the inbox for %s: %s", task.task_id, exc)


def json_dumps(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, default=str)[:4000]
    except Exception:
        return str(obj)[:2000]
