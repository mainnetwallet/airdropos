"""Knowledge Graph API (v1).

Exposes the entity graph for exploration and for the planner's context assembly.
Reads require `read`; writing edges requires `system_admin`, because a link
changes what the planner will believe about the system and should not be
something any authenticated session can introduce.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.knowledge.graph import (
    ENTITY_TYPES,
    InvalidRelationship,
    KnowledgeGraphService,
)
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_system_admin,
)

router = APIRouter()


class EntityRefPayload(BaseModel):
    entity_type: str = Field(description=f"One of: {', '.join(sorted(ENTITY_TYPES))}")
    entity_id: str = Field(min_length=1, max_length=64)


class LinkRequest(BaseModel):
    source: EntityRefPayload
    target: EntityRefPayload
    relationship_type: str = Field(min_length=1, max_length=100)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    properties: Optional[Dict[str, Any]] = None


class TraversalRequest(BaseModel):
    start: EntityRefPayload
    max_depth: int = Field(default=3, ge=1, le=5)
    direction: str = Field(default="out")
    relationship_types: Optional[List[str]] = None


def _ref(payload: EntityRefPayload):
    from backend.app.knowledge.graph import EntityRef

    try:
        return EntityRef(payload.entity_type, payload.entity_id)
    except InvalidRelationship as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/neighbors", tags=["knowledge"])
async def get_neighbors(
    request: Request,
    entity_type: str = Query(...),
    entity_id: str = Query(...),
    direction: str = Query(default="out", pattern="^(out|in)$"),
    relationship_types: Optional[List[str]] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Direct neighbors of an entity, strongest edge first."""
    enforce_rate_limit(request, "read")
    service = KnowledgeGraphService(db)
    ref = _ref(EntityRefPayload(entity_type=entity_type, entity_id=entity_id))

    if direction == "in":
        edges = await service.incoming(ref, relationship_types=relationship_types)
    else:
        edges = await service.neighbors(ref, relationship_types=relationship_types)
    return {"entity": {"type": ref.entity_type, "id": ref.entity_id}, "edges": edges}


@router.post("/knowledge/traverse", tags=["knowledge"])
async def traverse_graph(
    request: Request,
    payload: TraversalRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Breadth-first walk from an entity, bounded by depth and node count."""
    enforce_rate_limit(request, "read")
    service = KnowledgeGraphService(db)
    ref = _ref(payload.start)
    try:
        result = await service.traverse(
            ref,
            max_depth=payload.max_depth,
            direction=payload.direction,
            relationship_types=payload.relationship_types,
        )
    except InvalidRelationship as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"start": ref.key, **result.to_dict()}


@router.post("/knowledge/link", status_code=201, tags=["knowledge"])
async def link_entities(
    request: Request,
    payload: LinkRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_system_admin),
) -> Dict[str, Any]:
    """Create or strengthen a relationship between two entities."""
    enforce_rate_limit(request, "write")
    service = KnowledgeGraphService(db)
    try:
        edge = await service.link(
            _ref(payload.source),
            payload.relationship_type,
            _ref(payload.target),
            weight=payload.weight,
            properties=payload.properties,
        )
    except InvalidRelationship as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await db.commit()
    return {
        "source": f"{edge.source_entity_type}:{edge.source_entity_id}",
        "target": f"{edge.target_entity_type}:{edge.target_entity_id}",
        "relationship_type": edge.relationship_type,
        "weight": edge.weight,
    }


@router.get("/knowledge/blockers", tags=["knowledge"])
async def get_blockers(
    request: Request,
    entity_type: str = Query(...),
    entity_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Incoming blocking relationships for an entity."""
    enforce_rate_limit(request, "read")
    service = KnowledgeGraphService(db)
    ref = _ref(EntityRefPayload(entity_type=entity_type, entity_id=entity_id))
    return {"entity": ref.key, "blockers": await service.find_blockers(ref)}


@router.get("/knowledge/stats", tags=["knowledge"])
async def knowledge_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Graph size and composition."""
    enforce_rate_limit(request, "read")
    return await KnowledgeGraphService(db).stats()