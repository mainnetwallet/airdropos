"""The Swagger UI / ReDoc pages must be allowed to load their CDN assets.

Regression: the blanket ``default-src 'self'`` policy blocked the CDN scripts and the
inline initialiser, so ``/docs`` rendered a blank page. Only the two documentation
pages are relaxed; everything else keeps the strict policy.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.main import app


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
async def test_docs_pages_allow_cdn_assets(path):
    resp = await _get(path)
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp
    assert "script-src" in csp
    assert "frame-ancestors 'none'" in csp


async def test_other_responses_keep_strict_policy():
    resp = await _get("/")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert csp == "default-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    assert "cdn.jsdelivr.net" not in csp
    assert "unsafe-inline" not in csp


async def test_openapi_json_keeps_strict_policy():
    resp = await _get("/openapi.json")
    assert "unsafe-inline" not in resp.headers["content-security-policy"]
