"""Confirmed defects found in the post-push audit, each pinned by a test.

Every test here corresponds to a defect that was reproduced against real code,
not inferred from reading. They are grouped by the seam that was broken.

These are deliberately narrow: each one proves the specific defect is closed
without asserting anything about behaviour the audit did not establish.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.models import AirdropCampaign, Project
from backend.app.models.airdrops import WalletEligibility
from backend.app.models.transactions import (
    BlockchainTransaction,
    TransactionApproval,
    TransactionReceipt,
)


# ==================================================================
# Pending / reverted transactions must not verify (audit P0)
# ==================================================================


def test_a_broadcast_that_is_not_mined_yet_cannot_complete_a_task():
    """The regression this pins.

    `transfer_tools` returns `authoritative: False` for a pending broadcast and
    still includes the tx hash in the payload. The legacy payload fallback used
    to rescue that and return verified=True, so a transfer sitting in the
    mempool could complete a task and be reported as authoritative evidence.
    """
    from backend.tests.test_evidence_contract import _verify

    assert _verify(
        {"authoritative": False, "kind": "chain_receipt", "tx_hash": "0xdead"},
        {"tx_hash": "0xdead", "status": "PENDING", "confirmations": 0},
        "chain_receipt",
    ) is False


def test_the_fix_does_not_break_a_genuinely_confirmed_receipt():
    from backend.tests.test_evidence_contract import _verify

    assert _verify(
        {"authoritative": True, "kind": "chain_receipt"},
        {"tx_hash": "0xdead", "status": "CONFIRMED"},
        "chain_receipt",
    ) is True


# ==================================================================
# Transaction ids must be generated (audit P0)
# ==================================================================


@pytest.mark.parametrize("model", [BlockchainTransaction, TransactionReceipt])
def test_transaction_ids_have_a_generated_default(model):
    """Every insert sent NULL and failed on NOT NULL.

    `Base` supplies `default=uuid.uuid4`, but redeclaring `id` in a subclass
    replaces it with a bare column. `TransactionApproval` already declared it
    explicitly with a comment saying so; the other two did not.
    """
    column = model.__table__.c["id"]
    assert column.default is not None, f"{model.__name__}.id would insert NULL"

    # Behaviour, not function identity: the default must actually produce a
    # distinct UUID per call, which is what makes an INSERT succeed.
    default = column.default
    assert default.is_callable, f"{model.__name__}.id default must be callable"
    produced = {default.arg(None) for _ in range(5)}
    assert len(produced) == 5, f"{model.__name__}.id default must be unique per row"
    assert all(isinstance(v, uuid.UUID) for v in produced)


def test_transaction_approval_id_was_already_correct():
    """The reference implementation, kept honest so it cannot regress."""
    assert TransactionApproval.__table__.c["id"].default is not None


# ==================================================================
# Campaign persistence must actually persist (audit P0)
# ==================================================================


@pytest.mark.asyncio
async def test_discovered_campaigns_are_actually_written(_isolated_database):
    """`_persist_campaigns` silently wrote nothing.

    `Project.slug` is NOT NULL and was never set, so every INSERT raised. The
    exception was swallowed into a warning and the tool still reported success.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.tools.airdrop_tools import _persist_campaigns

    campaigns = [
        {
            "name": "Retrodrop Protocol",
            "source_url": "https://example.com/retrodrop",
            "source_type": "community",
            "network": "ethereum",
            "description": "A test airdrop",
            "risk_factors": [],
        }
    ]
    assert await _persist_campaigns(campaigns) == 1, "the campaign was not persisted"

    async with AsyncSession(_isolated_database) as db:
        row = await db.scalar(
            select(AirdropCampaign).where(AirdropCampaign.title == "Retrodrop Protocol")
        )
        assert row is not None
        assert row.lifecycle_status == "DISCOVERED"
        assert row.requirements.get("source_url") == "https://example.com/retrodrop"
        project = await db.get(Project, row.project_id)
        assert project is not None and project.slug


@pytest.mark.asyncio
async def test_the_same_campaign_twice_is_deduplicated_not_duplicated(_isolated_database):
    """The dedupe lookup ran against a freshly minted project id.

    It therefore matched nothing, always, and re-discovering a campaign created
    a duplicate row instead of skipping it.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.tools.airdrop_tools import _persist_campaigns

    campaign = {
        "name": "Duplicate Test Drop",
        "source_url": "https://example.com/dup",
        "source_type": "community",
        "network": "ethereum",
        "risk_factors": [],
    }
    assert await _persist_campaigns([campaign]) == 1
    assert await _persist_campaigns([campaign]) == 0, "a repeat discovery must be deduplicated"

    async with AsyncSession(_isolated_database) as db:
        rows = list((await db.execute(select(AirdropCampaign))).scalars().all())
    assert len([r for r in rows if r.title == "Duplicate Test Drop"]) == 1


# ==================================================================
# Eligibility projection must match the real schema (audit P1)
# ==================================================================


def test_check_wallet_eligibility_reads_only_real_columns():
    """It read four columns that do not exist, raising AttributeError on any row.

    Eligibility is also deliberately NOT collapsed to a boolean: `UNKNOWN` and
    `INELIGIBLE` are different facts and the model must be able to tell them
    apart.
    """
    import inspect

    from backend.app.tools.airdrop_tools import CheckWalletEligibilityTool

    source = inspect.getsource(CheckWalletEligibilityTool)
    real = {c.name for c in WalletEligibility.__table__.c}
    for phantom in ("is_eligible", "eligibility_score", "eligibility_reason", "checked_at"):
        assert f"r.{phantom}" not in source, f"{phantom} does not exist on WalletEligibility"
    assert "r.eligibility_status" in source
    assert "r.participation_status" in source


# ==================================================================
# A third-party page is not authoritative evidence (audit P1)
# ==================================================================


@pytest.mark.asyncio
async def test_research_campaign_does_not_mark_an_unverified_page_authoritative():
    """Any URL the agent chose used to satisfy a `source_scan` verification.

    That is the fabrication vector: a step promising "confirm the reward on the
    official site" could be closed by any page, including one whose text
    instructed the agent to go there.
    """
    from backend.app.tools import airdrop_tools, web_tools

    page = "<html><body><h1>Retrodrop</h1><p>Claim 5000 RETRO now!</p></body></html>"

    async def fake_fetch(url, timeout):
        return page, None

    # `_guarded_html_fetch` is imported inside the method body from web_tools,
    # so that module attribute is what has to be replaced.
    original = web_tools._guarded_html_fetch
    web_tools._guarded_html_fetch = fake_fetch
    try:
        result = await airdrop_tools.ResearchCampaignTool().execute(
            {"url": "https://some-random-blog.example/post"}
        )
    finally:
        web_tools._guarded_html_fetch = original

    assert result.evidence is not None
    assert result.evidence["authoritative"] is False, (
        "a self-reported page cannot be authoritative evidence of its own claims"
    )
    assert result.evidence["kind"] == "source_scan"


# ==================================================================
# LLM response model identity (spec 3.1)
# ==================================================================


def test_the_client_never_invents_a_served_model():
    from backend.app.llm.client import FixedLLMClient

    client = FixedLLMClient()
    response = client._normalize({"choices": [{"message": {"content": "hi"}}]})
    assert response.model is None
    assert response.requested_model == client.model_id


def test_health_check_does_not_default_the_served_model():
    import inspect

    from backend.app.llm.client import FixedLLMClient

    source = inspect.getsource(FixedLLMClient.health_check)
    assert 'raw.get("model", self._model_id)' not in source
    assert 'raw.get("model")' in source