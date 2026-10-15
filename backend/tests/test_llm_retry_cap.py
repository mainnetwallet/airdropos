"""An LLM call is retried once on error, then fails: two attempts in total, never three."""

import httpx
import pytest

import backend.app.llm.client as client_mod
from backend.app.llm.client import FixedLLMClient, LLMError


@pytest.mark.asyncio
async def test_timeout_is_tried_exactly_twice(monkeypatch):
    monkeypatch.setattr(client_mod.settings, "LLM_MAX_RETRIES", 3)  # old .env value
    llm = FixedLLMClient()
    assert llm._max_attempts == 2

    calls = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            calls["n"] += 1
            raise httpx.ReadTimeout("slow")

    async def _nosleep(_): ...

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(client_mod.asyncio, "sleep", _nosleep)
    with pytest.raises(LLMError) as err:
        await llm.complete([client_mod.Message("user", "hi")])
    assert err.value.category == "TIMEOUT"
    assert calls["n"] == 2
