"""API-side client for the worker gateway."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from backend.app.core.config import settings
from backend.app.live.security import GATEWAY_HEADER, gateway_secret

logger = logging.getLogger(__name__)


class GatewayUnavailable(Exception):
    """The worker gateway cannot be reached (worker down, browser disabled, wrong URL)."""


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.LIVE_GATEWAY_URL.rstrip("/"),
            timeout=httpx.Timeout(12.0, connect=2.0),
            headers={GATEWAY_HEADER: gateway_secret()},
        )
    return _client


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code, message = "error", "Request failed"
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, dict):
            code, message = detail.get("code", code), detail.get("message", message)
        elif isinstance(detail, str):
            message = detail
    except Exception:
        pass
    raise GatewayError(resp.status_code, code, message)


async def request(method: str, path: str, *, json: Any = None, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
    try:
        resp = await _http().request(method, path, json=json, params=params)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
        raise GatewayUnavailable(type(exc).__name__) from exc
    if resp.status_code == 401:
        raise GatewayUnavailable("gateway rejected the shared secret (check SECRET_KEY / LIVE_GATEWAY_SECRET)")
    _raise_for(resp)
    return resp


async def get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return (await request("GET", path, params=params)).json()


async def post_json(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return (await request("POST", path, json=body)).json()
