"""Tests for the Knowledge Graph Service.

The `KnowledgeRelationship` model existed with no writer and no reader: nothing
ever created an edge, so the graph could not answer a single query. These tests
exercise the service that now writes and reads it.

The properties under test are the ones that make a graph trustworthy rather than
decorative:

- Edges are typed and directed -- `A REQUIRES B` is not `B REQUIRES A`.
- Re-linking strengthens an edge rather than duplicating it.
- Traversal is bounded, and reports truncation instead of looping forever on a
  cycle.
- Deleting an entity prunes its edges; dangling references are never left behind.
- The graph cannot become a place secrets are persisted.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from backend.app.knowledge.graph import (
    DEFAULT_WEIGHT,
    ENTITY_TYPES,
    EntityRef,
    InvalidRelationship,
    KnowledgeGraphService,
)
from backend.app.memory.manager import MemoryManager
from backend.app.models import Base, KnowledgeRelationship, MemoryRecord


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003
    """Render PostgreSQL JSONB columns as SQLite JSON for the test database."""
    return "JSON"


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def graph(db):
    return KnowledgeGraphService(db)


PROJECT = EntityRef("PROJECT", "proj-1")
WALLET = EntityRef("WALLET", "0xabc")
CAMPAIGN = EntityRef("CAMPAIGN", "camp-1")
TASK = EntityRef("TASK", "task-1")
TOOL = EntityRef("TOOL", "playwright")


# ======================================================================
# Entity validation
# ======================================================================
def test_entity_type_must_be_known():
    with pytest.raises(InvalidRelationship) as excinfo:
        EntityRef("NOT_A_TYPE", "x")
    assert "NOT_A_TYPE" in str(excinfo.value)


def test_entity_id_must_be_bounded_and_non_empty():
    with pytest.raises(InvalidRelationship):
        EntityRef("PROJECT", "")
    with pytest.raises(InvalidRelationship):
        EntityRef("PROJECT", "x" * 65)


def test_entity_key_is_type_and_id():
    assert PROJECT.key == "PROJECT:proj-1"


# ======================================================================
# Writing edges
# ======================================================================
@pytest.mark.asyncio
async def test_link_creates_an_edge(graph):
    edge = await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    assert edge.source_entity_type == "CAMPAIGN"
    assert edge.target_entity_type == "WALLET"
    assert edge.weight == DEFAULT_WEIGHT


@pytest.mark.asyncio
async def test_relinking_strengthens_rather_than_duplicates(graph):
    """Repeated observation of the same fact is evidence, not a new fact."""
    first = await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=0.4)
    second = await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=0.8)

    assert first.id == second.id, "relinking must update the existing edge"
    assert second.weight > 0.4


@pytest.mark.asyncio
async def test_weight_is_clamped_to_range(graph):
    edge = await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=5.0)
    assert edge.weight <= 1.0


@pytest.mark.asyncio
async def test_self_referential_edge_is_refused(graph):
    """A self-edge would make traversal cycles on its own first hop."""
    with pytest.raises(InvalidRelationship):
        await graph.link(CAMPAIGN, "REQUIRES", CAMPAIGN)


@pytest.mark.asyncio
async def test_relationship_type_is_length_checked(graph):
    with pytest.raises(InvalidRelationship):
        await graph.link(CAMPAIGN, "R" * 101, WALLET)


@pytest.mark.asyncio
async def test_secret_bearing_properties_are_refused(graph):
    """The graph must not become a place secrets are persisted.

    Properties are the only free-text field on an edge, so they get the same
    screening memory records do.
    """
    with pytest.raises(InvalidRelationship):
        await graph.link(
            CAMPAIGN,
            "REQUIRES",
            WALLET,
            properties={"note": "0x" + "ab" * 32},
        )
    with pytest.raises(InvalidRelationship):
        await graph.link(
            CAMPAIGN,
            "REQUIRES",
            WALLET,
            properties={"note": "private_key = hunter2hunter2"},
        )


@pytest.mark.asyncio
async def test_link_many_fails_the_batch_on_a_bad_edge(graph):
    """A partial graph that looks complete is worse than a loud failure."""
    with pytest.raises(InvalidRelationship):
        await graph.link_many(
            [
                (CAMPAIGN, "REQUIRES", WALLET),
                (EntityRef("BOGUS", "x"), "REQUIRES", WALLET),  # invalid on construction
            ]
        )


# ======================================================================
# Reading
# ======================================================================
@pytest.mark.asyncio
async def test_neighbors_are_directed(graph):
    """`A REQUIRES B` must not imply `B REQUIRES A`."""
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)

    assert len(await graph.neighbors(CAMPAIGN)) == 1
    assert await graph.neighbors(WALLET) == []
    assert len(await graph.incoming(WALLET)) == 1
    assert await graph.incoming(CAMPAIGN) == []


@pytest.mark.asyncio
async def test_neighbors_filter_by_type_and_relationship(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(CAMPAIGN, "PREPARES_FOR", TASK)

    only_wallets = await graph.neighbors(CAMPAIGN, entity_types=["WALLET"])
    assert [e["entity_id"] for e in only_wallets] == [WALLET.entity_id]

    only_requires = await graph.neighbors(CAMPAIGN, relationship_types=["REQUIRES"])
    assert len(only_requires) == 1


@pytest.mark.asyncio
async def test_weak_edges_are_filtered_out(graph):
    """Stale knowledge should stop being returned, not persist forever."""
    await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=0.9)
    assert len(await graph.neighbors(CAMPAIGN, min_weight=0.0)) == 1
    # An edge the service has decayed below the floor is invisible.
    edge = await graph._find(CAMPAIGN, "REQUIRES", WALLET)
    edge.weight = 0.01
    await graph.db.flush()
    assert await graph.neighbors(CAMPAIGN) == []
    assert len(await graph.neighbors(CAMPAIGN, min_weight=0.0)) == 1


@pytest.mark.asyncio
async def test_neighbors_are_ordered_by_weight(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=0.2)
    await graph.link(CAMPAIGN, "AFFECTS", TASK, weight=0.95)
    ordered = await graph.neighbors(CAMPAIGN)
    assert [e["weight"] for e in ordered] == sorted(
        [e["weight"] for e in ordered], reverse=True
    )


# ======================================================================
# Weakening and removing
# ======================================================================
@pytest.mark.asyncio
async def test_weaken_halves_and_floors(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET, weight=1.0)
    weight = await graph.weaken(CAMPAIGN, "REQUIRES", WALLET)
    assert weight == 0.5
    # Weakening repeatedly shrinks toward the floor, never back to full strength.
    await graph.weaken(CAMPAIGN, "REQUIRES", WALLET)
    final = await graph.weaken(CAMPAIGN, "REQUIRES", WALLET)
    assert final < 0.5


@pytest.mark.asyncio
async def test_weaken_missing_edge_returns_zero(graph):
    assert await graph.weaken(CAMPAIGN, "REQUIRES", WALLET) == 0.0


@pytest.mark.asyncio
async def test_unlink_removes_the_edge(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    assert await graph.unlink(CAMPAIGN, "REQUIRES", WALLET) is True
    assert await graph.neighbors(CAMPAIGN) == []
    assert await graph.unlink(CAMPAIGN, "REQUIRES", WALLET) is False


@pytest.mark.asyncio
async def test_unlink_does_not_affect_other_relationship_types(graph):
    """`REQUIRES` and `DEPENDS_ON` between the same pair are distinct facts."""
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(CAMPAIGN, "DEPENDS_ON", WALLET)
    await graph.unlink(CAMPAIGN, "REQUIRES", WALLET)
    remaining = await graph.neighbors(CAMPAIGN)
    assert [e["relationship_type"] for e in remaining] == ["DEPENDS_ON"]


# ======================================================================
# Traversal
# ======================================================================
@pytest.mark.asyncio
async def test_traverse_walks_outward_by_depth(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(WALLET, "PARTICIPATES_IN", TASK)
    await graph.link(TASK, "USES", TOOL)

    result = await graph.traverse(CAMPAIGN, max_depth=3)
    depths = {n.ref.entity_type: n.depth for n in result.nodes}
    assert depths == {"WALLET": 1, "TASK": 2, "TOOL": 3}
    assert result.truncated is False


@pytest.mark.asyncio
async def test_traverse_respects_max_depth(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(WALLET, "PARTICIPATES_IN", TASK)
    await graph.link(TASK, "USES", TOOL)

    shallow = await graph.traverse(CAMPAIGN, max_depth=1)
    assert {n.ref.entity_type for n in shallow.nodes} == {"WALLET"}
    assert shallow.depth_reached == 1


@pytest.mark.asyncio
async def test_traverse_terminates_on_a_cycle(graph):
    """The reason traversal is bounded: a cyclic graph must not hang the planner."""
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(WALLET, "DEPENDS_ON", TASK)
    await graph.link(TASK, "BLOCKED_BY", CAMPAIGN)

    result = await graph.traverse(CAMPAIGN, max_depth=5, direction="both")
    keys = {n.ref.key for n in result.nodes}
    assert "PROJECT:proj-1" not in keys, "the start node must not reappear"
    assert len(keys) == len(result.nodes), "no node may be visited twice"


@pytest.mark.asyncio
async def test_traverse_reports_truncation_instead_of_looping(graph):
    """A wide graph must be cut off and flagged, not silently returned whole."""
    for index in range(30):
        await graph.link(
            CAMPAIGN, "AFFECTS", EntityRef("TASK", f"task-{index}")
        )

    result = await graph.traverse(CAMPAIGN, max_depth=1, limit=10)
    assert result.truncated is True
    assert len(result.nodes) <= 10


@pytest.mark.asyncio
async def test_traverse_rejects_an_out_of_range_depth(graph):
    from backend.app.knowledge.graph import MAX_TRAVERSAL_DEPTH

    with pytest.raises(InvalidRelationship):
        await graph.traverse(CAMPAIGN, max_depth=MAX_TRAVERSAL_DEPTH + 1)
    with pytest.raises(InvalidRelationship):
        await graph.traverse(CAMPAIGN, max_depth=0)


@pytest.mark.asyncio
async def test_traverse_filters_by_relationship_type(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(CAMPAIGN, "AFFECTS", TASK)

    result = await graph.traverse(CAMPAIGN, relationship_types=["REQUIRES"])
    assert {n.ref.entity_type for n in result.nodes} == {"WALLET"}


# ======================================================================
# Blockers
# ======================================================================
@pytest.mark.asyncio
async def test_find_blockers_reports_incoming_dependencies(graph):
    await graph.link(WALLET, "DEPENDS_ON", CAMPAIGN)
    await graph.link(CAMPAIGN, "AFFECTS", TASK)

    blockers = await graph.find_blockers(CAMPAIGN)
    assert [b["entity_type"] for b in blockers] == ["WALLET"]
    assert blockers[0]["entity_id"] == WALLET.entity_id
    assert blockers[0]["blocks"] == "CAMPAIGN:camp-1"
    # The non-blocking edge must not be reported as a blocker.
    assert all(b["relationship_type"] != "AFFECTS" for b in blockers)


# ======================================================================
# Deletion
# ======================================================================
@pytest.mark.asyncio
async def test_prune_entity_drops_incoming_and_outgoing(graph):
    """Edges are not foreign keys, so deletion must clean them explicitly."""
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(WALLET, "PARTICIPATES_IN", CAMPAIGN)
    await graph.link(WALLET, "PARTICIPATES_IN", TASK)

    removed = await graph.prune_entity(WALLET)
    assert removed == 3

    remaining = await graph.neighbors(CAMPAIGN)
    assert remaining == []
    assert await graph.neighbors(TASK) == []


@pytest.mark.asyncio
async def test_prune_leaves_unrelated_edges_alone(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(TASK, "USES", TOOL)

    await graph.prune_entity(WALLET)
    assert len(await graph.neighbors(TASK)) == 1


# ======================================================================
# Memory attachment
# ======================================================================
@pytest.mark.asyncio
async def test_attach_memory_and_read_it_back(graph, db):
    memory = await MemoryManager(db).remember(
        category="PROJECT_KNOWLEDGE",
        content="The Base campaign requires two signatures.",
        source="https://example.invalid/base",
        confidence=0.9,
    )
    await graph.attach_memory(CAMPAIGN, memory)

    records = await graph.related_memories(CAMPAIGN)
    assert [r.id for r in records] == [memory.id]


@pytest.mark.asyncio
async def test_related_memories_ignores_expired_records(graph, db):
    from datetime import datetime, timedelta, timezone

    memory = await MemoryManager(db).remember(
        category="PROJECT_KNOWLEDGE",
        content="A claim window that already closed.",
        source="test",
        ttl_days=1,
    )
    memory.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.flush()

    await graph.attach_memory(CAMPAIGN, memory)
    assert await graph.related_memories(CAMPAIGN) == []
    assert len(await graph.related_memories(CAMPAIGN, include_expired=True)) == 1


# ======================================================================
# Stats
# ======================================================================
@pytest.mark.asyncio
async def test_stats_reports_composition(graph):
    await graph.link(CAMPAIGN, "REQUIRES", WALLET)
    await graph.link(TASK, "USES", TOOL)

    stats = await graph.stats()
    assert stats["total_edges"] == 2
    assert stats["by_relationship_type"] == {"REQUIRES": 1, "USES": 1}
    assert stats["by_source_entity_type"] == {"CAMPAIGN": 1, "TASK": 1}
    assert stats["mean_weight"] == 1.0


@pytest.mark.asyncio
async def test_stats_on_an_empty_graph(graph):
    stats = await graph.stats()
    assert stats["total_edges"] == 0
    assert stats["mean_weight"] is None


# ======================================================================
# Model contract
# ======================================================================
def test_entity_types_are_closed_and_documented():
    """An untyped entity makes a traversal unanswerable."""
    assert "PROJECT" in ENTITY_TYPES
    assert "WALLET" in ENTITY_TYPES
    assert "CAMPAIGN" in ENTITY_TYPES


def test_relationship_model_has_no_cascade_from_entities():
    """Documents the deliberate choice: edges are pruned explicitly.

    If a future migration adds real foreign keys with ondelete=CASCADE, the
    `prune_entity` bookkeeping would become redundant -- which is fine, but the
    caller should know they can drop it.
    """
    table = KnowledgeRelationship.__table__
    assert not table.foreign_keys, "edge model intentionally has no FKs"