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

from backend.app.api.v1.router import api_router
from backend.app.core.config import settings
from backend.app.core.logging_setup import configure_logging
from backend.app.live.relay import relay_loop
from backend.app.security.emergency_stop import emergency_stop
from backend.app.terminal.events import publish_success
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.registry import registry  # noqa: F401  (re-exported for callers)
from backend.app.transactions.approvals import bind_verifier

configure_logging()
logger = logging.getLogger("airdropos.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AirdropOS backend starting (environment=%s)", settings.ENVIRONMENT)
    logger.info("Fixed model: %s @ %s", settings.LLM_MODEL_ID, settings.LLM_BASE_URL)
    # Sizes are logged, never content. An operator reading this knows whether a
    # compaction event later was expected or a sign the window is set too low.
    from backend.app.llm.token_budget import TokenBudget

    _budget = TokenBudget.from_settings()
    logger.info(
        "Input context budget: window=%s usable=%s compact-above=%s compact-to=%s "
        "(output reserved=%s, safety margin=%s)",
        _budget.context_window,
        _budget.usable_input,
        _budget.trigger,
        _budget.target,
        _budget.reserved_output,
        _budget.safety_margin,
    )
    # Count only. `bootstrap_tools` logs one summary line; the individual tool
    # names are DEBUG and are also served by GET /tools.
    bootstrap_tools()
    # Apply a changed OPERATOR_PASSWORD_HASH (from --set-password) to the existing account.
    from backend.app.security.operator_sync import sync_operator_password

    await sync_operator_password()
    # Server-side approvals are what let a verified transfer be broadcast at all.
    # Without this the engine correctly refuses every broadcast, so an approved
    # transfer could never complete.
    bind_verifier()
    if emergency_stop.is_stopped:
        logger.warning("EMERGENCY STOP ACTIVE at startup: %s", emergency_stop.status()["reason"])
    relay_stop = asyncio.Event()
    relay_task = asyncio.create_task(relay_loop(relay_stop)) if settings.LIVE_GATEWAY_ENABLED else None
    # "Started" is only true once this process answers on its own port, and the
    # lifespan body finishes before uvicorn binds the socket. Readiness is
    # therefore confirmed, not assumed.
    ready_task = asyncio.create_task(_announce_ready_when_serving())
    yield
    ready_task.cancel()
    relay_stop.set()
    if relay_task is not None:
        relay_task.cancel()
    logger.info("AirdropOS backend shutting down")


# How long startup confirmation waits for the socket to answer before giving up.
READY_PROBE_ATTEMPTS = 40
READY_PROBE_INTERVAL_SECONDS = 0.5


async def _announce_ready_when_serving() -> None:
    """Publish a confirmed start once the API answers its own health endpoint.

    The probe is the unauthenticated liveness route a load balancer would use, so
    this proves the API is reachable rather than merely that the process exists.
    Bounded: it gives up and logs instead of polling forever, and it can never
    delay startup - it is a background task.
    """
    import httpx

    prefix = settings.API_V1_PREFIX
    url = f"http://127.0.0.1:{settings.BACKEND_PORT}{prefix}/health"
    for _ in range(READY_PROBE_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            if response.status_code == 200:
                host = settings.BACKEND_HOST
                if host in ("127.0.0.1", "0.0.0.0", "::"):
                    host = "localhost"
                publish_success(
                    "airdropos.api",
                    f"Backend API started (http://{host}:{settings.BACKEND_PORT})",
                )
                return
        except Exception:
            pass
        await asyncio.sleep(READY_PROBE_INTERVAL_SECONDS)
    logger.warning("Backend API did not answer its own health probe on %s.", url)


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


_DEFAULT_CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'self'"

# FastAPI's built-in Swagger UI and ReDoc pages load their scripts and styles from
# cdn.jsdelivr.net and start with an inline script. Under the strict default policy
# the browser blocks all of that and the page renders blank. Only these two
# documentation pages get the relaxed policy; every other response keeps the strict one.
_DOCS_PATHS = frozenset({"/docs", "/redoc"})
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
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
        _DOCS_CSP if request.url.path in _DOCS_PATHS else _DEFAULT_CSP
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
