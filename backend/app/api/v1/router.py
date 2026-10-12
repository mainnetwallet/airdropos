"""API v1 Router Aggregation."""

from fastapi import APIRouter

from backend.app.api.v1.endpoints import (
    auth,
    browser_profiles,
    campaigns,
    live,
    knowledge,
    system,
    transactions,
    audit,
    settings,
    wallets,
    ws,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(campaigns.router)
api_router.include_router(system.router)
api_router.include_router(transactions.router)
api_router.include_router(audit.router)
api_router.include_router(settings.router)
api_router.include_router(wallets.router)
api_router.include_router(knowledge.router)
api_router.include_router(ws.router)
api_router.include_router(live.router)
api_router.include_router(browser_profiles.router)
