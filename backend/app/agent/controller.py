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

from backend.app.core.config import settings
from backend.app.llm.client import LLMError, Message, llm
from backend.app.models import Task
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

from backend.app.agent.prompts import AGENT_SYSTEM_PROMPT as SYSTEM_PROMPT  # noqa: E402


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
            return report

        # Take an execution lease so a dead worker's task is recoverable and a
        # live duplicate cannot execute the same task concurrently.
        lease = None
        worker_id = f"controller-{id(self):x}"
        try:
            lease = await self.leases.acquire(task, worker_id)
        except Exception as exc:
            logger.warning("Could not acquire lease for %s: %s", task.task_id, exc)
            report.outcome = LoopOutcome.STOPPED
            report.blockers.append(f"Another worker holds this task: {exc}")
            return report

        try:
            plan_spec = await self._ensure_plan(task, report)
            iteration = await self._resume_iteration(task)
            if iteration:
                report.steps.append(
                    StepResult(iteration, "resumed from checkpoint", None, None, True,
                               "Restored prior state", 0)
                )

            while iteration < budget:
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
            else:
                # Loop exhausted its budget without reaching a terminal state.
                report.outcome = LoopOutcome.BUDGET_EXHAUSTED
                report.blockers.append(
                    f"Reached the {budget}-iteration limit without completing. "
                    "Work is checkpointed and resumable."
                )
                await self._checkpoint(task, iteration, "budget_exhausted")

            report.duration_ms = int((time.perf_counter() - started) * 1000)
            await self._apply_final_status(task, report)
            return report
        finally:
            await self.leases.release(task, lease.lease_token)

    # ------------------------------------------------------------------
    async def _ensure_plan(self, task: Task, report: LoopReport) -> ExecutionPlanSpec:
        """Load the persisted plan, or decompose one if none exists."""
        existing = await self.planner.load_plan_spec(task)
        if existing is not None and existing.steps:
            logger.info(
                "Task %s resuming with plan revision (%s step(s))",
                task.task_id, len(existing.steps),
            )
            return existing

        constraints = PlanConstraints(
            spent_already_usd=task.actual_cost_usd,
            deadline=task.deadline_at,
            allow_transactions=self._transactions_allowed(),
        )
        objective = task.description or task.title
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
            return (
                StepResult(iteration, "halted", None, None, False, "Emergency stop", 0),
                True,
                LoopOutcome.STOPPED,
            )

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
            # analyze / notify / wait steps need no tool invocation.
            step.status = "completed"
            await self.planner.update_step_status(
                task, step.index, "completed", result={"note": "Completed without a tool call"}
            )
            await self.tasks.record_action(
                task, {"id": f"step-{step.index}", "description": action}, "completed"
            )
            report.verified_completions.append(action)
            return (
                StepResult(iteration, action, None, {"note": "no tool"}, True, reasoning, 0),
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

        tool_result = await self.registry.invoke(
            step.tool, step.parameters, policy=self.policy, task_id=task.task_id
        )

        if tool_result.status is not ToolResultStatus.SUCCESS:
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
            step.status = "failed"
            step.failure_reason = "Tool returned success but produced no authoritative evidence."
            await self.planner.update_step_status(
                task, step.index, "failed", failure_reason=step.failure_reason
            )
            report.unverified_claims.append(action)
            await self.diagnosis.record(
                self.db,
                task,
                self.diagnosis.diagnose(step.failure_reason, error_category="VERIFICATION_FAILURE"),
            )

        task.actual_cost_usd = round(task.actual_cost_usd + step.estimated_cost_usd, 6)

        upcoming = plan.ready_steps()
        await self.tasks.transition(
            task,
            TaskStatus.IN_PROGRESS,
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
    @staticmethod
    def _plan_progress(plan: "ExecutionPlanSpec") -> float:
        if not plan.steps:
            return 0.0
        done = sum(1 for s in plan.steps if s.status in ("completed", "skipped"))
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
            await self.tasks.transition(
                task,
                TaskStatus.NEEDS_USER_ACTION,
                reason=reason,
                next_planned_action="Resume when the blocking condition is resolved",
            )
            return (
                StepResult(iteration, action, step.tool, tool_result.data, False, reason, 0),
                True,
                LoopOutcome.NEEDS_USER_ACTION,
            )

        if tool_result.status is ToolResultStatus.NOT_AUTHORIZED:
            step.status = "skipped"
            step.failure_reason = tool_result.error or "not authorized"
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
        await self.tasks.transition(
            task,
            TaskStatus.IN_PROGRESS,
            reason=f"Replanned: {reason[:300]}",
            next_planned_action=upcoming[0].description if upcoming else "No executable steps remain",
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
        unverified = [s for s in plan.steps if s.status == "failed"]
        skipped = [s for s in plan.steps if s.status == "skipped"]
        blocked = [s for s in plan.steps if s.status == "blocked"]
        verified_count = sum(1 for s in plan.steps if s.status == "completed")

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

        # Anything skipped, blocked, or unverified means the objective is NOT complete.
        report.unverified_claims.extend(s.description for s in unverified + skipped)
        if skipped:
            report.failures.append(
                f"{len(skipped)} step(s) skipped by policy constraints: "
                + "; ".join(s.description for s in skipped[:3])
            )
        if blocked:
            report.blockers.extend(s.description for s in blocked[:3])

        await self.tasks.transition(
            task,
            TaskStatus.PARTIALLY_COMPLETED,
            reason=(
                f"{len(plan.steps)} step(s) processed but {len(unverified)} unverified, "
                f"{len(skipped)} skipped, {len(blocked)} blocked. The objective is not fully verified."
            ),
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

        The planner declares how each step must be proven; this checks whether
        the evidence actually satisfies that declaration. A step whose method
        is `unspecified` is never auto-verified.
        """
        method = (step.verification_method or "").strip().lower()
        evidence = getattr(tool_result, "evidence", None)

        if evidence and evidence.get("authoritative"):
            if method and "unspecified" not in method:
                return True, 1
            return method in ("", "tool_evidence", "evidence_present"), 1

        data = tool_result.data or {}
        if data.get("tx_hash") and method in ("chain_receipt", "tx_hash", "receipt"):
            return True, 1
        if data.get("screenshot_path") and method in ("screenshot", "page_observation"):
            return True, 1
        if data.get("expected_text_seen") is True:
            return True, 1

        if method and method not in ("unspecified", "tool_evidence"):
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


def json_dumps(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, default=str)[:4000]
    except Exception:
        return str(obj)[:2000]
