"""Campaign Context: what may be shared between tasks, and what may never be.

The specification (§6.2, §6.6) allows tasks on the same verified campaign to reuse
relevant campaign information, while forbidding the two failures that make reuse unsafe:
one account's or wallet's state standing in for another's, and a fact that has gone stale
being acted on as if it were current.

These tests pin:

* a second task on one campaign shares the campaign's facts and inherits none of the
  first task's execution history;
* account- and wallet-specific state is refused at campaign level and read back only with
  an explicit account reference;
* stale campaign facts are reported as stale rather than presented as current;
* an account-specific requirement is never offered as a reusable campaign fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import TaskContextService
from backend.app.airdrops.campaign_context import (
    CAMPAIGN_FRESHNESS_HOURS,
    CampaignContextService,
)
from backend.app.memory.manager import MemoryManager
from backend.app.models import (
    AirdropCampaign,
    Base,
    CampaignSource,
    MemoryRecord,
    Project,
    Task,
    Wallet,
    WalletEligibility,
)
from backend.app.models.context import OUTCOME_CONFIRMED
from backend.app.models.memory import SCOPE_CAMPAIGN


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield made
    await engine.dispose()


async def _campaign(factory, *, title="Example Airdrop", **kwargs) -> str:
    async with factory() as db:
        project = Project(name=f"project-{title}", slug=title.lower().replace(" ", "-"))
        db.add(project)
        await db.flush()
        campaign = AirdropCampaign(
            project_id=project.id,
            title=title,
            lifecycle_status=kwargs.pop("lifecycle_status", "RESEARCHING"),
            requirements=kwargs.pop("requirements", {"tasks": ["join the channel"]}),
            reward_info=kwargs.pop("reward_info", {"token": "TKN"}),
            risk_factors=kwargs.pop("risk_factors", []),
            source_reliability=kwargs.pop("source_reliability", 0.8),
            **kwargs,
        )
        db.add(campaign)
        await db.commit()
        return campaign.id


async def _task(factory, title: str) -> str:
    async with factory() as db:
        task = Task(
            task_id=f"AIR-2026-{abs(hash(title)) % 900000 + 100000:06d}",
            title=title,
            description=title,
            status="IN_PROGRESS",
            owner_email="operator@test.local",
        )
        db.add(task)
        await db.commit()
        return task.id


async def _wallet(factory, address: str) -> str:
    async with factory() as db:
        wallet = Wallet(
            address=address, chain_type="EVM", label=address[-4:], encrypted_secret="x"
        )
        db.add(wallet)
        await db.commit()
        return wallet.id


def _evidence(kind: str = "membership_verified") -> dict:
    return {"authoritative": True, "kind": kind}


# ======================================================================
class TestCampaignFactsAreShared:
    async def test_two_tasks_on_one_campaign_share_the_campaign_facts(self, factory):
        campaign_id = await _campaign(factory)
        first = await _task(factory, "Finish the airdrop")
        second = await _task(factory, "Check whether I am eligible")

        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            await service.record_shared_fact(
                campaign,
                "The campaign requires joining @examplechannel and holding 100 TKN.",
                source="official site",
            )
            await db.commit()

            task_a = await db.get(Task, first)
            task_b = await db.get(Task, second)
            await service.link_task(task_a, campaign)
            await service.link_task(task_b, campaign)
            await db.commit()

            snapshot = await service.snapshot(campaign)
            assert "joining @examplechannel" in snapshot.shared_facts[0]

            # Both tasks resolve the same campaign fact...
            context = TaskContextService(db)
            resolved_a = await context.resolve(task_a)
            resolved_b = await context.resolve(task_b)
            assert resolved_a.campaign_facts and resolved_b.campaign_facts
            assert resolved_a.campaign_facts[0]["content"] == resolved_b.campaign_facts[0]["content"]

    async def test_the_second_task_inherits_no_execution_history(self, factory):
        """A new task on the same campaign gets a new Task Context, not the old one."""
        campaign_id = await _campaign(factory)
        first = await _task(factory, "Do the whole airdrop")
        second = await _task(factory, "Do the airdrop again for another wallet")

        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            task_a = await db.get(Task, first)
            task_b = await db.get(Task, second)

            await service.link_task(task_a, campaign)
            await service.link_task(task_b, campaign)

            context = TaskContextService(db)
            await context.record_observation(
                task_a, action="joined @examplechannel", outcome=OUTCOME_CONFIRMED,
                tool="telegram_join_chat", parameters={"chat": "@examplechannel"},
                evidence=_evidence(),
            )
            await context.record_requirement(task_a, "this account joined the channel")
            await db.commit()

            resolved_b = await context.resolve(task_b)
            assert resolved_b.observations == [], "a new task must start with no history"
            assert resolved_b.outstanding_requirements() == []
            assert resolved_b.task_context.id != (await context.get(task_a)).id

    async def test_a_task_linked_to_no_campaign_reads_no_campaign_facts(self, factory):
        campaign_id = await _campaign(factory)
        unrelated = await _task(factory, "Unrelated task")

        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            await service.record_shared_fact(campaign, "campaign specific fact", source="site")
            await db.commit()

            task = await db.get(Task, unrelated)
            resolved = await TaskContextService(db).resolve(task)
            assert resolved.campaign_facts == []
            assert resolved.campaign_id is None


# ======================================================================
class TestAccountAndWalletSeparation:
    async def test_account_specific_state_is_refused_at_campaign_level(self, factory):
        """One participant's state must never become a shared campaign fact."""
        campaign_id = await _campaign(factory)
        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            with pytest.raises(ValueError):
                await service.record_shared_fact(
                    campaign,
                    "wallet 0xabc is eligible",
                    source="observed",
                    is_account_specific=True,
                    account_ref="0xabc",
                )

    async def test_one_wallet_eligibility_does_not_imply_another(self, factory):
        campaign_id = await _campaign(factory)
        good = await _wallet(factory, "0x" + "1" * 40)
        bad = await _wallet(factory, "0x" + "2" * 40)

        async with factory() as db:
            db.add_all(
                [
                    WalletEligibility(
                        campaign_id=campaign_id, wallet_id=good,
                        eligibility_status="ELIGIBLE", participation_status="NOT_STARTED",
                        tasks_completed_count=0,
                    ),
                    WalletEligibility(
                        campaign_id=campaign_id, wallet_id=bad,
                        eligibility_status="INELIGIBLE", participation_status="NOT_STARTED",
                        tasks_completed_count=0,
                    ),
                ]
            )
            await db.commit()

            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)

            assert (await service.wallet_standing(campaign, good)).eligibility_status == "ELIGIBLE"
            assert (await service.wallet_standing(campaign, bad)).eligibility_status == "INELIGIBLE"

            standings = await service.all_wallet_standings(campaign)
            # `wallet_id` is normalised to a string in the view; compare on that form.
            assert {s.wallet_id: s.eligibility_status for s in standings} == {
                str(good): "ELIGIBLE",
                str(bad): "INELIGIBLE",
            }, "each wallet keeps its own verdict"

    async def test_an_unknown_wallet_has_no_standing_rather_than_the_neighbours_one(self, factory):
        campaign_id = await _campaign(factory)
        known = await _wallet(factory, "0x" + "3" * 40)
        unknown = await _wallet(factory, "0x" + "4" * 40)

        async with factory() as db:
            db.add(
                WalletEligibility(
                    campaign_id=campaign_id, wallet_id=known,
                    eligibility_status="ELIGIBLE", participation_status="NOT_STARTED",
                )
            )
            await db.commit()
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            assert await service.wallet_standing(campaign, unknown) is None

    async def test_an_account_specific_requirement_stays_in_its_own_task(self, factory):
        campaign_id = await _campaign(factory)
        first = await _task(factory, "Wallet one eligibility")
        second = await _task(factory, "Wallet two eligibility")

        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            task_a = await db.get(Task, first)
            task_b = await db.get(Task, second)
            await service.link_task(task_a, campaign)
            await service.link_task(task_b, campaign)

            context = TaskContextService(db)
            await context.record_requirement(
                task_a,
                "wallet 0x1111 completed the onboarding quest",
                is_account_specific=True,
                account_ref="0x1111",
            )
            await db.commit()

            resolved_b = await context.resolve(task_b)
            assert resolved_b.outstanding_requirements() == [], (
                "an account-specific requirement must never be inherited by another task"
            )


# ======================================================================
class TestFreshnessAndProvenance:
    async def test_a_never_checked_campaign_is_stale(self, factory):
        campaign_id = await _campaign(factory)
        async with factory() as db:
            campaign = await db.get(AirdropCampaign, campaign_id)
            snapshot = await CampaignContextService(db).snapshot(campaign)
            assert snapshot.is_stale is True
            assert any("never" in note for note in snapshot.notes)

    async def test_a_recently_checked_campaign_is_not_stale(self, factory):
        campaign_id = await _campaign(factory, last_checked_at=datetime.now(timezone.utc))
        async with factory() as db:
            campaign = await db.get(AirdropCampaign, campaign_id)
            snapshot = await CampaignContextService(db).snapshot(campaign)
            assert snapshot.is_stale is False

    async def test_facts_older_than_the_window_are_reported_stale(self, factory):
        """Reusing a stale fact is allowed; doing so silently is not."""
        old = datetime.now(timezone.utc) - timedelta(hours=CAMPAIGN_FRESHNESS_HOURS + 1)
        campaign_id = await _campaign(factory, last_checked_at=old)
        async with factory() as db:
            campaign = await db.get(AirdropCampaign, campaign_id)
            await CampaignContextService(db).record_shared_fact(
                campaign, "claim deadline is 30 June", source="site"
            )
            # record_shared_fact marks the campaign as checked; age it again.
            campaign.last_checked_at = old
            await db.commit()

            snapshot = await CampaignContextService(db).snapshot(campaign)
            assert snapshot.is_stale is True
            assert any("revalidate" in note for note in snapshot.notes)

    async def test_only_sources_judged_official_are_labelled_official(self, factory):
        campaign_id = await _campaign(factory)
        async with factory() as db:
            db.add_all(
                [
                    CampaignSource(
                        campaign_id=campaign_id, source_url="https://example.org/airdrop",
                        source_type="OFFICIAL", credibility_score=0.95,
                    ),
                    CampaignSource(
                        campaign_id=campaign_id, source_url="https://randomforum.example/x",
                        source_type="COMMUNITY", credibility_score=0.2,
                    ),
                ]
            )
            await db.commit()
            campaign = await db.get(AirdropCampaign, campaign_id)
            snapshot = await CampaignContextService(db).snapshot(campaign)

        assert snapshot.official_sources == ["https://example.org/airdrop"]
        assert any("randomforum" in note for note in snapshot.notes)

    async def test_a_cancelled_campaign_is_reported_as_history_not_current_work(self, factory):
        campaign_id = await _campaign(factory, lifecycle_status="CANCELLED")
        async with factory() as db:
            campaign = await db.get(AirdropCampaign, campaign_id)
            snapshot = await CampaignContextService(db).snapshot(campaign)
            assert snapshot.is_terminal is True
            assert any("history" in note for note in snapshot.notes)

    async def test_deadlines_are_surfaced_so_a_task_can_act_on_time(self, factory):
        campaign_id = await _campaign(
            factory, claim_deadline=datetime(2026, 12, 31, tzinfo=timezone.utc)
        )
        async with factory() as db:
            campaign = await db.get(AirdropCampaign, campaign_id)
            snapshot = await CampaignContextService(db).snapshot(campaign)
        assert snapshot.deadlines["claim_deadline"].startswith("2026-12-31")


# ======================================================================
class TestCampaignMemoryStaysInItsLane:
    async def test_campaign_facts_are_readable_only_through_the_campaign_link(self, factory):
        campaign_id = await _campaign(factory)
        linked = await _task(factory, "Linked task")
        unlinked = await _task(factory, "Unlinked task")

        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            await service.record_shared_fact(campaign, "needs an EIP-712 signature", source="docs")
            task = await db.get(Task, linked)
            await service.link_task(task, campaign)
            await db.commit()

            resolved_linked = await TaskContextService(db).resolve(task)
            resolved_unlinked = await TaskContextService(db).resolve(await db.get(Task, unlinked))

            assert any("EIP-712" in f["content"] for f in resolved_linked.campaign_facts)
            assert resolved_unlinked.campaign_facts == []

    async def test_campaign_records_are_written_at_campaign_scope(self, factory):
        campaign_id = await _campaign(factory)
        async with factory() as db:
            service = CampaignContextService(db)
            campaign = await db.get(AirdropCampaign, campaign_id)
            await service.record_shared_fact(campaign, "a reusable fact", source="official")
            await db.commit()

            memory = MemoryManager(db)
            scoped = await memory.recall_for_context(campaign_id=campaign_id)
            assert [r.scope for r in scoped] == [SCOPE_CAMPAIGN]
            assert [r.campaign_id for r in scoped] == [campaign_id]

            # And they are never handed to a task with no relationship to the campaign.
            elsewhere = await memory.recall_for_context(task_id=await _task(factory, "Stranger"))
            assert [r.content for r in elsewhere] == []