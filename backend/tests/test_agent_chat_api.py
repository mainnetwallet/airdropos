"""HTTP-level tests for the chat endpoint and the new agent surfaces.

These prove the wiring, not just the tools: that `POST /agent/chat` really runs the
tools the model asked for, that it enforces the agent_chat scope, that it returns the
structured report the frontend renders, and that a created task gets a real id.

The fixed LLM is stubbed. Nothing here contacts a model, a chain, or a source site.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    made = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_get_db():
        async with made() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    yield made
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest_asyncio.fixture
async def operator(factory):
    """A full-scope operator session."""
    email = f"op-{uuid.uuid4().hex[:8]}@test.local"
    token = generate_token()
    async with factory() as db:
        user = User(
            email=email,
            hashed_password=hash_password("correct-horse-battery"),
            is_active=True,
            is_superuser=True,
        )
        db.add(user)
        await db.flush()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=sorted(s.value for s in Scope),
            )
        )
        await db.commit()
    return token


def _client(token: str | None) -> AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    )


class _StubResponse:
    def __init__(self, payload: Dict[str, Any]):
        self.content = json.dumps(payload)
        self.model = "fixed-model"
        self.usage = {"total_tokens": 42}


class _StubLLM:
    """Replaces the single fixed model for the duration of one test."""

    def __init__(self, payloads: List[Dict[str, Any]]):
        self._payloads = list(payloads)
        self.calls: List[List[Any]] = []

    async def complete(self, messages, json_mode=False, **kwargs):
        self.calls.append(messages)
        payload = self._payloads.pop(0) if self._payloads else {"reply": "done", "tool_calls": []}
        return _StubResponse(payload)

    @staticmethod
    def parse_json(content: str) -> Dict[str, Any]:
        return json.loads(content)


@pytest.fixture
def stub_llm(monkeypatch):
    def install(payloads: List[Dict[str, Any]]) -> _StubLLM:
        from backend.app.tools import bootstrap as bootstrap_module

        bootstrap_module.bootstrap_tools()
        stub = _StubLLM(payloads)
        monkeypatch.setattr("backend.app.agent.chat_loop.llm", stub)
        return stub

    return install


# ======================================================================
# Authentication
# ======================================================================
class TestChatAuthentication:
    @pytest.mark.asyncio
    async def test_chat_requires_authentication(self, factory, stub_llm):
        stub_llm([{"reply": "hi", "tool_calls": []}])
        async with _client(None) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "hello"})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_chat_requires_the_agent_chat_scope(self, factory):
        email = f"narrow-{uuid.uuid4().hex[:8]}@test.local"
        token = generate_token()
        async with factory() as db:
            user = User(
                email=email,
                hashed_password=hash_password("correct-horse-battery"),
                is_active=True,
            )
            db.add(user)
            await db.flush()
            db.add(
                UserSession(
                    user_id=user.id,
                    token=hash_token(token),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    scopes=[Scope.READ.value],
                )
            )
            await db.commit()

        async with _client(token) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "hello"})
        # A session without agent_chat must not be able to drive the agent, whatever
        # it asks for.
        assert response.status_code == 403


# ======================================================================
# The chat endpoint executes tools
# ======================================================================
class TestChatExecutesTools:
    @pytest.mark.asyncio
    async def test_a_wallet_address_request_returns_a_real_tool_result(
        self, factory, operator, stub_llm, monkeypatch
    ):
        from backend.app.database import session as session_module
        from backend.app.wallets.service import WalletService
        from sqlalchemy.pool import StaticPool as _StaticPool

        # The tool opens its own session; point that factory at the test database.
        engine = factory.kw["bind"]
        monkeypatch.setattr(
            session_module,
            "AsyncSessionLocal",
            lambda *a, **k: session_module.AsyncSession(engine, expire_on_commit=False),
        )

        async with factory() as db:
            wallet = await WalletService(db).create_wallet(
                label="Main", supported_networks=["ethereum"]
            )
            await db.commit()
            address = wallet.address

        stub = stub_llm(
            [
                {
                    "reply": "Looking that up.",
                    "tool_calls": [{"tool": "get_wallet_address", "arguments": {"label": "Main"}}],
                },
                {"reply": "Your address is the one shown.", "tool_calls": []},
            ]
        )

        async with _client(operator) as client:
            response = await client.post(
                "/api/v1/agent/chat", json={"message": "Show my wallet address"}
            )

        assert response.status_code == 200
        body = response.json()

        # The tool really ran.
        assert [c["tool"] for c in body["tool_calls"]] == ["get_wallet_address"]
        assert body["tool_calls"][0]["status"] == "success"
        # And it returned the address the wallet service actually stored.
        assert body["tool_calls"][0]["data"]["address"] == address
        assert body["tool_calls"][0]["data"]["copyable_address"] == address
        # Plus the evidence the tool attached.
        assert body["verified"][0]["evidence"]["kind"] == "wallet_record"
        # The model saw the tool result before answering.
        assert len(stub.calls) == 2

    @pytest.mark.asyncio
    async def test_the_response_carries_the_full_report_the_frontend_renders(
        self, factory, operator, stub_llm
    ):
        stub_llm(
            [
                {
                    "reply": "",
                    "tool_calls": [{"tool": "list_wallets", "arguments": {}}],
                },
                {"reply": "No wallets are configured yet.", "tool_calls": []},
            ]
        )

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "my wallets"})
        body = response.json()

        for key in (
            "reply", "tool_calls", "task", "approvals_required", "blockers",
            "needs_user_action", "verified", "usage", "truncated",
        ):
            assert key in body, key
        assert body["model_policy"] == "single_fixed_model"

    @pytest.mark.asyncio
    async def test_a_task_request_still_creates_a_real_task(self, factory, operator, stub_llm):
        stub_llm(
            [
                {
                    "reply": "Created and queued.",
                    "tool_calls": [],
                    "create_task": {
                        "title": "Claim the Example airdrop",
                        "description": "Follow the steps.",
                        "category": "airdrop",
                        "network": "ethereum",
                        "priority": 3,
                    },
                }
            ]
        )

        async with _client(operator) as client:
            response = await client.post(
                "/api/v1/agent/chat",
                json={"message": "Start this airdrop: https://example.test/airdrop"},
            )

        body = response.json()
        assert body["task"] is not None
        assert body["task"]["task_id"].startswith("AIR-")
        assert body["task"]["status"] == "READY"
        assert body["task"]["worker_will_execute"] is True

    @pytest.mark.asyncio
    async def test_the_task_description_carries_the_operators_own_words(
        self, factory, operator, stub_llm
    ):
        stub_llm(
            [
                {
                    "reply": "ok",
                    "tool_calls": [],
                    "create_task": {"title": "Follow up", "description": "Step one."},
                }
            ]
        )

        async with _client(operator) as client:
            response = await client.post(
                "/api/v1/agent/chat",
                json={
                    "message": "use this link https://example.test/airdrop",
                    "history": [
                        {"role": "user", "content": "my wallet is 0x" + "ab" * 20},
                        {"role": "assistant", "content": "noted"},
                    ],
                },
            )

        body = response.json()
        task_id = body["task"]["task_id"]

        from sqlalchemy import select

        from backend.app.models import Task

        async with factory() as db:
            task = await db.scalar(select(Task).where(Task.task_id == task_id))
            # The worker never sees the chat, so the earlier turns must be in the task.
            assert "0x" + "ab" * 20 in task.description
            assert "https://example.test/airdrop" in task.description
            assert "untrusted data" in task.description

    @pytest.mark.asyncio
    async def test_history_is_forwarded_so_the_model_has_context(self, factory, operator, stub_llm):
        stub = stub_llm([{"reply": "Continuing.", "tool_calls": []}])

        async with _client(operator) as client:
            await client.post(
                "/api/v1/agent/chat",
                json={
                    "message": "continue",
                    "history": [
                        {"role": "user", "content": "Start the ACME airdrop"},
                        {"role": "assistant", "content": "Task created."},
                    ],
                },
            )

        sent = stub.calls[0]
        rendered = " ".join(m.content for m in sent)
        assert "Start the ACME airdrop" in rendered
        assert "Task created." in rendered
        assert "continue" in rendered

    @pytest.mark.asyncio
    async def test_a_model_outage_is_reported_rather_than_faked(self, factory, operator, monkeypatch):
        from backend.app.tools import bootstrap as bootstrap_module
        from backend.app.llm.client import LLMError

        bootstrap_module.bootstrap_tools()

        class _Down:
            async def complete(self, *a, **k):
                raise LLMError("endpoint unreachable", category="CONNECTION_ERROR")

        monkeypatch.setattr("backend.app.agent.chat_loop.llm", _Down())

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "hello"})

        # The turn completes with the outage reported as a blocker, and nothing is
        # claimed to have run. The response is deliberately still a 200 with an
        # explicit blocker rather than a fabricated answer.
        assert response.status_code == 200
        body = response.json()
        assert body["tool_calls"] == []
        assert body["task"] is None
        assert any("unavailable" in b.lower() or "CONNECTION_ERROR" in b for b in body["blockers"])
        assert body["blockers"]

    @pytest.mark.asyncio
    async def test_a_hallucinated_tool_is_reported_not_executed(self, factory, operator, stub_llm):
        stub_llm(
            [
                {
                    "reply": "",
                    "tool_calls": [{"tool": "sudo_transfer_all_funds", "arguments": {}}],
                },
                {"reply": "I have no tool that can do that.", "tool_calls": []},
            ]
        )

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "send it all"})

        body = response.json()
        assert body["tool_calls"][0]["status"] == "failure"
        assert body["tool_calls"][0]["error_category"] == "UNSUPPORTED_OPERATION"

    @pytest.mark.asyncio
    async def test_submission_tool_is_absent_from_the_model_catalog_by_default(
        self, factory, operator, stub_llm
    ):
        from backend.app.agent.prompts import build_chat_system_prompt
        from backend.app.tools.bootstrap import bootstrap_tools
        from backend.app.tools.registry import registry

        bootstrap_tools()
        catalog = registry.catalog_for_llm(registry.policy)
        names = {c["name"] for c in catalog}

        # Value moving is off unless an operator enables it, so the model is not even
        # shown the tool. This is the default deployment posture.
        assert "submit_prepared_transfer" not in names
        # Read and analyse capabilities ARE shown, which is what makes the console able
        # to answer questions rather than only give advice.
        assert {"get_wallet_address", "list_tasks", "system_diagnostics"} <= names

        # And the catalog is rendered into the prompt with its argument names.
        rendered = build_chat_system_prompt(catalog)
        assert "get_wallet_address(" in rendered
        assert "prepare_native_transfer(" in rendered


# ======================================================================
# Emergency stop at the HTTP surface
# ======================================================================
class TestEmergencyStopThroughChat:
    @pytest.mark.asyncio
    async def test_a_stopped_platform_refuses_a_state_change_tool(
        self, factory, operator, stub_llm, monkeypatch
    ):
        from backend.app.security.emergency_stop import emergency_stop
        from backend.app.tools.bootstrap import bootstrap_tools
        from backend.app.tools.registry import AuthorizationPolicy, registry

        bootstrap_tools()
        monkeypatch.setattr(
            registry,
            "policy",
            AuthorizationPolicy(
                enabled_permissions=["wallet.write", "system.read"],
                allow_web_interaction=True,
            ),
        )
        emergency_stop.activate("incident", "test")
        try:
            stub_llm(
                [
                    {"reply": "", "tool_calls": [{"tool": "create_wallet", "arguments": {"label": "X"}}]},
                    {"reply": "Blocked.", "tool_calls": []},
                ]
            )
            async with _client(operator) as client:
                response = await client.post("/api/v1/agent/chat", json={"message": "create a wallet"})

            body = response.json()
            assert body["tool_calls"][0]["status"] == "not_authorized"
            assert body["tool_calls"][0]["error_category"] == "EMERGENCY_STOP"
        finally:
            emergency_stop.resume("test cleanup")


# ======================================================================
# Diagnostics endpoints still behave
# ======================================================================
class TestDiagnosticsEndpoints:
    @pytest.mark.asyncio
    async def test_health_detail_reports_subsystems_without_a_key(self, factory, operator):
        async with _client(operator) as client:
            response = await client.get("/api/v1/health/detail")

        assert response.status_code == 200
        body = response.json()
        # The endpoint builds its own summary; the key fields must not appear in it.
        assert body["llm"]["policy"] == "single_fixed_model"
        assert "api_key" not in body["llm"]
        assert "api_key_masked" not in body["llm"]
        # Every subsystem the agent can be asked about is present.
        assert {"database", "emergency_stop", "llm", "browser", "telegram"} <= set(body)

    @pytest.mark.asyncio
    async def test_agent_info_never_returns_the_api_key(self, factory, operator):
        async with _client(operator) as client:
            response = await client.get("/api/v1/agent/info")

        body = response.json()
        assert "api_key" not in body
        assert body["api_key_masked"].count("*") > 0

    @pytest.mark.asyncio
    async def test_tool_catalog_endpoint_lists_the_new_capabilities(self, factory, operator):
        async with _client(operator) as client:
            response = await client.get("/api/v1/tools")

        assert response.status_code == 200
        names = {t["name"] for t in response.json()["tools"]}
        assert {
            "get_wallet_address", "create_wallet", "list_tasks", "get_task",
            "prepare_native_transfer", "prepare_token_transfer",
            "submit_prepared_transfer", "system_diagnostics", "discover_airdrops",
        } <= names

class _RawLLM:
    """Returns raw text (not parsed dicts) and uses the real JSON extraction."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls: List[List[Any]] = []

    async def complete(self, messages, json_mode=False, **kwargs):
        self.calls.append(list(messages))
        text = self._texts.pop(0)
        return _RawResponse(text)

    @staticmethod
    def parse_json(content: str) -> Dict[str, Any]:
        from backend.app.llm.client import FixedLLMClient

        return FixedLLMClient.parse_json(content)


class _RawResponse:
    def __init__(self, text: str) -> None:
        self.content = text
        self.model = "stub"
        self.usage: Dict[str, Any] = {}


_TASK_JSON = json.dumps(
    {
        "reply": "Created and queued.",
        "tool_calls": [],
        "create_task": {
            "title": "LockOn airdrop",
            "description": "Follow the steps.",
            "category": "airdrop",
            "network": "solana",
            "priority": 5,
        },
    }
)


class TestWrappedModelOutput:
    @pytest.mark.asyncio
    async def test_prose_plus_fenced_json_still_creates_the_task(self, factory, operator, monkeypatch):
        from backend.app.tools import bootstrap as bootstrap_module

        bootstrap_module.bootstrap_tools()
        raw = _RawLLM(["I'll create a task for this.\n\n```json\n" + _TASK_JSON + "\n```"])
        monkeypatch.setattr("backend.app.agent.chat_loop.llm", raw)

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "create task"})

        body = response.json()
        assert body["task"] is not None and body["task"]["task_id"].startswith("AIR-")
        assert not any("structured response" in b for b in body.get("blockers", []))
        assert len(raw.calls) == 1  # no retry was needed

    @pytest.mark.asyncio
    async def test_prose_gets_one_retry_then_works(self, factory, operator, monkeypatch):
        from backend.app.tools import bootstrap as bootstrap_module

        bootstrap_module.bootstrap_tools()
        raw = _RawLLM(["Shall I create a task for this campaign?", _TASK_JSON])
        monkeypatch.setattr("backend.app.agent.chat_loop.llm", raw)

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "create task"})

        body = response.json()
        assert body["task"] is not None
        assert len(raw.calls) == 2
        assert "valid JSON" in raw.calls[1][-1].content

    @pytest.mark.asyncio
    async def test_still_prose_after_the_retry_is_blocked_and_nothing_runs(self, factory, operator, monkeypatch):
        from backend.app.tools import bootstrap as bootstrap_module

        bootstrap_module.bootstrap_tools()
        raw = _RawLLM(["Sure, I will do that.", "I really will, trust me."])
        monkeypatch.setattr("backend.app.agent.chat_loop.llm", raw)

        async with _client(operator) as client:
            response = await client.post("/api/v1/agent/chat", json={"message": "create task"})

        body = response.json()
        assert body["task"] is None
        assert len(raw.calls) == 2  # exactly one re-ask, never a loop
        assert any("structured response" in b for b in body.get("blockers", []))
