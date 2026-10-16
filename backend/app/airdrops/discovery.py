"""Airdrop Discovery & Research Subsystem.

Discovers campaigns from configurable sources, deduplicates them, and records
every claim with its provenance and confidence. Never promises a reward that has
not been confirmed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backend.app.tools.registry import registry

logger = logging.getLogger(__name__)


class SourceReliability(str, Enum):
    OFFICIAL = "official"            # The project's own site/docs
    ONCHAIN = "onchain"              # Verified on-chain state
    ANNOUNCEMENT = "announcement"    # Official social announcement
    COMMUNITY = "community"          # Aggregators, forums
    UNVERIFIED = "unverified"        # Unknown provenance


RELIABILITY_SCORE = {
    SourceReliability.OFFICIAL: 1.0,
    SourceReliability.ONCHAIN: 0.95,
    SourceReliability.ANNOUNCEMENT: 0.75,
    SourceReliability.COMMUNITY: 0.4,
    SourceReliability.UNVERIFIED: 0.1,
}

# Default sources used when a cycle is run without an explicit list.
#
# Coingecko's airdrop index was removed from here. It answers 403 to any
# non-browser client, so every cycle spent a request on a source that could only
# ever fail, and pushed a permanent, meaningless entry into the cycle's failure
# report. The failure reporting itself is unchanged and still reports sources
# that break for real reasons -- the cause is fixed by not asking a source that
# cannot answer, not by muting the report.
DEFAULT_OFFICIAL_SOURCES: List[str] = []

DEFAULT_COMMUNITY_SOURCES: List[str] = [
    "https://airdrops.io",
    "https://airdropalert.com",
]


class VerificationClass(str, Enum):
    CONFIRMED_FACT = "confirmed_fact"      # Verified against an authoritative source
    ESTIMATE = "estimate"                  # Reasoned figure, not confirmed
    INFERENCE = "inference"                # Derived, plausible, unverified
    UNVERIFIED_CLAIM = "unverified_claim"  # Asserted but unsupported
    CONFLICTING = "conflicting"            # Sources disagree
    OUTDATED = "outdated"                  # Previously known, may have changed


class CampaignLifecycle(str, Enum):
    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    PARTICIPATION_AVAILABLE = "participation_available"
    PARTICIPATION_IN_PROGRESS = "participation_in_progress"
    PARTICIPATION_COMPLETED = "participation_completed"
    SNAPSHOT_ANNOUNCED = "snapshot_announced"
    SNAPSHOT_COMPLETED = "snapshot_completed"
    REWARD_ANNOUNCED = "reward_announced"
    CLAIM_NOT_OPEN = "claim_not_open"
    CLAIM_OPEN = "claim_open"
    CLAIM_SUBMITTED = "claim_submitted"
    CLAIM_CONFIRMED = "claim_confirmed"
    DISTRIBUTION_PENDING = "distribution_pending"
    REWARD_RECEIVED = "reward_received"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    UNVERIFIED = "unverified"


@dataclass
class SourceClaimRecord:
    """A single factual claim with full provenance (Specification Part 24)."""

    claim: str
    source_url: str
    source_type: SourceReliability
    observed_at: str
    classification: VerificationClass = VerificationClass.UNVERIFIED_CLAIM
    confidence: float = 0.0
    related_project: Optional[str] = None
    related_task: Optional[str] = None
    supporting_evidence: Dict[str, Any] = field(default_factory=dict)
    conflicting_evidence: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.confidence:
            self.confidence = RELIABILITY_SCORE.get(self.source_type, 0.1)
        if self.classification is VerificationClass.UNVERIFIED_CLAIM and self.source_type in (
            SourceReliability.OFFICIAL,
            SourceReliability.ONCHAIN,
        ):
            # Official/on-chain sources support confirmed facts by default.
            self.classification = VerificationClass.CONFIRMED_FACT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "source_url": self.source_url,
            "source_type": self.source_type.value,
            "observed_at": self.observed_at,
            "classification": self.classification.value,
            "confidence": round(self.confidence, 3),
            "related_project": self.related_project,
            "related_task": self.related_task,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
        }


@dataclass
class DiscoveredCampaign:
    """A campaign candidate assembled from discovery, before verification."""

    name: str
    source_url: str
    source_type: SourceReliability
    network: Optional[str] = None
    description: str = ""
    requirements: Dict[str, Any] = field(default_factory=dict)
    deadlines: Dict[str, str] = field(default_factory=dict)
    risk_factors: List[str] = field(default_factory=list)
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claims: List[SourceClaimRecord] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        """Stable identity for deduplication.

        Uses the registrable domain when available so the same project announced
        from two different pages collapses into one campaign.
        """
        domain = urlparse(self.source_url).netloc.lower().removeprefix("www.")
        if domain:
            return hashlib.sha256(f"{domain}|{self.name.lower().strip()}".encode()).hexdigest()[:32]
        return hashlib.sha256(self.name.lower().strip().encode()).hexdigest()[:32]

    @property
    def dedupe_key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")[:80]

    def reliability(self) -> float:
        if not self.claims:
            return RELIABILITY_SCORE.get(self.source_type, 0.1)
        return max(c.confidence for c in self.claims)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source_url": self.source_url,
            "source_type": self.source_type.value,
            "network": self.network,
            "description": self.description[:1000],
            "requirements": self.requirements,
            "deadlines": self.deadlines,
            "risk_factors": self.risk_factors,
            "discovered_at": self.discovered_at,
            "fingerprint": self.fingerprint,
            "reliability": round(self.reliability(), 3),
            "claims": [c.to_dict() for c in self.claims],
        }


class CampaignDeduplicator:
    """Collapses duplicate discoveries into a single campaign with merged claims."""

    def __init__(self) -> None:
        self._by_fingerprint: Dict[str, DiscoveredCampaign] = {}

    def add(self, campaign: DiscoveredCampaign) -> tuple[DiscoveredCampaign, bool]:
        """Returns (campaign, is_new). Merges claims when a duplicate arrives."""
        existing = self._by_fingerprint.get(campaign.fingerprint)
        if existing is None:
            self._by_fingerprint[campaign.fingerprint] = campaign
            return campaign, True

        known = {c.claim for c in existing.claims}
        for claim in campaign.claims:
            if claim.claim not in known:
                existing.claims.append(claim)
                known.add(claim.claim)

        # Prefer the higher-reliability source for descriptive fields.
        if campaign.reliability() > existing.reliability():
            existing.source_url = campaign.source_url
            existing.source_type = campaign.source_type
            existing.network = campaign.network or existing.network
            existing.description = campaign.description or existing.description

        for key, value in (campaign.deadlines or {}).items():
            existing.deadlines.setdefault(key, value)
        for risk in campaign.risk_factors:
            if risk not in existing.risk_factors:
                existing.risk_factors.append(risk)

        return existing, False

    def campaigns(self) -> List[DiscoveredCampaign]:
        return list(self._by_fingerprint.values())

    def __len__(self) -> int:
        return len(self._by_fingerprint)


RISK_SIGNALS = {
    "requires_upfront_deposit": "Requests an upfront deposit before participation.",
    "seed_phrase_request": "Asks for a seed phrase. Almost certainly a scam.",
    "guaranteed_returns": "Promises guaranteed or fixed returns, a strong scam signal.",
    "unknown_contract": "Contract address is unverified.",
    "urgent_deadline_pressure": "Uses artificial urgency to force an immediate action.",
}


def assess_risk(campaign: DiscoveredCampaign, text: str = "") -> List[str]:
    """Flag scam indicators. These inform the user; they never auto-block a
    legitimate campaign without evidence."""
    lowered = text.lower()
    risks = list(campaign.risk_factors)

    if "seed phrase" in lowered or "private key" in lowered or "mnemonic" in lowered:
        risks.append(RISK_SIGNALS["seed_phrase_request"])
    if re.search(r"guaranteed?\s+(returns?|apy|profit)", lowered):
        risks.append(RISK_SIGNALS["guaranteed_returns"])
    if re.search(r"(act now|hurry|limited time|last chance).{0,40}(deposit|connect|sign)", lowered):
        risks.append(RISK_SIGNALS["urgent_deadline_pressure"])
    return list(dict.fromkeys(risks))


# ======================================================================
# Discovery Workflow Functions
# ======================================================================

async def discover_from_sources(
    source_urls: List[str],
    source_type: SourceReliability = SourceReliability.COMMUNITY,
    network: Optional[str] = None,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> List[DiscoveredCampaign]:
    """Discover airdrop campaigns from a list of source URLs.

    This is the main discovery workflow that uses web tools to fetch and
    analyze content for airdrop-related information.

    `failures`, when supplied, collects one entry per source that could not be
    read. Sources used to disappear silently: a 403 or a DNS failure was logged
    and skipped, so a cycle that checked three sources and found nothing was
    indistinguishable from a cycle that found three sources and found nothing.
    """
    if not source_urls:
        return []

    # Use the SiteSearchTool to scan for campaign keywords
    site_search_tool = registry.get("site_search")
    if not site_search_tool:
        logger.warning("SiteSearchTool not available in registry")
        if failures is not None:
            failures.append(
                {
                    "url": None,
                    "source_type": source_type.value,
                    "error": "site_search tool is not registered",
                    "category": "TOOL_UNAVAILABLE",
                    "retryable": False,
                }
            )
        return []

    campaigns: List[DiscoveredCampaign] = []
    deduplicator = CampaignDeduplicator()

    for url in source_urls:
        try:
            # Search the site for airdrop-related content
            result = await site_search_tool.execute({
                "url": url,
                "keywords": ["airdrop", "claim", "snapshot", "reward", "droplet", "earn"]
            })

            if not result.ok:
                logger.warning("Failed to search %s: %s", url, result.error)
                if failures is not None:
                    failures.append(
                        {
                            "url": url,
                            "source_type": source_type.value,
                            "error": result.error,
                            "category": result.error_category or "TOOL_FAILURE",
                            "retryable": bool(result.retryable),
                        }
                    )
                continue

            matches = result.data.get("matches", [])
            if not matches:
                continue

            # For each match, create a potential campaign
            for match in matches[:10]:  # Limit to prevent excessive processing
                # Extract potential campaign name from context
                name = _extract_campaign_name(match, url)
                if not name:
                    continue

                # Create a discovered campaign
                campaign = DiscoveredCampaign(
                    name=name,
                    source_url=url,
                    source_type=source_type,
                    network=network,
                    description=match[:200],  # First 200 chars as description
                    discovered_at=datetime.now(timezone.utc).isoformat()
                )

                # Add basic claims based on the match
                if "airdrop" in match.lower():
                    campaign.claims.append(SourceClaimRecord(
                        claim=f"Airdrop opportunity mentioned at {url}",
                        source_url=url,
                        source_type=source_type,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        classification=VerificationClass.UNVERIFIED_CLAIM
                    ))

                if "claim" in match.lower():
                    campaign.claims.append(SourceClaimRecord(
                        claim=f"Claim opportunity mentioned at {url}",
                        source_url=url,
                        source_type=source_type,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        classification=VerificationClass.UNVERIFIED_CLAIM
                    ))

                # Add to deduplicator
                deduped_campaign, is_new = deduplicator.add(campaign)
                if is_new:
                    campaigns.append(deduped_campaign)

        except Exception as exc:
            logger.exception("Error discovering from %s: %s", url, exc)
            continue

    logger.info("Discovered %s campaigns from %s sources", len(campaigns), len(source_urls))
    return campaigns


async def discover_from_documentation(
    doc_urls: List[str],
    source_type: SourceReliability = SourceReliability.OFFICIAL,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> List[DiscoveredCampaign]:
    """Discover campaigns from official documentation pages.

    This workflow focuses on extracting eligibility, requirements, and
    deadline information from official docs. Unreadable sources are collected into
    `failures` for the same reason as in :func:`discover_from_sources`.
    """
    if not doc_urls:
        return []

    # Use the DocumentationReaderTool
    doc_tool = registry.get("read_documentation")
    if not doc_tool:
        logger.warning("DocumentationReaderTool not available in registry")
        if failures is not None:
            failures.append(
                {
                    "url": None,
                    "source_type": source_type.value,
                    "error": "read_documentation tool is not registered",
                    "category": "TOOL_UNAVAILABLE",
                    "retryable": False,
                }
            )
        return []

    campaigns: List[DiscoveredCampaign] = []
    deduplicator = CampaignDeduplicator()

    for url in doc_urls:
        try:
            result = await doc_tool.execute({
                "url": url,
                "sections": ["eligib", "require", "claim", "deadline", "airdrop", "reward"]
            })

            if not result.ok:
                logger.warning("Failed to read documentation %s: %s", url, result.error)
                if failures is not None:
                    failures.append(
                        {
                            "url": url,
                            "source_type": source_type.value,
                            "error": result.error,
                            "category": result.error_category or "TOOL_FAILURE",
                            "retryable": bool(result.retryable),
                        }
                    )
                continue

            sections = result.data.get("sections", {})

            # Check if this documentation contains airdrop-related information
            has_airdrop_info = any(
                key in ["eligib", "require", "claim", "deadline", "airdrop", "reward"]
                and sections.get(key)
                for key in sections
            )

            if not has_airdrop_info:
                continue

            # Extract campaign name from URL or content
            name = _extract_campaign_name_from_url(url)
            if not name:
                # Try to get from title or first section
                all_text = ""
                for section_list in sections.values():
                    all_text += " ".join(section_list[:3])  # First 3 items from each section
                name = _extract_campaign_name(all_text, url) or f"Campaign from {url}"

            campaign = DiscoveredCampaign(
                name=name,
                source_url=url,
                source_type=source_type,
                description="; ".join([
                    f"{section}: {'; '.join(items[:2])}"
                    for section, items in sections.items()
                    if items
                ])[:500],
                discovered_at=datetime.now(timezone.utc).isoformat()
            )

            # Extract requirements and deadlines
            if sections.get("require"):
                campaign.requirements = {
                    "extracted_text": sections["require"][:5]
                }

            if sections.get("deadline"):
                campaign.deadlines = {
                    "extracted_text": sections["deadline"][:5]
                }

            # Add claims for each relevant section
            for section_type in ["eligib", "require", "claim", "deadline", "reward"]:
                if sections.get(section_type):
                    campaign.claims.append(SourceClaimRecord(
                        claim=f"{section_type.title()} information found in documentation",
                        source_url=url,
                        source_type=source_type,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        classification=VerificationClass.CONFIRMED_FACT,  # Official docs
                        related_project=name
                    ))

            deduped_campaign, is_new = deduplicator.add(campaign)
            if is_new:
                campaigns.append(deduped_campaign)

        except Exception as exc:
            logger.exception("Error reading documentation from %s: %s", url, exc)
            continue

    logger.info("Discovered %s campaigns from documentation", len(campaigns))
    return campaigns


def _extract_campaign_name(text: str, source_url: str = "") -> Optional[str]:
    """Extract a campaign name from text content."""
    if not text:
        return None

    # Look for common patterns
    patterns = [
        r"([A-Z][a-zA-Z0-9_\s]{2,30})\s+(?:airdrop|drop|reward|claim)",
        r"(?:airdrop|drop|reward|claim)\s+([A-Z][a-zA-Z0-9_\s]{2,30})",
        r"([A-Z][a-zA-Z0-9_\s]{2,30})\s+(?:season|phase|round)\s+\d+",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            # Clean up the name
            name = re.sub(r"\s+", " ", name)  # Normalize whitespace
            if len(name) >= 3:  # Reasonable minimum length
                return name

    # Fallback: try to get from URL
    if source_url:
        return _extract_campaign_name_from_url(source_url)

    return None


def _extract_campaign_name_from_url(url: str) -> Optional[str]:
    """Extract a campaign name from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")

        # Take the first subdomain or domain part
        if domain:
            parts = domain.split(".")
            # Skip common www, blog, docs prefixes
            if parts[0] in ["www", "blog", "docs", "help", "support"]:
                if len(parts) > 1:
                    return parts[1].upper()
            else:
                return parts[0].upper()
    except Exception:
        pass

    return None


async def run_discovery_cycle(
    discovery_sources: Optional[Dict[str, List[str]]] = None
) -> Dict[str, Any]:
    """Run a complete discovery cycle.

    This is the main entry point that would be called by the scheduler.

    Args:
        discovery_sources: Dict mapping source types to lists of URLs.
                          If None, uses default configured sources.

    Returns:
        Dict with discovery results and statistics.
    """
    if discovery_sources is None:
        # Copied so a caller mutating the returned list cannot corrupt the defaults
        # for the next cycle.
        discovery_sources = {
            "official": list(DEFAULT_OFFICIAL_SOURCES),
            "community": list(DEFAULT_COMMUNITY_SOURCES),
        }

    start_time = datetime.now(timezone.utc)
    all_campaigns: List[DiscoveredCampaign] = []
    results_by_source: Dict[str, Any] = {}
    # Sources that could not be read. Reported to the caller so a partial cycle is
    # never presented as a complete one.
    failures: List[Dict[str, Any]] = []

    # Discover from official sources
    if discovery_sources.get("official"):
        official_campaigns = await discover_from_documentation(
            discovery_sources["official"],
            SourceReliability.OFFICIAL,
            failures=failures,
        )
        all_campaigns.extend(official_campaigns)
        results_by_source["official"] = {
            "count": len(official_campaigns),
            "sources_checked": len(discovery_sources["official"]),
        }

    # Discover from community sources
    if discovery_sources.get("community"):
        community_campaigns = await discover_from_sources(
            discovery_sources["community"],
            SourceReliability.COMMUNITY,
            failures=failures,
        )
        all_campaigns.extend(community_campaigns)
        results_by_source["community"] = {
            "count": len(community_campaigns),
            "sources_checked": len(discovery_sources["community"]),
        }

    # TODO: Add discovery from social media, forums, etc.

    end_time = datetime.now(timezone.utc)

    return {
        "status": "completed" if not failures else "completed_with_source_failures",
        "campaigns_discovered": len(all_campaigns),
        "campaigns": [camp.to_dict() for camp in all_campaigns[:50]],  # Limit response size
        "results_by_source": results_by_source,
        "source_failures": failures,
        "sources_failed": len(failures),
        "duration_seconds": (end_time - start_time).total_seconds(),
        "timestamp": end_time.isoformat()
    }