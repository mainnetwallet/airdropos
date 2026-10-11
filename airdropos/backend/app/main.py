"""AirdropOS FastAPI Application Entry Point.

Run with: uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.v1.router import api_router
from backend.app.blockchain.chains import known_networks
from backend.app.core.config import settings
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tools.blockchain_tools import register_blockchain_tools
from backend.app.tools.browser_tools import register_browser_tools
from backend.app.tools.notification_tools import register_notification_tools
from backend.app.tools.registry import AuthorizationPolicy, registry
from backend.app.tools.web_tools import register_web_tools

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("airdropos.api")


def bootstrap_tools() -> None:
    """Register every tool the agent may invoke, then apply authorization.

    Permissions are granted by the operator at startup. The agent cannot grant
    itself access to anything not listed here.
    """
    register_web_tools(registry)
    register_blockchain_tools(registry)
    register_browser_tools(registry)
    register_notification_tools(registry)

    registry.policy = AuthorizationPolicy(
        enabled_permissions=[
            "web.read",
            "blockchain.read",
            "browser.read",
            "browser.navigate",
            "browser.interact",
            "notify.send",
            "report.generate",
            "social.read",
        ],
        allowed_networks=known_networks(),
        allow_web_interaction=True,
        # Value-moving permissions are intentionally NOT granted by default.
        # Enabling them is an explicit operator decision.
        allow_transaction_submission=False,
        allow_wallet_signing=False,
    )
    logger.info("Registered %s tools with restricted authorization policy", len(registry._tools))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 70)
    logger.info("AirdropOS backend starting (environment=%s)", settings.ENVIRONMENT)
    logger.info("Fixed model: %s @ %s", settings.LLM_MODEL_ID, settings.LLM_BASE_URL)
    bootstrap_tools()
    if emergency_stop.is_stopped:
        logger.warning("EMERGENCY STOP ACTIVE at startup: %s", emergency_stop.status()["reason"])
    logger.info("=" * 70)
    yield
    logger.info("AirdropOS backend shutting down")


app = FastAPI(
    title="AirdropOS",
    description=(
        "Autonomous AI agent platform for legitimate cryptocurrency airdrop research, "
        "execution, monitoring, and tracking. One fixed LLM. Deterministic authorization."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# --------------------------------------------------------------------------
# Security middleware
# --------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach security headers and measure latency."""
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    response.headers["X-Process-Time"] = f"{(time.perf_counter() - started) * 1000:.2f}ms"
    return response


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["system"])
async def root() -> Dict[str, Any]:
    """Public service banner. Intentionally unauthenticated and deliberately terse.

    It does not advertise the fixed model, the WebSocket path, or emergency-stop
    state: those are operational details available to authenticated callers via
    `/api/v1/agent/info` and `/api/v1/emergency-stop`.
    """
    return {
        "name": "AirdropOS",
        "version": "1.0.0",
        "status": "operational",
        "api": settings.API_V1_PREFIX,
    }
