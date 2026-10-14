"""Controlled Self-Improvement & Self-Coding Workflow (Specification Part 27.2).

Generated code is NEVER executed, deployed, or merged automatically. It lands in
QUARANTINED status with a defined validation requirement and a rollback plan.
Validation commands are recorded but NOT run during this phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.llm.client import LLMError, Message, llm
from backend.app.models import ImprovementProposal

logger = logging.getLogger(__name__)

# Files the self-coding workflow is never permitted to modify, regardless of prompt.
PROTECTED_PATHS = {
    "app/security/",
    "app/wallets/vault.py",
    "app/llm/client.py",
    "app/tools/registry.py",
    "app/core/config.py",
}

GENERATOR_SYSTEM_PROMPT = """You are a senior engineer proposing a minimal, surgical improvement
to the AirdropOS codebase.

Rules:
- Propose the SMALLEST change that solves the stated problem.
- Never weaken, remove, or bypass any security control, permission check, spend limit,
  verification gate, or emergency stop.
- Never modify protected files.
- State the required tests that would validate the change. These are recorded, not run now.
- Output strict JSON only.
"""


@dataclass
class CodeProposal:
    title: str
    problem: str
    root_cause: str
    proposed_change: str
    affected_files: List[str] = field(default_factory=list)
    required_tests: List[str] = field(default_factory=list)
    security_review_required: bool = True
    rollback_plan: str = ""


class SelfImprovementEngine:
    """Generates, tracks, and governs self-generated code changes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    @staticmethod
    def is_protected(path: str) -> bool:
        normalized = path.replace("\\", "/")
        return any(normalized.startswith(p) or p in normalized for p in PROTECTED_PATHS)

    async def propose(
        self,
        problem: str,
        logs_summary: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> ImprovementProposal:
        """Ask the single fixed LLM for a change proposal, then govern it."""
        user = f"""Problem:
{problem}

Relevant log context (non-sensitive):
{logs_summary or "(none provided)"}

Additional context:
{context or {}}

Respond with EXACTLY this JSON:
{{
  "title": "<short title>",
  "root_cause": "<analysis>",
  "proposed_change": "<the specific edit>",
  "affected_files": ["path/one.py"],
  "required_tests": ["test name or assertion"],
  "rollback_plan": "<how to revert>"
}}
"""
        try:
            result = await llm.complete_json(
                [Message("system", GENERATOR_SYSTEM_PROMPT), Message("user", user)]
            )
        except LLMError as exc:
            logger.error("Self-coding proposal failed: %s", exc)
            proposal = CodeProposal(
                title="Proposal generation failed",
                problem=problem,
                root_cause=f"Fixed LLM error: {exc}",
                proposed_change="(none generated)",
                affected_files=[],
            )
            result = {
                "title": proposal.title,
                "root_cause": proposal.root_cause,
                "proposed_change": proposal.proposed_change,
                "affected_files": [],
                "required_tests": [],
                "rollback_plan": "N/A",
            }

        affected = [str(f) for f in (result.get("affected_files") or [])]
        blocked_files = [f for f in affected if self.is_protected(f)]

        # If the proposal touches protected code, it is recorded but marked for
        # security review and can never be auto-deployed.
        security_review = True if blocked_files else bool(result.get("security_review_required", True))

        record = ImprovementProposal(
            title=str(result.get("title", "Untitled proposal"))[:500],
            problem_statement=problem,
            root_cause_analysis=str(result.get("root_cause", "")),
            proposed_change=str(result.get("proposed_change", "")),
            affected_files=affected,
            required_tests=[str(t) for t in (result.get("required_tests") or [])],
            security_review_required=security_review,
            status="PROPOSED",
            rollback_plan=str(result.get("rollback_plan", "")),
            version_history={"created_at": datetime.now(timezone.utc).isoformat(), "revision": 1},
        )
        self.db.add(record)
        await self.db.flush()

        if blocked_files:
            logger.warning(
                "Proposal %s touches protected paths %s. Manual security review is mandatory.",
                record.id,
                blocked_files,
            )
            record.outcome_notes = f"PROTECTED FILES FLAGGED: {', '.join(blocked_files)}"

        logger.info("Created improvement proposal: %s", record.title)
        return record

    # ------------------------------------------------------------------
    async def mark_branch_created(self, proposal: ImprovementProposal, branch_name: str) -> None:
        proposal.branch_name = branch_name
        proposal.status = "BRANCH_CREATED"
        history = proposal.version_history or {}
        history["branch_created_at"] = datetime.now(timezone.utc).isoformat()
        proposal.version_history = history
        await self.db.flush()

    async def mark_validation_pending(self, proposal: ImprovementProposal) -> None:
        proposal.status = "VALIDATION_PENDING"
        await self.db.flush()

    async def mark_deployed(self, proposal: ImprovementProposal, notes: str) -> None:
        """Only callable from an authorized administrative action after validation."""
        if proposal.status != "VALIDATED":
            raise ValueError(
                f"Cannot deploy a proposal in state '{proposal.status}'. "
                "Validation must complete first; unattended deployment of unvalidated "
                "self-generated code is prohibited."
            )
        proposal.status = "DEPLOYED"
        proposal.outcome_notes = notes
        history = proposal.version_history or {}
        history["deployed_at"] = datetime.now(timezone.utc).isoformat()
        proposal.version_history = history
        await self.db.flush()

    async def mark_rolled_back(self, proposal: ImprovementProposal, notes: str) -> None:
        proposal.status = "ROLLED_BACK"
        proposal.outcome_notes = notes
        history = proposal.version_history or {}
        history["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        proposal.version_history = history
        await self.db.flush()

    async def list_proposals(self, status: Optional[str] = None, limit: int = 50) -> List[ImprovementProposal]:
        stmt = select(ImprovementProposal).order_by(ImprovementProposal.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(ImprovementProposal.status == status)
        return list((await self.db.execute(stmt)).scalars().all())
