"""API v1 Router Aggregation."""

from fastapi import APIRouter

from backend.app.api.v1.endpoints import (
    agent_conversations,
    auth,
    browser_profiles,
    campaigns,
    discord,
    inbox,
    live,
    knowledge,
    scheduled_tasks,
    system,
    transactions,
    audit,
    settings,
    wallets,
    ws,
    x_integration,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(campaigns.router)
api_router.include_router(system.router)
api_router.include_router(agent_conversations.router)
api_router.include_router(inbox.router)
api_router.include_router(transactions.router)
api_router.include_router(audit.router)
api_router.include_router(settings.router)
api_router.include_router(wallets.router)
api_router.include_router(knowledge.router)
api_router.include_router(ws.router)
api_router.include_router(live.router)
api_router.include_router(browser_profiles.router)
api_router.include_router(x_integration.router)
api_router.include_router(discord.router)
api_router.include_router(scheduled_tasks.router)
