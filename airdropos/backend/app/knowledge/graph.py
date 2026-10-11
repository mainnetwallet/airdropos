"""Knowledge Graph Service.

Typed, weighted edges between platform entities -- projects, wallets, campaigns,
tasks, tools, sources, and memory records.

The `KnowledgeRelationship` model existed with no writer and no reader: nothing
ever created an edge, so the graph could not answer anything. This service is the
writer and the query layer.

Design notes:

- **Edges are typed and directed.** `A REQUIRES B` is not `B REQUIRES A`, and
  collapsing them would let the planner reason backwards from a consequence to
  its cause.
- **Edges decay.** A weight below `MIN_WEIGHT` stops being returned. Knowledge
  about airdrop campaigns goes stale, and a graph that only ever grows returns
  stale answers with total confidence.
- **The service is read-only with respect to value.** It creates relationships
  between records; it never moves funds, signs, or dispatches actions. Linking a
  wallet to a campaign is a statement about structure, not authority to act.
- **Entity ids are opaque strings, not foreign keys.** A campaign may reference a
  wallet row that has since been deleted, and a task may cite a source URL that
  was never a row at all. Enforcing referential integrity across those would make
  the graph refuse to record the things it exists to record. Deletion is handled
  explicitly in `prune_entity` instead of by cascade.

A note on the migration: `1_initial_schema.py` creates `knowledge_relationships`
with a `from_memory_id`/`to_memory_id` shape linking two `memory_records` rows,
which does not match the ORM model at all (no `id`, no entity types, no weight,
and a `properties` column the migration lacks). A fresh database built from that
migration will fail on the first insert. This is tracked in
`IMPLEMENTATION_CHECKLIST.md`; the service follows the ORM, which is the shape the
rest of the application reads.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.memory.manager import MemoryManager, SecretLeakError
from backend.app.models import KnowledgeRelationship, MemoryRecord

logger = logging.getLogger(__name__)

# Entity types the graph understands. Anything else is rejected rather than
# stored, because an untyped entity makes a traversal unanswerable.
ENTITY_TYPES = frozenset(
    {"PROJECT", "WALLET", "TASK", "CAMPAIGN", "TOOL", "SOURCE", "MEMORY"}
)

# Relationship vocabulary. Kept open-ended -- project-specific predicates are
# legitimate -- but the constants below are what the query helpers understand.
RELATIONSHIP_TYPES = frozenset(
    {
        "REQUIRES",       # campaign REQUIRES wallet
        "PARTICIPATES_IN",# wallet PARTICIPATES_IN campaign
        "USES",           # task USES tool
        "BELONGS_TO",     # task BELONGS_TO project
        "DEPENDS_ON",     # task DEPENDS_ON wallet / campaign
        "DERIVED_FROM",   # memory DERIVED_FROM source
        "SUPPORTS",       # source SUPPORTS memory
        "CONTRADICTS",    # memory CONTRADICTS memory
        "BLOCKED_BY",
        "PREPARES_FOR",
        "AFFECTS",
    }
)

# An edge lighter than this is treated as noise and filtered from reads.
MIN_WEIGHT = 0.05
MAX_WEIGHT = 1.0
DEFAULT_WEIGHT = 1.0

# Traversal bounds. A graph with no cap on depth is a denial-of-service vector
# against the planner, which traverses it on every task.
MAX_TRAVERSAL_DEPTH = 5
MAX_TRAVERSAL_NODES = 500

# Relationships that imply a dependency, used by `find_blockers`.
BLOCKING_TYPES = frozenset({"DEPENDS_ON", "BLOCKED_BY", "REQUIRES"})


class KnowledgeGraphError(Exception):
    """A graph operation was refused or failed."""


class InvalidRelationship(KnowledgeGraphError):
    """The proposed relationship is malformed or uses unknown vocabulary."""


class EntityNotFound(KnowledgeGraphError):
    """A traversal referenced an entity that has no rows."""


class TraversalLimitExceeded(KnowledgeGraphError):
    """The traversal hit its node or depth budget and was stopped."""


@dataclass(frozen=True)
class EntityRef:
    """A typed reference to a node. `id` is opaque and stored as a string."""

    entity_type: str
    entity_id: str

    def __post_init__(self) -> None:
        if self.entity_type not in ENTITY_TYPES:
            raise InvalidRelationship(
                f"Unknown entity_type '{self.entity_type}'. "
                f"Known types: {', '.join(sorted(ENTITY_TYPES))}"
            )
        if not self.entity_id or len(self.entity_id) > 64:
            raise InvalidRelationship(
                "entity_id must be a non-empty string of at most 64 characters."
            )

    @property
    def key(self) -> str:
        return f"{self.entity_type}:{self.entity_id}"


@dataclass
class TraversalNode:
    """A node reached during a traversal, with the path that reached it."""

    ref: EntityRef
    depth: int
    relationship_type: str
    weight: float
    via: Optional[EntityRef] = None


@dataclass
class TraversalResult:
    nodes: List[TraversalNode]
    truncated: bool = False
    depth_reached: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {
                    "entity_type": n.ref.entity_type,
                    "entity_id": n.ref.entity_id,
                    "relationship_type": n.relationship_type,
                    "weight": round(n.weight, 4),
                    "depth": n.depth,
                    "via": n.via.key if n.via else None,
                }
                for n in self.nodes
            ],
            "truncated": self.truncated,
            "depth_reached": self.depth_reached,
            "count": len(self.nodes),
        }


def _validate_relationship(relationship_type: str) -> str:
    if not relationship_type or len(relationship_type) > 100:
        raise InvalidRelationship("relationship_type must be 1-100 characters.")
    return relationship_type


def _clamp_weight(weight: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, float(weight)))


class KnowledgeGraphService:
    """Create, strengthen, weaken, query, and traverse entity relationships."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    async def link(
        self,
        source: EntityRef,
        relationship_type: str,
        target: EntityRef,
        *,
        weight: float = DEFAULT_WEIGHT,
        properties: Optional[Dict[str, Any]] = None,
        decay_half_life_days: Optional[float] = None,
    ) -> KnowledgeRelationship:
        """Create or strengthen an edge.

        Re-linking an existing edge strengthens it rather than creating a
        duplicate: the graph should express how strongly A relates to B, and
        repeated observations of the same fact are evidence, not new facts.
        """
        _validate_relationship(relationship_type)

        if source.key == target.key:
            raise InvalidRelationship(
                "A relationship must connect two different entities."
            )

        payload = dict(properties or {})
        if decay_half_life_days is not None:
            if decay_half_life_days <= 0:
                raise InvalidRelationship("decay_half_life_days must be positive.")
            payload["decay_half_life_days"] = float(decay_half_life_days)

        # Entity ids are opaque, but a weight may carry user-supplied text.
        # Screen it so the graph cannot become a place secrets are persisted.
        try:
            MemoryManager.assert_no_secrets(str(payload))
        except SecretLeakError as exc:
            raise InvalidRelationship(
                "Relationship properties were rejected by the secret screener."
            ) from None

        existing = await self._find(source, relationship_type, target)
        now = datetime.now(timezone.utc)

        if existing is not None:
            existing.weight = _clamp_weight(existing.weight * 0.5 + weight * 0.5)
            existing.properties = {**(existing.properties or {}), **payload}
            await self.db.flush()
            return existing

        edge = KnowledgeRelationship(
            source_entity_type=source.entity_type,
            source_entity_id=source.entity_id,
            target_entity_type=target.entity_type,
            target_entity_id=target.entity_id,
            relationship_type=relationship_type,
            weight=_clamp_weight(weight),
            properties=payload,
        )
        self.db.add(edge)
        try:
            await self.db.flush()
        except IntegrityError:
            # Two workers linked the same pair concurrently. Keep the winner.
            await self.db.rollback()
            found = await self._find(source, relationship_type, target)
            if found is None:
                raise
            return found
        return edge

    async def link_many(
        self,
        edges: Iterable[Tuple[EntityRef, str, EntityRef]],
        *,
        weight: float = DEFAULT_WEIGHT,
        properties: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Link several pairs in one call. Returns how many were written.

        One bad edge fails the batch rather than being skipped silently: a partial
        graph that looks complete is worse than a loud failure.
        """
        count = 0
        for source, rel, target in edges:
            await self.link(source, rel, target, weight=weight, properties=properties)
            count += 1
        return count

    async def weaken(self, source: EntityRef, relationship_type: str, target: EntityRef) -> float:
        """Reduce an edge's weight. Returns the new weight, or 0.0 if absent.

        Halving rather than subtracting keeps a weak edge weak instead of
        resetting it to full strength on the next observation.
        """
        _validate_relationship(relationship_type)
        edge = await self._find(source, relationship_type, target)
        if edge is None:
            return 0.0
        edge.weight = _clamp_weight(edge.weight * 0.5)
        await self.db.flush()
        return edge.weight

    async def unlink(self, source: EntityRef, relationship_type: str, target: EntityRef) -> bool:
        """Remove an edge outright. Used when a relationship is disproved."""
        _validate_relationship(relationship_type)
        edge = await self._find(source, relationship_type, target)
        if edge is None:
            return False
        await self.db.delete(edge)
        await self.db.flush()
        return True

    async def prune_entity(self, ref: EntityRef, *, session: Optional[AsyncSession] = None) -> int:
        """Drop every edge touching an entity. Call when the entity is deleted.

        Edges are not foreign keys (see the module docstring), so nothing removes
        them automatically. A dangling edge pointing at a deleted campaign would
        otherwise keep answering traversals with a reference to nothing.
        """
        db = session or self.db
        stmt = delete(KnowledgeRelationship).where(
            or_(
                (KnowledgeRelationship.source_entity_type == ref.entity_type)
                & (KnowledgeRelationship.source_entity_id == ref.entity_id),
                (KnowledgeRelationship.target_entity_type == ref.entity_type)
                & (KnowledgeRelationship.target_entity_id == ref.entity_id),
            )
        )
        result = await db.execute(stmt)
        await db.flush()
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def _find(
        self,
        source: EntityRef,
        relationship_type: str,
        target: EntityRef,
    ) -> Optional[KnowledgeRelationship]:
        return await self.db.scalar(
            select(KnowledgeRelationship)
            .where(
                KnowledgeRelationship.source_entity_type == source.entity_type,
                KnowledgeRelationship.source_entity_id == source.entity_id,
                KnowledgeRelationship.target_entity_type == target.entity_type,
                KnowledgeRelationship.target_entity_id == target.entity_id,
                KnowledgeRelationship.relationship_type == relationship_type,
            )
            .limit(1)
        )

    async def neighbors(
        self,
        ref: EntityRef,
        *,
        relationship_types: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        min_weight: float = MIN_WEIGHT,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Direct neighbours in the outgoing direction."""
        edges, _saturated = await self._neighbors(
            ref,
            outgoing=True,
            relationship_types=relationship_types,
            entity_types=entity_types,
            min_weight=min_weight,
            limit=limit,
        )
        return edges

    async def incoming(
        self,
        ref: EntityRef,
        *,
        relationship_types: Optional[List[str]] = None,
        min_weight: float = MIN_WEIGHT,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Direct neighbours pointing *at* this entity."""
        edges, _saturated = await self._neighbors(
            ref,
            outgoing=False,
            relationship_types=relationship_types,
            min_weight=min_weight,
            limit=limit,
        )
        return edges

    async def _neighbors(
        self,
        ref: EntityRef,
        *,
        outgoing: bool,
        relationship_types: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        min_weight: float = MIN_WEIGHT,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if outgoing:
            stmt = select(KnowledgeRelationship).where(
                KnowledgeRelationship.source_entity_type == ref.entity_type,
                KnowledgeRelationship.source_entity_id == ref.entity_id,
            )
        else:
            stmt = select(KnowledgeRelationship).where(
                KnowledgeRelationship.target_entity_type == ref.entity_type,
                KnowledgeRelationship.target_entity_id == ref.entity_id,
            )

        if relationship_types:
            stmt = stmt.where(KnowledgeRelationship.relationship_type.in_(relationship_types))
        if entity_types:
            column = (
                KnowledgeRelationship.target_entity_type
                if outgoing
                else KnowledgeRelationship.source_entity_type
            )
            stmt = stmt.where(column.in_(entity_types))

        stmt = (
            stmt.where(KnowledgeRelationship.weight >= min_weight)
            .order_by(KnowledgeRelationship.weight.desc())
            .limit(limit + 1)
        )

        edges = list((await self.db.execute(stmt)).scalars().all())
        # Ask for one more than the caller wants. Getting a full page back means
        # there *may* be more, which is the only honest signal available here --
        # inferring "there are more" from a page that happens to be full reports
        # truncation for an exactly-full result and never for a partial one.
        saturated = len(edges) > limit
        edges = edges[:limit]

        if outgoing:
            neighbors = [
                {
                    "entity_type": e.target_entity_type,
                    "entity_id": e.target_entity_id,
                    "relationship_type": e.relationship_type,
                    "weight": e.weight,
                    "properties": e.properties or {},
                }
                for e in edges
            ]
        else:
            neighbors = [
                {
                    "entity_type": e.source_entity_type,
                    "entity_id": e.source_entity_id,
                    "relationship_type": e.relationship_type,
                    "weight": e.weight,
                    "properties": e.properties or {},
                }
                for e in edges
            ]
        return neighbors, saturated

    async def traverse(
        self,
        start: EntityRef,
        *,
        max_depth: int = 3,
        relationship_types: Optional[List[str]] = None,
        min_weight: float = MIN_WEIGHT,
        direction: str = "out",
        limit: int = MAX_TRAVERSAL_NODES,
    ) -> TraversalResult:
        """Breadth-first walk from `start`.

        Depth is bounded because the planner calls this on every task and an
        unbounded walk over a cyclic graph does not terminate. Reaching the
        budget sets `truncated` rather than raising: a partial answer is useful,
        and the caller is told it is partial.
        """
        if direction not in ("out", "in", "both"):
            raise InvalidRelationship("direction must be 'out', 'in', or 'both'.")
        if max_depth < 1:
            raise InvalidRelationship("max_depth must be at least 1.")
        if max_depth > MAX_TRAVERSAL_DEPTH:
            raise InvalidRelationship(
                f"max_depth must not exceed {MAX_TRAVERSAL_DEPTH}."
            )

        seen: Dict[str, TraversalNode] = {}
        frontier: List[EntityRef] = [start]
        depth_reached = 0
        truncated = False

        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            next_frontier: List[EntityRef] = []

            for ref in frontier:
                directions = ("out", "in") if direction == "both" else (direction,)
                for leg in directions:
                    found, saturated = await self._neighbors(
                        ref,
                        outgoing=(leg == "out"),
                        relationship_types=relationship_types,
                        min_weight=min_weight,
                        limit=limit,
                    )
                    for edge in found:
                        neighbour = EntityRef(edge["entity_type"], edge["entity_id"])
                        if neighbour.key == start.key or neighbour.key in seen:
                            continue
                        if len(seen) >= limit:
                            truncated = True
                            break
                        seen[neighbour.key] = TraversalNode(
                            ref=neighbour,
                            depth=depth,
                            relationship_type=edge["relationship_type"],
                            weight=edge["weight"],
                            via=ref,
                        )
                        next_frontier.append(neighbour)
                    # A saturated page means this hop had more edges than were
                    # read, so the walk is incomplete even if `limit` was not hit.
                    if saturated and not truncated:
                        truncated = True
                    if truncated:
                        break
                if truncated:
                    break

            depth_reached = depth
            frontier = next_frontier
            if truncated:
                break

        nodes = sorted(seen.values(), key=lambda n: (n.depth, -n.weight))
        return TraversalResult(nodes=nodes, truncated=truncated, depth_reached=depth_reached)

    async def find_blockers(self, ref: EntityRef) -> List[Dict[str, Any]]:
        """Incoming blocking relationships: what is stopping this entity."""
        blocking = await self.incoming(
            ref, relationship_types=sorted(BLOCKING_TYPES)
        )
        return [
            {
                **edge,
                "blocks": ref.key,
                "direction": edge["relationship_type"],
            }
            for edge in blocking
        ]

    async def related_memories(
        self,
        ref: EntityRef,
        *,
        limit: int = 20,
        include_expired: bool = False,
    ) -> List[MemoryRecord]:
        """Memory records attached to an entity, newest-confidence first."""
        edges = await self.neighbors(
            ref, entity_types=["MEMORY"], limit=limit * 2
        )
        if not edges:
            return []

        memory_ids = [e["entity_id"] for e in edges if _looks_like_uuid(e["entity_id"])]
        if not memory_ids:
            return []

        stmt = select(MemoryRecord).where(
            MemoryRecord.id.in_([uuid.UUID(m) for m in memory_ids])
        )
        if not include_expired:
            now = datetime.now(timezone.utc)
            stmt = stmt.where(
                (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > now)
            )
        stmt = stmt.order_by(MemoryRecord.confidence.desc()).limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def attach_memory(
        self,
        ref: EntityRef,
        memory: MemoryRecord,
        *,
        relationship_type: str = "DERIVED_FROM",
        weight: float = 0.9,
    ) -> KnowledgeRelationship:
        """Link an entity to a stored memory record."""
        return await self.link(
            ref,
            relationship_type,
            EntityRef("MEMORY", str(memory.id)),
            weight=weight,
        )

    async def stats(self) -> Dict[str, Any]:
        """Graph size, broken down for the dashboard."""
        total = await self.db.scalar(select(func.count()).select_from(KnowledgeRelationship))
        by_type = dict(
            (
                await self.db.execute(
                    select(
                        KnowledgeRelationship.relationship_type,
                        func.count(),
                    ).group_by(KnowledgeRelationship.relationship_type)
                )
            ).all()
        )
        by_entity = dict(
            (
                await self.db.execute(
                    select(
                        KnowledgeRelationship.source_entity_type,
                        func.count(),
                    ).group_by(KnowledgeRelationship.source_entity_type)
                )
            ).all()
        )
        mean_weight = await self.db.scalar(
            select(func.avg(KnowledgeRelationship.weight))
        )
        return {
            "total_edges": int(total or 0),
            "by_relationship_type": by_type,
            "by_source_entity_type": by_entity,
            "mean_weight": round(float(mean_weight), 4) if mean_weight is not None else None,
        }


def _looks_like_uuid(value: str) -> bool:
    """Entity ids are opaque strings; only real UUIDs can be memory-record ids."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False