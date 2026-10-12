"""Airdrop tools: discover campaigns, research them, and judge eligibility.

`discover_airdrops` runs the real discovery cycle through the registered web tools
and reports what it found *and* what it could not reach. Sources that refuse
automated access (CoinGecko's airdrop index answers 403 to non-browser clients) are
recorded as failures rather than silently dropped, so a cycle that checked three
sources and read none is never presented as a clean run.

`research_campaign` re-reads a specific campaign's pages through the SSRF-guarded
fetcher and extracts requirements, deadlines and risk signals. Everything it returns
is labelled UNVERIFIED unless an official source confirmed it: the platform cannot
prove a site is safe, and says so instead of implying it can.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.app.database.session import session_scope
from backend.app.models import AirdropCampaign
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)


def _campaign_row(row: AirdropCampaign) -> Dict[str, Any]:
    """Public projection of a campaign, matching the real columns.

    The field names here are the ones on `AirdropCampaign`. A projection that
    invents columns (`name`, `status`, `url`) does not merely read as empty - it
    raises `AttributeError` at query time, so `list_campaigns` could never run.
    """
    return {
        "campaign_id": str(row.id),
        "project_id": str(row.project_id),
        "title": row.title,
        "lifecycle_status": row.lifecycle_status,
        "network": row.network,
        "requirements": row.requirements or {},
        "eligibility_rules": row.eligibility_rules or {},
        "estimated_cost_usd": row.estimated_cost_usd,
        "reward_info": row.reward_info or {},
        "risk_factors": row.risk_factors or [],
        "source_reliability": row.source_reliability,
        "snapshot_date": row.snapshot_date.isoformat() if row.snapshot_date else None,
        "claim_deadline": row.claim_deadline.isoformat() if row.claim_deadline else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "discovered_at": row.created_at.isoformat() if row.created_at else None,
    }


class DiscoverAirdropsTool(BaseTool):
    name = "discover_airdrops"
    version = "1.0.0"
    description = (
        "Run a real airdrop discovery cycle over the configured sources. Returns the "
        "campaigns found, deduplicated, plus an explicit list of sources that could "
        "not be read and why. Use this to answer 'find good airdrops'."
    )
    required_permissions = ["airdrop.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 180
    input_schema = {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pages to scan. Defaults to the configured discovery sources.",
            },
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.airdrops.discovery import run_discovery_cycle

        urls = [str(u) for u in (params.get("urls") or []) if str(u).strip()]
        sources = {"community": urls} if urls else None

        try:
            cycle = await run_discovery_cycle(sources)
        except Exception as exc:
            logger.exception("Discovery cycle failed")
            return ToolResult.failure(
                f"The discovery cycle failed: {type(exc).__name__}: {exc}",
                category="DISCOVERY_FAILED",
            )

        campaigns = cycle.get("campaigns") or []
        failures = cycle.get("source_failures") or []

        # Persist what was genuinely discovered so the operator's history is real.
        stored = 0
        if campaigns:
            try:
                stored = await _persist_campaigns(campaigns)
            except Exception as exc:  # persistence must not lose the findings
                logger.warning("Could not persist discovered campaigns: %s", exc)

        note_parts = []
        if failures:
            note_parts.append(
                f"{len(failures)} source(s) could not be read; their contents are unknown, "
                f"not empty."
            )
        note_parts.append(
            "Campaigns are leads, not endorsements. Nothing here has been proven safe."
        )

        return ToolResult.success(
            data={
                "campaigns": campaigns,
                "campaign_count": len(campaigns),
                "results_by_source": cycle.get("results_by_source") or {},
                "source_failures": failures,
                "sources_failed": len(failures),
                "persisted": stored,
                "status": cycle.get("status"),
                "note": " ".join(note_parts),
            },
            evidence={
                "authoritative": True,
                "kind": "source_scan",
                "campaign_count": len(campaigns),
                "sources_failed": len(failures),
            },
        )


async def _persist_campaigns(campaigns: List[Dict[str, Any]]) -> int:
    """Save discovered campaigns, deduplicated by project + title.

    `AirdropCampaign.project_id` is NOT NULL with a cascade foreign key, so a
    discovered campaign is filed under a `Project` created for it. The URL is kept in
    `requirements` (the model's only free-text field for provenance), and the
    reliability string is mapped onto the numeric `source_reliability` column rather
    than written into it as text.
    """
    from backend.app.models import Project

    stored = 0
    async with session_scope() as db:
        for campaign in campaigns:
            title = (campaign.get("name") or "").strip()[:500]
            if not title:
                continue
            url = (campaign.get("source_url") or "").strip()

            project = Project(
                name=title,
                description=(campaign.get("description") or "")[:4000] or None,
            )
            db.add(project)
            await db.flush()

            existing = await db.scalar(
                select(AirdropCampaign).where(
                    AirdropCampaign.project_id == project.id,
                    AirdropCampaign.title == title,
                )
            )
            if existing is not None:
                await db.delete(project)
                await db.flush()
                continue

            db.add(
                AirdropCampaign(
                    project_id=project.id,
                    title=title,
                    lifecycle_status="DISCOVERED",
                    network=campaign.get("network"),
                    source_reliability=_reliability_score(campaign.get("source_type")),
                    requirements={"source_url": url} if url else {},
                    eligibility_rules={"status": "NOT_EVALUATED"},
                    reward_info={},
                    risk_factors=list(campaign.get("risk_factors") or []),
                )
            )
            stored += 1
    return stored


def _reliability_score(source_type: Optional[str]) -> float:
    """Map the discovery layer's reliability vocabulary onto the model's 0..1 score."""
    return {
        "OFFICIAL": 0.9,
        "ONCHAIN": 0.85,
        "RESEARCH": 0.6,
        "COMMUNITY": 0.4,
    }.get(str(source_type or "").upper(), 0.5)


class ListCampaignsTool(BaseTool):
    name = "list_campaigns"
    version = "1.0.0"
    description = "List campaigns already recorded in the database, with their real verification state."
    required_permissions = ["airdrop.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "lifecycle_status": {
                "type": "string",
                "maxLength": 50,
                "description": "e.g. DISCOVERED, ELIGIBLE, CLAIM_OPEN, EXPIRED",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        limit = int(params.get("limit") or 25)
        async with session_scope() as db:
            query = select(AirdropCampaign).order_by(AirdropCampaign.created_at.desc()).limit(limit)
            if params.get("lifecycle_status"):
                query = query.where(
                    AirdropCampaign.lifecycle_status == str(params["lifecycle_status"]).upper()
                )
            rows = list((await db.execute(query)).scalars().all())
            items = [_campaign_row(r) for r in rows]

        return ToolResult.success(
            data={"campaigns": items, "count": len(items)},
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "campaign",
                "count": len(items),
            },
        )


class ResearchCampaignTool(BaseTool):
    name = "research_campaign"
    version = "1.0.0"
    description = (
        "Read a campaign's own pages and extract requirements, deadlines, risk signals and "
        "prompt-injection flags. Reports what the page said and how much it can be trusted; "
        "it never claims a site is verified safe."
    )
    required_permissions = ["web.read", "airdrop.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 60
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "maxLength": 2048, "description": "The campaign page"},
            "sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Section keywords to extract",
            },
        },
        "required": ["url"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.tools.web_tools import (
            _guarded_html_fetch,
            _html_to_text,
            extract_dates,
            sanitize_external_text,
        )
        from backend.app.airdrops.discovery import RISK_SIGNALS

        url = params["url"]
        html, failure = await _guarded_html_fetch(url, self.timeout_seconds)
        if failure is not None:
            return failure

        text = _html_to_text(html or "")
        sanitized = sanitize_external_text(text, 20000)
        lowered = text.lower()

        sections = [
            s.lower()
            for s in (params.get("sections") or ["eligib", "require", "claim", "deadline", "snapshot", "reward"])
        ]
        extracted: Dict[str, List[str]] = {s: [] for s in sections}
        for line in sanitized["text"].splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 500:
                continue
            for section in sections:
                if section in stripped.lower() and len(extracted[section]) < 30:
                    extracted[section].append(stripped)

        risks: List[str] = []
        if "seed phrase" in lowered or "private key" in lowered or "mnemonic" in lowered:
            risks.append(RISK_SIGNALS["seed_phrase_request"])
        if "deposit" in lowered:
            risks.append("Mentions a deposit. A deposit is a spendable transaction needing approval.")
        if any(word in lowered for word in ("guaranteed return", "guaranteed profit", "guaranteed apy")):
            risks.append(RISK_SIGNALS["guaranteed_returns"])

        return ToolResult.success(
            data={
                "url": url,
                "sections": extracted,
                "dates_detected": extract_dates(text),
                "risk_signals": risks,
                "injection_flagged": sanitized["is_flagged"],
                "injection_markers": sanitized["injection_markers_found"],
                "confidence": (
                    "official_source_unverified" if sanitized["is_flagged"] else "self_reported_unverified"
                ),
                "note": (
                    "This is what the page says. AirdropOS cannot prove a site is safe or that a "
                    "reward is real; a reward is only real when the chain or your account shows it."
                ),
            },
            evidence={
                "authoritative": True,
                "kind": "source_scan",
                "url": url,
                "risk_signals": len(risks),
            },
        )


class CheckWalletEligibilityTool(BaseTool):
    name = "check_wallet_eligibility"
    version = "1.0.0"
    description = (
        "Report a wallet's recorded eligibility for a recorded campaign, and read the live "
        "native balance so the operator can see what a deposit would actually cost."
    )
    required_permissions = ["airdrop.read", "wallet.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 25
    input_schema = {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string", "maxLength": 64},
            "wallet_id": {"type": "string", "maxLength": 64},
        },
        "required": ["campaign_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        import uuid

        from backend.app.models import WalletEligibility

        async with session_scope() as db:
            try:
                campaign_uuid = uuid.UUID(str(params["campaign_id"]))
            except ValueError:
                return ToolResult.failure(
                    f"'{params['campaign_id']}' is not a campaign id. Use list_campaigns.",
                    category="INVALID_INPUT",
                )
            query = select(WalletEligibility).where(WalletEligibility.campaign_id == campaign_uuid)
            if params.get("wallet_id"):
                try:
                    query = query.where(
                        WalletEligibility.wallet_id == uuid.UUID(str(params["wallet_id"]))
                    )
                except ValueError:
                    return ToolResult.failure(
                        f"'{params['wallet_id']}' is not a wallet id.", category="INVALID_INPUT"
                    )
            rows = list((await db.execute(query)).scalars().all())
            items = [
                {
                    "wallet_id": str(r.wallet_id),
                    "is_eligible": r.is_eligible,
                    "eligibility_score": r.eligibility_score,
                    "reason": r.eligibility_reason,
                    "checked_at": r.checked_at.isoformat() if r.checked_at else None,
                }
                for r in rows
            ]

        if not items:
            return ToolResult.needs_user(
                "No eligibility has been evaluated for this campaign yet. Run the campaign's "
                "task first; eligibility is assessed from what that task actually observed."
            )

        return ToolResult.success(
            data={"eligibility": items, "count": len(items)},
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "wallet_eligibility",
                "count": len(items),
            },
        )


def register_airdrop_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            DiscoverAirdropsTool(),
            ListCampaignsTool(),
            ResearchCampaignTool(),
            CheckWalletEligibilityTool(),
        ]
    )