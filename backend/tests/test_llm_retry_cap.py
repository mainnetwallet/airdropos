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


def test_every_network_timeout_phase_is_finite():
    """`None` on the httpx timeout is "wait forever", and this asserts it is gone.

    The client used to set `_timeout = None`, which httpx reads as no limit on connect,
    read, write or pool alike. One hung socket then pinned a worker for ever: no error,
    no retry, no telemetry, and the task simply stopped. Every phase is now finite and
    configurable, which is a per-attempt bound and not a bound on the task.
    """

    client = FixedLLMClient()
    for label, timeouts in (
        ("non-streamed", client._http_timeouts),
        ("streamed", client._stream_http_timeouts),
    ):
        for phase in ("connect", "read", "write", "pool"):
            value = getattr(timeouts, phase)
            assert value is not None, f"{label} {phase} timeout must be finite, not None"
            assert value > 0, f"{label} {phase} timeout must be positive, got {value}"
            assert value <= 3600, f"{label} {phase} timeout is absurdly long: {value}"

    # Streamed read inactivity is deliberately more generous than non-streamed: a stream
    # is idle between tokens by nature, and a tight read timeout would kill a healthy
    # long answer between words.
    assert client._stream_http_timeouts.read >= client._http_timeouts.read


def test_a_requested_timeout_is_clamped_to_the_hard_ceiling():
    """A caller cannot turn a per-attempt bound into an unbounded wait."""
    from backend.app.tools.timeouts import HARD_TIMEOUT_CEILING_SECONDS, resolve_timeout

    class SlowTool:
        name = "slow"
        timeout_seconds = 30
        side_effect = None
        __module__ = "backend.app.tools.blockchain_tools"

    seconds, _ = resolve_timeout(SlowTool(), requested=10_000_000)
    assert seconds <= HARD_TIMEOUT_CEILING_SECONDS


def test_a_tool_timeout_floor_survives_a_short_requested_value():
    """A malformed or absurd request degrades to the computed default, never below it."""
    from backend.app.tools.timeouts import HARD_TIMEOUT_FLOOR_SECONDS, resolve_timeout

    class QuickTool:
        name = "quick"
        timeout_seconds = 30
        side_effect = None
        __module__ = "backend.app.tools.arithmetic_tools"

    for bad in (-1, 0, "immediately", None, object()):
        seconds, _ = resolve_timeout(QuickTool(), requested=bad)
        assert seconds >= HARD_TIMEOUT_FLOOR_SECONDS, f"{bad!r} produced {seconds}"


# ======================================================================
# Exact-once accounting, exercised through the real transport
# ======================================================================


class _CountingContext:
    """Counts physical attempts and records what the task registry saw."""

    def __init__(self, responder):
        self.attempts = 0
        self._responder = responder

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        self.attempts += 1
        return self._responder(self.attempts)


def _response(status: int, body: str = ""):
    class _Resp:
        status_code = status
        text = body

        def json(self):
            return {"error": {"message": body}}

    return _Resp()


@pytest.mark.asyncio
async def test_a_rate_limited_attempt_is_counted_exactly_once(monkeypatch):
    """Two 429 attempts are two requests, not four.

    This drives the real `_post`. The counting used to sit on BOTH the error-response
    branch and the bottom of the retry loop, so a 429 -- which falls through to the retry
    rather than raising -- passed through both and was counted twice per attempt.
    """
    from backend.app.tasks import telemetry
    from backend.app.tools.context import task_context

    telemetry.reset_task_usage()
    client = FixedLLMClient()
    counter = _CountingContext(lambda n: _response(429, "slow down"))

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", counter)
    async def _nosleep(_): ...
    monkeypatch.setattr(client_mod.asyncio, "sleep", _nosleep)

    with task_context("AIR-TEST-000001"):
        with pytest.raises(LLMError):
            await client.complete([client_mod.Message("user", "hi")])

    usage = telemetry.registry.take("AIR-TEST-000001")
    assert counter.attempts == 2, f"expected 2 physical attempts, made {counter.attempts}"
    assert usage.llm_requests == 2, (
        f"{counter.attempts} physical attempts were reported as {usage.llm_requests} requests"
    )


@pytest.mark.asyncio
async def test_a_timed_out_attempt_is_counted(monkeypatch):
    """A timeout is a request that was made, not a request that never happened.

    `httpx.TimeoutException` was caught separately from the other transport errors and
    recorded nowhere, so a provider that hung burned real wall-clock and token spend that
    appeared in no per-task telemetry at all.
    """
    from backend.app.tasks import telemetry
    from backend.app.tools.context import task_context

    telemetry.reset_task_usage()
    client = FixedLLMClient()
    counter = _CountingContext(lambda n: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", counter)
    async def _nosleep(_): ...
    monkeypatch.setattr(client_mod.asyncio, "sleep", _nosleep)

    with task_context("AIR-TEST-000002"):
        with pytest.raises(LLMError):
            await client.complete([client_mod.Message("user", "hi")])

    usage = telemetry.registry.take("AIR-TEST-000002")
    assert usage.llm_requests == 2, f"2 attempts reported as {usage.llm_requests}"


@pytest.mark.asyncio
async def test_a_connection_failure_is_counted(monkeypatch):
    """An unreachable endpoint is still a request against the task's budget."""
    from backend.app.tasks import telemetry
    from backend.app.tools.context import task_context

    telemetry.reset_task_usage()
    client = FixedLLMClient()
    counter = _CountingContext(
        lambda n: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", counter)
    async def _nosleep(_): ...
    monkeypatch.setattr(client_mod.asyncio, "sleep", _nosleep)

    with task_context("AIR-TEST-000003"):
        with pytest.raises(LLMError):
            await client.complete([client_mod.Message("user", "hi")])

    usage = telemetry.registry.take("AIR-TEST-000003")
    assert usage.llm_requests == 2, f"2 attempts reported as {usage.llm_requests}"


@pytest.mark.asyncio
async def test_a_successful_call_is_counted_once(monkeypatch):
    """The success path is counted once and carries the provider's real usage."""
    from backend.app.tasks import telemetry
    from backend.app.tools.context import task_context

    telemetry.reset_task_usage()
    client = FixedLLMClient()

    class _Ok:
        status_code = 200
        text = ""

        def json(self):
            return {
                "model": client.model_id,
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            }

    counter = _CountingContext(lambda n: _Ok())
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", counter)

    with task_context("AIR-TEST-000004"):
        response = await client.complete([client_mod.Message("user", "hi")])

    assert response.content == "hello"
    usage = telemetry.registry.take("AIR-TEST-000004")
    assert usage.llm_requests == 1
    assert usage.input_tokens == 11
    assert usage.output_tokens == 3
    assert usage.totals_are_exact is True
