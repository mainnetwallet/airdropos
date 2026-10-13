"""AirdropOS FastAPI Application Entry Point.

Run with: uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.v1.router import api_router
from backend.app.core.config import settings
from backend.app.core.logging_setup import configure_logging
from backend.app.live.relay import relay_loop
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.registry import registry  # noqa: F401  (re-exported for callers)
from backend.app.transactions.approvals import bind_verifier

configure_logging()
logger = logging.getLogger("airdropos.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AirdropOS backend starting (environment=%s)", settings.ENVIRONMENT)
    logger.info("Fixed model: %s @ %s", settings.LLM_MODEL_ID, settings.LLM_BASE_URL)
    # Count only. `bootstrap_tools` logs one summary line; the individual tool
    # names are DEBUG and are also served by GET /tools.
    bootstrap_tools()
    # Server-side approvals are what let a verified transfer be broadcast at all.
    # Without this the engine correctly refuses every broadcast, so an approved
    # transfer could never complete.
    bind_verifier()
    if emergency_stop.is_stopped:
        logger.warning("EMERGENCY STOP ACTIVE at startup: %s", emergency_stop.status()["reason"])
    relay_stop = asyncio.Event()
    relay_task = asyncio.create_task(relay_loop(relay_stop)) if settings.LIVE_GATEWAY_ENABLED else None
    yield
    relay_stop.set()
    if relay_task is not None:
        relay_task.cancel()
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
    expose_headers=["X-Process-Time", "X-Frame-Id", "X-Frame-Width", "X-Frame-Height", "X-Captured-At"],
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
