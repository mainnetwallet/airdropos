"""Database-Backed Persistent Memory Manager.

Distinguishes verified facts from inferences, supports time-based revalidation,
and NEVER stores secrets (Specification Part 26).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import MemoryRecord
from backend.app.models.memory import SCOPE_CAMPAIGN, SCOPE_GLOBAL, SCOPE_TASK

logger = logging.getLogger(__name__)

# Patterns that must never be written to ordinary memory.
SECRET_PATTERNS = [
    r"0x[a-fA-F0-9]{64}",                  # private keys / tx hashes of raw keys
    r"\b(?:seed|mnemonic|recovery)\s+phrase\b",
    r"\bprivate[_ ]key\b",
    r"\b(?:api|secret|master)[_ ]key\b\s*[:=]",
    r"\bapi[_-]?key\s*[:=]\s*\S{12,}",
    r"\btoken\s*[:=]\s*\S{16,}",
]


class SecretLeakError(Exception):
    """Raised when content matching a secret pattern is about to be stored."""


# The only model attributes a caller may set through `remember(extra=...)`. Everything
# else - notably `content` and `source`, which are secret-screened - is out of reach, so
# the escape hatch cannot be used to write unscreened text.
_SETTABLE_BY_CALLER = frozenset({"scope", "task_id", "campaign_id"})


class MemoryManager:
    """Stores and retrieves agent memory with confidence and revalidation."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    @staticmethod
    def assert_no_secrets(content: str) -> None:
        """Hard block on secret persistence. Memory is not a secret store."""
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                raise SecretLeakError(
                    f"Refusing to store content matching a secret pattern: {pattern}"
                )

    async def remember(
        self,
        category: str,
        content: str,
        source: str,
        *,
        confidence: float = 0.7,
        is_inference: bool = False,
        related_entities: Optional[Dict[str, Any]] = None,
        ttl_days: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        """Persist a memory record after screening it for secrets.

        This is the single write path, so the context hierarchy is enforced HERE rather
        than only in `remember_scoped`. Previously `remember_scoped` validated the
        scope/owner pair and `remember` did not, and `extra` could `setattr` any model
        attribute - so a caller reaching `remember` directly could write a "global" record
        carrying a task_id, which is precisely the private-data-published-as-general
        mistake the hierarchy exists to prevent.

        `extra` is restricted to `_SETTABLE_BY_CALLER`. An allowlist, because `extra` is
        an escape hatch for attributes outside the signature; a dict of arbitrary model
        fields would be a way to set `content` or `source` without screening them.
        """
        self.assert_no_secrets(content)

        scope, task_id, campaign_id = self._validate_scope_owner(
            (extra or {}).get("scope", SCOPE_GLOBAL),
            (extra or {}).get("task_id"),
            (extra or {}).get("campaign_id"),
        )

        record = MemoryRecord(
            category=category,
            content=content,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            is_inference=is_inference,
            related_entities=related_entities or {},
            expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days) if ttl_days else None,
            last_verified_at=datetime.now(timezone.utc) if not is_inference else None,
            review_policy={"revalidate_after_days": 30 if category.endswith("_REQUIREMENT") else 90},
            scope=scope,
            task_id=task_id,
            campaign_id=campaign_id,
        )
        for key, value in (extra or {}).items():
            if key in _SETTABLE_BY_CALLER and value is not None and hasattr(record, key):
                setattr(record, key, value)
        self.db.add(record)
        await self.db.flush()
        return record

    @staticmethod
    def _validate_scope_owner(
        scope: Any, task_id: Any, campaign_id: Any
    ) -> tuple:
        """Refuse every scope/owner combination that cannot mean what it claims.

        * a TASK or CAMPAIGN record with no owner would be private context nobody can
          read back - silent data loss;
        * a GLOBAL record carrying an owner is private data published as general
          knowledge, which any task could then recall.
        """
        if scope not in (SCOPE_GLOBAL, SCOPE_CAMPAIGN, SCOPE_TASK):
            raise ValueError(f"Unknown memory scope: {scope!r}")
        if scope == SCOPE_TASK and task_id is None:
            raise ValueError("A task-scoped memory must name its task.")
        if scope == SCOPE_CAMPAIGN and campaign_id is None:
            raise ValueError("A campaign-scoped memory must name its campaign.")
        if scope == SCOPE_GLOBAL and (task_id is not None or campaign_id is not None):
            raise ValueError(
                "A global memory cannot belong to a task or campaign; that is what "
                "'global' means. Write it at the narrower scope instead."
            )
        # A record cannot belong to a task and a campaign at once: it would be readable
        # by two different audiences on two different terms.
        if scope == SCOPE_TASK and campaign_id is not None:
            raise ValueError("A task-scoped memory cannot also name a campaign.")
        if scope == SCOPE_CAMPAIGN and task_id is not None:
            raise ValueError("A campaign-scoped memory cannot also name a task.")
        return scope, task_id, campaign_id

    # ------------------------------------------------------------------
    # Context hierarchy
    #
    # Memory is three levels, and the levels are enforced here rather than trusted to
    # each call site: a TASK record is readable only by that task, a CAMPAIGN record only
    # by tasks linked to that campaign, and only GLOBAL records are general knowledge.
    # Before this, every record was global, so any recall returned one task's private
    # execution history to another task's prompt.
    # ------------------------------------------------------------------
    async def remember_scoped(
        self,
        category: str,
        content: str,
        source: str,
        *,
        scope: str = SCOPE_GLOBAL,
        task_id: Optional[Any] = None,
        campaign_id: Optional[Any] = None,
        confidence: float = 0.7,
        is_inference: bool = False,
        related_entities: Optional[Dict[str, Any]] = None,
        ttl_days: Optional[int] = None,
    ) -> MemoryRecord:
        """Store a memory at an explicit level of the context hierarchy.

        The scope and the owner must agree. A TASK record with no task, or a GLOBAL record
        carrying one, are both refused: the first would be private context nobody can
        read back, the second would be private context published as general knowledge.
        """
        if scope not in (SCOPE_GLOBAL, SCOPE_CAMPAIGN, SCOPE_TASK):
            raise ValueError(f"Unknown memory scope: {scope!r}")
        if scope == SCOPE_TASK and task_id is None:
            raise ValueError("A task-scoped memory must name its task.")
        if scope == SCOPE_CAMPAIGN and campaign_id is None:
            raise ValueError("A campaign-scoped memory must name its campaign.")
        if scope == SCOPE_GLOBAL and (task_id is not None or campaign_id is not None):
            raise ValueError(
                "A global memory cannot belong to a task or campaign; that is what "
                "'global' means. Write it at the narrower scope instead."
            )

        return await self.remember(
            category,
            content,
            source,
            confidence=confidence,
            is_inference=is_inference,
            related_entities=related_entities,
            ttl_days=ttl_days,
            extra={"scope": scope, "task_id": task_id, "campaign_id": campaign_id},
        )

    async def recall_for_context(
        self,
        *,
        task_id: Optional[Any] = None,
        campaign_id: Optional[Any] = None,
        query: str = "",
        categories: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[MemoryRecord]:
        """Recall only what this task is entitled to see.

        The three levels are unioned, never substituted for one another, and the result is
        ordered so this task's own context is read before general knowledge.

        The GLOBAL branch requires BOTH owner columns to be NULL. Matching on the scope
        alone would hand any task a record that claims to be general knowledge while
        carrying a task_id - the exact shape `remember` now refuses to write, and which a
        row written before that check, or by any other writer, could still hold.
        """
        global_clause = (
            (MemoryRecord.scope == SCOPE_GLOBAL)
            & MemoryRecord.task_id.is_(None)
            & MemoryRecord.campaign_id.is_(None)
        )
        owner_clauses = [global_clause]
        if task_id is not None:
            owner_clauses.append(
                (MemoryRecord.scope == SCOPE_TASK) & (MemoryRecord.task_id == task_id)
            )
        if campaign_id is not None:
            owner_clauses.append(
                (MemoryRecord.scope == SCOPE_CAMPAIGN)
                & (MemoryRecord.campaign_id == campaign_id)
            )

        stmt = select(MemoryRecord).where(or_(*owner_clauses))
        if categories:
            stmt = stmt.where(MemoryRecord.category.in_(categories))
        if query:
            stmt = stmt.where(MemoryRecord.content.ilike(f"%{query}%"))
        stmt = stmt.order_by(MemoryRecord.confidence.desc()).limit(limit)

        records = list((await self.db.execute(stmt)).scalars().all())
        for record in records:
            record.access_count += 1
        await self.db.flush()
        return records

    # ------------------------------------------------------------------
    async def recall(
        self,
        query: str = "",
        categories: Optional[List[str]] = None,
        limit: int = 20,
        include_expired: bool = False,
    ) -> List[MemoryRecord]:
        """Retrieve relevant memories. Loads selectively rather than everything.

        Administrative view across every scope, for the operator's own console. Agent
        decisions must go through `recall_for_context`, which enforces the hierarchy.
        """
        stmt = select(MemoryRecord)

        if categories:
            stmt = stmt.where(MemoryRecord.category.in_(categories))
        if not include_expired:
            stmt = stmt.where(
                (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > datetime.now(timezone.utc))
            )
        if query:
            stmt = stmt.where(MemoryRecord.content.ilike(f"%{query}%"))

        stmt = stmt.order_by(MemoryRecord.confidence.desc()).limit(limit)
        records = list((await self.db.execute(stmt)).scalars().all())

        for record in records:
            record.access_count += 1
        await self.db.flush()
        return records

    async def get_needing_revalidation(self, limit: int = 50) -> List[MemoryRecord]:
        """Time-sensitive memories whose TTL has lapsed."""
        stmt = (
            select(MemoryRecord)
            .where(MemoryRecord.expires_at.is_not(None))
            .where(MemoryRecord.expires_at <= datetime.now(timezone.utc))
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def extend_ttl(self, record_id, days: int = 30) -> None:
        """Re-validate a memory by extending its validity window."""
        record = await self.db.get(MemoryRecord, record_id)
        if record is None:
            return
        record.expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        record.last_verified_at = datetime.now(timezone.utc)
        record.confidence = min(1.0, record.confidence + 0.05)
        await self.db.flush()

    # ------------------------------------------------------------------
    async def consolidate(self, category: Optional[str] = None) -> Dict[str, int]:
        """Merge near-duplicate memories to reduce redundancy.

        Keeps the highest-confidence instance and lowers the confidence of
        duplicates rather than deleting data outright.
        """
        stmt = select(MemoryRecord).order_by(MemoryRecord.content)
        if category:
            stmt = stmt.where(MemoryRecord.category == category)

        records = list((await self.db.execute(stmt)).scalars().all())
        merged = 0
        seen: Dict[str, MemoryRecord] = {}

        for record in records:
            key = re.sub(r"\W+", "", record.content.lower())[:120]
            existing = seen.get(key)
            if existing is None:
                seen[key] = record
                continue
            keeper, dupe = (
                (existing, record) if existing.confidence >= record.confidence else (record, existing)
            )
            keeper.confidence = min(1.0, keeper.confidence + 0.03)
            dupe.confidence = max(0.0, dupe.confidence - 0.05)
            seen[key] = keeper
            merged += 1

        await self.db.flush()
        return {"scanned": len(records), "duplicates_merged": merged}

    async def stats(self) -> Dict[str, Any]:
        records = list((await self.db.execute(select(MemoryRecord))).scalars().all())
        by_category: Dict[str, int] = {}
        for record in records:
            by_category[record.category] = by_category.get(record.category, 0) + 1
        return {
            "total": len(records),
            "by_category": by_category,
            "inferences": sum(1 for r in records if r.is_inference),
            "expired": sum(
                1
                for r in records
                if r.expires_at and r.expires_at <= datetime.now(timezone.utc)
            ),
        }
