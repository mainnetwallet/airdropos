"""Source Verification, Change Detection & Eligibility Evaluation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from backend.app.airdrops.discovery import (
    DiscoveredCampaign,
    SourceClaimRecord,
    SourceReliability,
    VerificationClass,
    assess_risk,
)

logger = logging.getLogger(__name__)

# Actions the agent must never take to farm rewards (Part 15).
DISALLOWED_ACTIVITIES = {
    "create_fake_identity",
    "sybil_multiple_fake_accounts",
    "evade_anti_abuse_controls",
    "impersonate_other_users",
    "use_vpn_to_evade_geofencing_for_anti_abuse",
    "automate_engagement_metrics",
}


@dataclass
class EligibilityAssessment:
    """Per-wallet eligibility with an explicit confidence and evidence trail."""

    wallet_address: str
    status: str  # ELIGIBLE, INELIGIBLE, UNKNOWN
    confidence: float
    reasons: List[str] = field(default_factory=list)
    unmet_requirements: List[str] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "unmet_requirements": self.unmet_requirements,
            "checked_at": self.checked_at,
        }


@dataclass
class ChangeRecord:
    """An observed change to a tracked attribute (Specification Part 24)."""

    entity_type: str
    entity_id: str
    change_type: str
    old_value: Any
    new_value: Any
    impact: str  # LOW, MEDIUM, HIGH, CRITICAL
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notification_sent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "change_type": self.change_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "impact": self.impact,
            "detected_at": self.detected_at,
            "notification_sent": self.notification_sent,
        }


class SourceVerifier:
    """Cross-checks claims across sources and classifies their reliability."""

    @staticmethod
    def verify_claim(
        claim_text: str,
        sources: List[SourceClaimRecord],
    ) -> SourceClaimRecord:
        """Reconcile multiple sources for one claim into a single verdict."""
        if not sources:
            return SourceClaimRecord(
                claim=claim_text,
                source_url="",
                source_type=SourceReliability.UNVERIFIED,
                observed_at=datetime.now(timezone.utc).isoformat(),
                classification=VerificationClass.UNVERIFIED_CLAIM,
                confidence=0.0,
            )

        official = [s for s in sources if s.source_type is SourceReliability.OFFICIAL]
        onchain = [s for s in sources if s.source_type is SourceReliability.ONCHAIN]

        if onchain:
            verdict = "On-chain state confirms the claim."
            classification = VerificationClass.CONFIRMED_FACT
            confidence = 0.95
        elif official:
            verdict = "Confirmed by the project's official source."
            classification = VerificationClass.CONFIRMED_FACT
            confidence = 0.85
        else:
            community = [s for s in sources if s.source_type is SourceReliability.COMMUNITY]
            if len(sources) > 1 and len({s.claim for s in sources}) > 1:
                verdict = "Sources conflict on this claim."
                classification = VerificationClass.CONFLICTING
                confidence = 0.3
            elif community:
                verdict = "Community-reported only; not confirmed by an official source."
                classification = VerificationClass.UNVERIFIED_CLAIM
                confidence = 0.4
            else:
                verdict = "Announcement-level evidence only."
                classification = VerificationClass.INFERENCE
                confidence = 0.6

        merged = max(sources, key=lambda s: s.confidence)
        merged.classification = classification
        merged.confidence = confidence
        conflicting = [
            s.source_url for s in sources if s.source_type is SourceReliability.COMMUNITY
        ] if classification is VerificationClass.CONFLICTING else []
        merged.conflicting_evidence = conflicting
        merged.claim = f"{claim_text} ({verdict})"
        return merged

    @staticmethod
    def score_source_reliability(sources: List[SourceClaimRecord]) -> float:
        """Average reliability across a campaign's sources."""
        if not sources:
            return 0.0
        return sum(s.confidence for s in sources) / len(sources)


class EligibilityEvaluator:
    """Evaluates per-wallet eligibility from campaign requirements.

    Eligibility is tracked SEPARATELY per wallet and never conflated with
    participation completion or reward receipt.
    """

    def __init__(self) -> None:
        self.disallowed = DISALLOWED_ACTIVITIES

    def evaluate(
        self,
        campaign: DiscoveredCampaign,
        wallet_address: str,
        wallet_signals: Dict[str, Any],
    ) -> EligibilityAssessment:
        """Assess whether a wallet plausibly meets the campaign's requirements."""
        requirements = campaign.requirements or {}
        reasons: List[str] = []
        unmet: List[str] = []
        confidences: List[float] = []

        min_balance = requirements.get("minimum_balance_native")
        if min_balance is not None:
            balance = wallet_signals.get("balance_native")
            if balance is None:
                unmet.append("Wallet balance unknown")
                confidences.append(0.3)
            elif balance >= min_balance:
                reasons.append(f"Balance {balance} meets minimum {min_balance}")
                confidences.append(0.9)
            else:
                unmet.append(f"Balance {balance} below required {min_balance}")
                confidences.append(0.9)

        required_networks = requirements.get("required_networks") or []
        if required_networks:
            wallet_networks = set(wallet_signals.get("networks") or [])
            if wallet_networks.intersection(required_networks):
                reasons.append("Wallet is active on a required network")
                confidences.append(0.85)
            else:
                unmet.append(
                    f"Wallet has no activity on required network(s): {', '.join(required_networks)}"
                )
                confidences.append(0.8)

        min_transactions = requirements.get("minimum_transactions")
        if min_transactions is not None:
            count = wallet_signals.get("transaction_count", 0)
            if count >= min_transactions:
                reasons.append(f"Transaction count {count} meets minimum {min_transactions}")
                confidences.append(0.85)
            else:
                unmet.append(f"Only {count} transactions; {min_transactions} required")
                confidences.append(0.85)

        disallowed_requested = [
            act for act in requirements.get("activities", []) if act in self.disallowed
        ]
        if disallowed_requested:
            # These are refused regardless of eligibility.
            return EligibilityAssessment(
                wallet_address=wallet_address,
                status="INELIGIBLE",
                confidence=1.0,
                reasons=[
                    "Campaign requests activities that violate the anti-abuse policy: "
                    + ", ".join(disallowed_requested)
                ],
            )

        if not requirements:
            return EligibilityAssessment(
                wallet_address=wallet_address,
                status="UNKNOWN",
                confidence=0.0,
                reasons=["Campaign has no machine-checkable requirements. Manual review required."],
            )

        confidence = sum(confidences) / len(confidences) if confidences else 0.4
        status = "ELIGIBLE" if not unmet else ("INELIGIBLE" if confidence >= 0.8 else "UNKNOWN")

        return EligibilityAssessment(
            wallet_address=wallet_address,
            status=status,
            confidence=confidence,
            reasons=reasons,
            unmet_requirements=unmet,
        )


class ChangeDetector:
    """Diffs current observations against stored state and emits change records."""

    HIGH_IMPACT_FIELDS = {
        "requirements", "eligibility_rules", "claim_contract_address", "claim_deadline",
        "network", "contract_address", "claim_open", "reward_announced",
    }

    def __init__(self) -> None:
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._notified: set[str] = set()

    def diff(
        self, entity_type: str, entity_id: str, current: Dict[str, Any]
    ) -> List[ChangeRecord]:
        """Compare the current observation to the last stored one."""
        key = f"{entity_type}:{entity_id}"
        previous = self._seen.get(key, {})
        changes: List[ChangeRecord] = []

        for attribute, new_value in current.items():
            if attribute not in previous:
                continue
            old_value = previous[attribute]
            if old_value == new_value:
                continue
            changes.append(
                ChangeRecord(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    change_type=attribute,
                    old_value=old_value,
                    new_value=new_value,
                    impact="HIGH" if attribute in self.HIGH_IMPACT_FIELDS else "LOW",
                )
            )

        self._seen[key] = dict(current)
        return changes

    @staticmethod
    def build_report(changes: List[ChangeRecord]) -> Dict[str, Any]:
        """Structured difference report for the dashboard and notifications."""
        high = [c for c in changes if c.impact in ("HIGH", "CRITICAL")]
        return {
            "change_count": len(changes),
            "high_impact_count": len(high),
            "requires_replanning": any(c.impact == "HIGH" for c in changes),
            "changes": [c.to_dict() for c in changes],
        }


def campaign_risk_score(campaign: DiscoveredCampaign, text: str = "") -> Tuple[float, List[str]]:
    """Return a 0-10 risk score and the reasons behind it."""
    risks = assess_risk(campaign, text)
    score = min(10.0, len(risks) * 2.5)
    if not campaign.source_url.startswith("https://"):
        score += 1.0
    return round(min(10.0, score), 1), risks
