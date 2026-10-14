"""Campaign Context: facts that several tasks on one campaign may legitimately share.

The specification (§6.2) asks for three separate levels of context. Task Context lives in
`app.agent.context` and is strictly private. This module is the middle level: verified,
campaign-wide information that more than one task may read.

The separation that matters is between what describes the *campaign* and what describes an
*account or a wallet*:

* "the campaign requires joining @channel" is campaign-wide. Every task on this campaign
  may be told it.
* "this account joined @channel" is account-specific. Another task, and another wallet,
  must never inherit it.

Both are stored, but they are stored differently and read differently. Campaign facts go
into the campaign row and into campaign-scoped memory; account and wallet state goes into
`wallet_eligibility` and the owning task's context and is read back only with an explicit
account reference.

Freshness is enforced rather than assumed. A campaign fact has a `last_checked_at`, and
`snapshot()` reports whether it is still current. Reusing a stale fact is allowed but is
never silent: the caller is told, so a task can revalidate before acting on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.memory.manager import MemoryManager
from backend.app.models import (
    AirdropCampaign,
    CampaignSource,
    MemoryRecord,
    Task,
    TaskContext,
    WalletEligibility,
)
from backend.app.models.memory import SCOPE_CAMPAIGN

logger = logging.getLogger(__name__)

# How long campaign-wide facts stay current without being re-checked. Past this the
# snapshot is reported as stale, and a task is expected to revalidate before relying on
# a deadline or a requirement it names.
CAMPAIGN_FRESHNESS_HOURS = 24

# Lifecycle states in which a campaign's own facts are still worth reading. A cancelled
# or expired campaign is not deleted - its history explains why a task failed - but its
# requirements must not be presented as current work.
TERMINAL_LIFECYCLE = frozenset({"EXPIRED", "CANCELLED"})

# Memory category used for campaign-wide facts.
CATEGORY_CAMPAIGN_FACT = "CAMPAIGN_REQUIREMENT"


@dataclass
class CampaignSnapshot:
    """The campaign-wide facts one or more tasks may share, and how current they are."""

    campaign_id: str
    title: str
    lifecycle_status: str
    is_stale: bool = False
    last_checked_at: Optional[str] = None
    official_sources: List[str] = field(default_factory=list)
    requirements: List[Any] = field(default_factory=list)
    reward_info: Dict[str, Any] = field(default_factory=dict)
    risk_factors: List[str] = field(default_factory=list)
    deadlines: Dict[str, Optional[str]] = field(default_factory=dict)
    shared_facts: List[str] = field(default_factory=list)
    reliability: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle_status in TERMINAL_LIFECYCLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "title": self.title,
            "lifecycle_status": self.lifecycle_status,
            "is_stale": self.is_stale,
            "last_checked_at": self.last_checked_at,
            "official_sources": self.official_sources,
            "requirements": self.requirements,
            "reward_info": self.reward_info,
            "risk_factors": self.risk_factors,
            "deadlines": self.deadlines,
            "shared_facts": self.shared_facts,
            "reliability": self.reliability,
            "notes": self.notes,
        }


@dataclass
class WalletStanding:
    """One wallet's standing on one campaign. Never shared between wallets."""

    campaign_id: str
    wallet_id: str
    eligibility_status: str
    participation_status: str
    tasks_completed_count: int
    last_verified_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "wallet_id": self.wallet_id,
            "eligibility_status": self.eligibility_status,
            "participation_status": self.participation_status,
            "tasks_completed_count": self.tasks_completed_count,
            "last_verified_at": self.last_verified_at,
        }


class CampaignContextService:
    """Reads and writes the shared middle level of the context hierarchy."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._memory = MemoryManager(db)

    # ------------------------------------------------------------------
    # Linking a task to a campaign
    # ------------------------------------------------------------------
    async def link_task(self, task: Task, campaign: AirdropCampaign) -> None:
        """Associate this task with a campaign it was verified to belong to.

        A second task on the same campaign gets its own Task Context and points at the
        same campaign here; it never inherits the first task's execution history. That is
        the whole point of keeping the two levels apart.
        """
        context = await self.db.scalar(
            select(TaskContext).where(TaskContext.task_id == task.id)
        )
        if context is None:
            # No context yet means this task has not run. Create one so the link is
            # recorded now rather than depending on which task happens to execute first.
            context = TaskContext(task_id=task.id, goal=(task.title or "")[:2000] or None)
            self.db.add(context)
            await self.db.flush()

        context.campaign_id = campaign.id
        flag_modified(context, "campaign_id")
        await self.db.flush()

    async def find_campaign_for_task(self, task: Task) -> Optional[AirdropCampaign]:
        context = await self.db.scalar(
            select(TaskContext).where(TaskContext.task_id == task.id)
        )
        if context is None or context.campaign_id is None:
            return None
        return await self.db.get(AirdropCampaign, context.campaign_id)

    # ------------------------------------------------------------------
    # Campaign-wide facts
    # ------------------------------------------------------------------
    async def snapshot(self, campaign: AirdropCampaign) -> CampaignSnapshot:
        """Assemble the campaign-wide facts, and say whether they are still current.

        Staleness is reported rather than hidden. A deadline or a requirement that was
        true yesterday may be false today, and a task reusing it needs to know that.
        """
        snapshot = CampaignSnapshot(
            campaign_id=str(campaign.id),
            title=campaign.title,
            lifecycle_status=campaign.lifecycle_status,
            last_checked_at=(
                campaign.last_checked_at.isoformat() if campaign.last_checked_at else None
            ),
            requirements=campaign.requirements or {},
            reward_info=campaign.reward_info or {},
            risk_factors=list(campaign.risk_factors or []),
            reliability=float(campaign.source_reliability or 0.0),
        )

        snapshot.is_stale = self.is_stale(campaign)
        if snapshot.is_stale:
            snapshot.notes.append(
                f"Campaign facts were last checked "
                f"{(campaign.last_checked_at or 'never').isoformat() if campaign.last_checked_at else 'never'}"
                f"; treat them as out of date and revalidate before acting on them."
            )
        if snapshot.is_terminal:
            snapshot.notes.append(
                f"This campaign is {campaign.lifecycle_status}. Its requirements are history, "
                "not current work."
            )

        sources = list(
            (
                await self.db.execute(
                    select(CampaignSource).where(CampaignSource.campaign_id == campaign.id)
                )
            )
            .scalars()
            .all()
        )
        for source in sources:
            # Only sources already judged official are presented as official. A community
            # link is kept for provenance but never labelled as the project's own site.
            if str(source.source_type).upper() == "OFFICIAL":
                snapshot.official_sources.append(source.source_url)
            elif source.source_url not in snapshot.official_sources:
                snapshot.notes.append(f"Non-official source recorded: {source.source_url}")

        for label, value in (
            ("start", campaign.start_date),
            ("end", campaign.end_date),
            ("snapshot", campaign.snapshot_date),
            ("claim_deadline", campaign.claim_deadline),
        ):
            snapshot.deadlines[label] = value.isoformat() if value else None

        records = list(
            (
                await self.db.execute(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.scope == SCOPE_CAMPAIGN,
                        MemoryRecord.campaign_id == campaign.id,
                    )
                    .order_by(MemoryRecord.confidence.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        snapshot.shared_facts = [r.content for r in records]
        return snapshot

    @staticmethod
    def is_stale(campaign: AirdropCampaign, *, now: Optional[datetime] = None) -> bool:
        """True when the campaign has not been re-checked within the freshness window.

        A campaign that has never been checked is stale. Presenting unchecked facts as
        current is the failure mode this exists to prevent.
        """
        if campaign.last_checked_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        checked = campaign.last_checked_at
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return now - checked > timedelta(hours=CAMPAIGN_FRESHNESS_HOURS)

    async def record_shared_fact(
        self,
        campaign: AirdropCampaign,
        content: str,
        *,
        source: str,
        confidence: float = 0.8,
        is_account_specific: bool = False,
        account_ref: Optional[str] = None,
        ttl_days: Optional[int] = 30,
    ) -> None:
        """Store a fact other tasks on this campaign may reuse.

        Account-specific facts are refused outright. "This wallet is eligible" or "this
        account joined the channel" describe one participant, and writing them here is
        exactly the mistake that makes one account's state stand in for another's. They
        belong to the task that observed them.
        """
        if is_account_specific:
            raise ValueError(
                "Account- or wallet-specific state must not be written to campaign context. "
                "Record it against the task that observed it."
            )
        await self._memory.remember_scoped(
            CATEGORY_CAMPAIGN_FACT,
            content,
            source,
            scope=SCOPE_CAMPAIGN,
            campaign_id=campaign.id,
            confidence=confidence,
            related_entities={"campaign_id": str(campaign.id), "account_ref": account_ref},
            ttl_days=ttl_days,
        )
        campaign.last_checked_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def mark_checked(self, campaign: AirdropCampaign) -> None:
        campaign.last_checked_at = datetime.now(timezone.utc)
        await self.db.flush()

    # ------------------------------------------------------------------
    # Account- and wallet-specific state
    # ------------------------------------------------------------------
    async def wallet_standing(
        self, campaign: AirdropCampaign, wallet_id: Any
    ) -> Optional[WalletStanding]:
        """This wallet's standing on this campaign, and only this wallet's.

        Read back with an explicit wallet reference precisely so the answer can never be
        taken from another wallet's row: one wallet being eligible says nothing about
        whether another is.
        """
        if wallet_id is None:
            return None
        row = await self.db.scalar(
            select(WalletEligibility).where(
                WalletEligibility.campaign_id == campaign.id,
                WalletEligibility.wallet_id == wallet_id,
            )
        )
        if row is None:
            return None
        return WalletStanding(
            campaign_id=str(campaign.id),
            wallet_id=str(wallet_id),
            eligibility_status=row.eligibility_status,
            participation_status=row.participation_status,
            tasks_completed_count=int(row.tasks_completed_count or 0),
            last_verified_at=row.last_verified_at.isoformat() if row.last_verified_at else None,
        )

    async def all_wallet_standings(self, campaign: AirdropCampaign) -> List[WalletStanding]:
        """Every wallet's standing, each labelled with its own wallet.

        Returned as a list rather than merged into one verdict on purpose: the caller has
        to choose a wallet, and the choice is then visible in what it does next.
        """
        rows = list(
            (
                await self.db.execute(
                    select(WalletEligibility).where(
                        WalletEligibility.campaign_id == campaign.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            WalletStanding(
                campaign_id=str(campaign.id),
                wallet_id=str(r.wallet_id),
                eligibility_status=r.eligibility_status,
                participation_status=r.participation_status,
                tasks_completed_count=int(r.tasks_completed_count or 0),
                last_verified_at=r.last_verified_at.isoformat() if r.last_verified_at else None,
            )
            for r in rows
        ]


__all__ = [
    "CAMPAIGN_FRESHNESS_HOURS",
    "CampaignContextService",
    "CampaignSnapshot",
    "WalletStanding",
]