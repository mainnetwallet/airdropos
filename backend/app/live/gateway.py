"""Internal gateway served by the WORKER.

The browsers and the Telegram client live in the worker, so only the worker can
show or control them. This tiny HTTP app exposes them to the API process and to
nobody else:

  * binds to loopback by default (compose overrides this to the private network),
  * every request must carry the shared secret (constant-time compare),
  * it never trusts the caller's identity claims beyond what the API asserts
    after doing the real user authentication and scope checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response

from backend.app.core.config import settings
from backend.app.live import hub
from backend.app.live.models import Conflict, NotSupported, SessionNotFound
from backend.app.live.providers import PROVIDERS
from backend.app.live.security import GATEWAY_HEADER, bind_allowed, secret_matches

logger = logging.getLogger("airdropos.live.gateway")


async def _require_secret(x_live_gateway_key: Optional[str] = Header(default=None, alias=GATEWAY_HEADER)) -> None:
    if not secret_matches(x_live_gateway_key):
        raise HTTPException(status_code=401, detail="bad gateway key")


async def _json_body(request: Request) -> Dict[str, Any]:
    """The request body as a JSON object; anything else is a clean 400 instead of a 500."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_json", "message": "Body must be a JSON object."})
    return body


def _provider(integration: str):
    provider = PROVIDERS.get(integration)
    if provider is None:
        raise HTTPException(status_code=404, detail="unknown integration")
    return provider


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionNotFound):
        return HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found or no longer running."})
    if isinstance(exc, NotSupported):
        return HTTPException(status_code=422, detail={"code": "not_supported", "message": str(exc)})
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})
    raise exc


def resources() -> Dict[str, Any]:
    """Host usage of the worker (where the browsers run). None means 'cannot measure here'."""
    out: Dict[str, Any] = {"cpu_percent": None, "ram_percent": None, "disk_percent": None, "network_kbps": None}
    try:
        import psutil  # optional

        out["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        out["ram_percent"] = round(psutil.virtual_memory().percent, 1)
        io = psutil.net_io_counters()
        out["_net_total"] = io.bytes_sent + io.bytes_recv
    except Exception:
        try:
            load = os.getloadavg()[0]
            out["cpu_percent"] = round(min(100.0, load / (os.cpu_count() or 1) * 100), 1)
            out["cpu_source"] = "loadavg"
        except Exception:
            pass
    try:
        usage = shutil.disk_usage(settings.BROWSER_USER_DATA_DIR if os.path.isdir(settings.BROWSER_USER_DATA_DIR) else ".")
        out["disk_percent"] = round(usage.used / usage.total * 100, 1)
    except Exception:
        pass
    return out


_last_net: Dict[str, float] = {}


def _with_network(res: Dict[str, Any]) -> Dict[str, Any]:
    import time

    total = res.pop("_net_total", None)
    if total is not None:
        now = time.monotonic()
        if "t" in _last_net and now > _last_net["t"]:
            res["network_kbps"] = round((total - _last_net["n"]) / 1024 / (now - _last_net["t"]), 1)
        _last_net.update(t=now, n=total)
    return res


def build_app() -> FastAPI:
    app = FastAPI(title="AirdropOS live gateway", docs_url=None, redoc_url=None, openapi_url=None)
    deps = [Depends(_require_secret)]

    @app.get("/health", dependencies=deps)
    async def health() -> Dict[str, Any]:
        return {"ok": True, "epoch": hub.events.epoch}

    @app.get("/integrations", dependencies=deps)
    async def integrations() -> Dict[str, Any]:
        return {"integrations": [p.descriptor().as_dict() for p in PROVIDERS.values()]}

    @app.get("/sessions", dependencies=deps)
    async def sessions() -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for provider in PROVIDERS.values():
            try:
                out.extend(await provider.list_sessions())
            except Exception:
                logger.exception("Provider %s failed to list sessions", provider.integration)
        return {"sessions": out, "epoch": hub.events.epoch, "resources": _with_network(resources())}

    @app.post("/sessions", dependencies=deps)
    async def create_session(request: Request) -> Dict[str, Any]:
        body = await _json_body(request)
        provider = _provider(str(body.get("integration") or ""))
        try:
            return {"session": await provider.create(body)}
        except Exception as exc:
            raise _guard(exc)

    @app.get("/sessions/{integration}/{key}", dependencies=deps)
    async def session(integration: str, key: str) -> Dict[str, Any]:
        try:
            return {"session": await _provider(integration).get_session(key)}
        except HTTPException:
            raise
        except Exception as exc:
            raise _guard(exc)

    @app.get("/sessions/{integration}/{key}/frame", dependencies=deps)
    async def frame(integration: str, key: str, quality: str = Query(default="thumb")) -> Response:
        try:
            f = await _provider(integration).frame(key, quality)
        except HTTPException:
            raise
        except Exception as exc:
            raise _guard(exc)
        return Response(
            content=f.data,
            media_type=f.mime,
            headers={
                "X-Frame-Id": f.frame_id,
                "X-Frame-Width": str(f.width),
                "X-Frame-Height": str(f.height),
                "X-Captured-At": f"{f.captured_at:.3f}",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/sessions/{integration}/{key}/action", dependencies=deps)
    async def action(integration: str, key: str, request: Request) -> Dict[str, Any]:
        body = await _json_body(request)
        try:
            return await _provider(integration).action(key, str(body.get("action") or ""), body)
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
        except Exception as exc:
            raise _guard(exc)

    @app.post("/sessions/{integration}/{key}/input", dependencies=deps)
    async def send_input(integration: str, key: str, request: Request) -> Dict[str, Any]:
        body = await _json_body(request)
        try:
            return await _provider(integration).input(key, body)
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
        except Exception as exc:
            raise _guard(exc)

    @app.get("/events", dependencies=deps)
    async def events(
        after: Optional[int] = None,
        epoch: Optional[str] = None,
        session: Optional[str] = None,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        log = hub.events
        if after is None:  # caller only wants the current cursor / recent tail
            return {"epoch": log.epoch, "last_seq": log.last_seq, "events": log.recent(limit=limit, session_id=session)}
        if epoch != log.epoch:  # the worker restarted since the caller's cursor
            after = 0
        return {"epoch": log.epoch, "last_seq": log.last_seq, "events": log.since(after, limit=limit, session_id=session)}

    return app


async def serve(shutdown_event: asyncio.Event) -> None:
    """Run the gateway for the lifetime of the worker. Never raises into the worker."""
    if not settings.LIVE_GATEWAY_ENABLED:
        logger.info("Live gateway disabled (LIVE_GATEWAY_ENABLED=false)")
        return
    refusal = bind_allowed()
    if refusal:
        logger.error("Live gateway NOT started: %s", refusal)
        return
    try:
        import socket

        import uvicorn

        # Bind ourselves: if the port is taken, uvicorn would call sys.exit() and take the
        # whole worker down with it. A busy port must only disable the live view.
        sock = socket.socket(socket.AF_INET6 if ":" in settings.LIVE_GATEWAY_HOST else socket.AF_INET)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((settings.LIVE_GATEWAY_HOST, settings.LIVE_GATEWAY_PORT))
            sock.listen(64)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            logger.error(
                "Live gateway NOT started: cannot bind %s:%s (%s). Another worker may already be using it.",
                settings.LIVE_GATEWAY_HOST, settings.LIVE_GATEWAY_PORT, exc,
            )
            return

        config = uvicorn.Config(build_app(), log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # the worker owns signals
        task = asyncio.create_task(server.serve(sockets=[sock]))
        sweeper = asyncio.create_task(_sweep_loop(shutdown_event))
        logger.info("Live gateway listening on %s:%s", settings.LIVE_GATEWAY_HOST, settings.LIVE_GATEWAY_PORT)
        await shutdown_event.wait()
        server.should_exit = True
        sweeper.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            sock.close()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Live gateway stopped unexpectedly")


async def _sweep_loop(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        try:
            hub.browser_sessions.sweep()
            # Close pages/contexts left behind by crashes or closed tabs even when nobody is watching.
            browser = PROVIDERS.get("browser")
            if browser is not None and hasattr(browser, "reconcile"):
                await browser.reconcile()
        except Exception:
            logger.exception("Session sweep failed")
        await asyncio.sleep(5)
