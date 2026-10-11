"""Database-Backed Persistent Memory Manager.

Distinguishes verified facts from inferences, supports time-based revalidation,
and NEVER stores secrets (Specification Part 26).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import MemoryRecord

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
    ) -> MemoryRecord:
        """Persist a memory record after screening it for secrets."""
        self.assert_no_secrets(content)

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
        )
        self.db.add(record)
        await self.db.flush()
        return record

    # ------------------------------------------------------------------
    async def recall(
        self,
        query: str = "",
        categories: Optional[List[str]] = None,
        limit: int = 20,
        include_expired: bool = False,
    ) -> List[MemoryRecord]:
        """Retrieve relevant memories. Loads selectively rather than everything."""
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
