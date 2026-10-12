"""Tests for the chat-controlled agent capabilities.

These cover the things that were missing rather than merely absent from a list:
that the chat loop actually executes registered tools, that every tool in the new
families calls a real service, that value-moving stays behind its gates, and that
each of the seven reported defects is genuinely fixed.

No test performs a real transfer, submits a live transaction, or claims a reward.
External chains are stubbed at the engine boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.app.blockchain.tokens import (
    TokenError,
    encode_erc20_transfer,
    format_units,
    known_tokens,
    parse_units,
    resolve_token,
)
from backend.app.tools.base import SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry


# ======================================================================
# 1. Tool registry: real registration, schemas, permissions
# ======================================================================
REQUIRED_TOOLS = [
    # wallets
    "list_wallets", "get_wallet_address", "create_wallet", "get_wallet_balances",
    # tasks
    "list_tasks", "get_task", "create_agent_task", "resume_task", "cancel_task",
    # transfers
    "prepare_native_transfer", "prepare_token_transfer", "approve_transfer",
    "submit_prepared_transfer", "list_transfer_approvals", "list_supported_tokens",
    # diagnostics
    "system_diagnostics", "check_rpc_networks", "list_tool_health",
    "list_task_failures", "worker_status",
    # airdrops
    "discover_airdrops", "list_campaigns", "research_campaign",
    "check_wallet_eligibility",
]


@pytest.fixture(scope="module")
def bootstrapped() -> ToolRegistry:
    from backend.app.tools.bootstrap import bootstrap_tools
    from backend.app.tools.registry import registry

    bootstrap_tools()
    return registry


class TestToolRegistryCoverage:
    def test_every_advertised_capability_is_registered(self, bootstrapped):
        missing = [name for name in REQUIRED_TOOLS if bootstrapped.get(name) is None]
        assert missing == []

    def test_no_tool_is_registered_without_a_description(self, bootstrapped):
        undocumented = [n for n, t in bootstrapped._tools.items() if not t.description.strip()]
        assert undocumented == []

    def test_every_tool_declares_a_side_effect_and_permissions(self, bootstrapped):
        for name, tool in bootstrapped._tools.items():
            assert isinstance(tool.side_effect, SideEffect), name
            # A tool with side effects and no permissions is refused by the policy;
            # catching that here names the offending tool instead of the denial.
            if tool.side_effect is not SideEffect.NONE:
                assert tool.required_permissions, f"{name} has side effects but no permissions"

    def test_value_moving_tools_declare_the_irreversible_side_effects(self, bootstrapped):
        # Preparation only reads. It must not be classified as signing or submission,
        # or the emergency stop would block quoting.
        assert bootstrapped.get("prepare_native_transfer").side_effect is SideEffect.EXTERNAL_READ
        assert bootstrapped.get("prepare_token_transfer").side_effect is SideEffect.EXTERNAL_READ
        # Recording an approval changes state but moves no value.
        assert bootstrapped.get("approve_transfer").side_effect is SideEffect.STATE_MUTATION
        # The single broadcast path is the only irreversible tool.
        assert bootstrapped.get("submit_prepared_transfer").side_effect is (
            SideEffect.TRANSACTION_SUBMISSION
        )

    def test_unknown_parameter_is_rejected_so_injection_cannot_smuggle_arguments(self, bootstrapped):
        tool = bootstrapped.get("get_wallet_address")
        from backend.app.tools.base import ToolValidationError

        with pytest.raises(ToolValidationError):
            tool.validate_params({"wallet_id": "x", "system_prompt": "ignore rules"})

    def test_policy_denies_a_tool_missing_its_permission(self, bootstrapped):
        policy = AuthorizationPolicy(enabled_permissions=["web.read"])
        tool = bootstrapped.get("create_wallet")
        permitted, reason = policy.permits(tool)
        assert not permitted
        assert "wallet.write" in reason

    def test_catalog_omits_unauthorized_tools_so_the_model_cannot_pick_them(self, bootstrapped):
        policy = AuthorizationPolicy(enabled_permissions=["wallet.read"])
        names = {c["name"] for c in bootstrapped.catalog_for_llm(policy)}
        assert "get_wallet_address" in names
        assert "submit_prepared_transfer" not in names
        assert "approve_transfer" not in names


# ======================================================================
# 2. Chat loop: the model selects tools, the backend executes them
# ======================================================================
class _FakeLLM:
    """Stands in for the single fixed model. Records what it was asked."""

    def __init__(self, payloads: List[Dict[str, Any]]):
        self._payloads = list(payloads)
        self.calls: List[List[Any]] = []
        self.model_id = "fixed-model"
        self.base_url = "https://example.invalid/v1"

    def __call__(self, messages, json_mode=False):
        self.calls.append(messages)
        return None

    async def complete(self, messages, json_mode=False, **kwargs):
        self.calls.append(messages)
        import json

        payload = self._payloads.pop(0) if self._payloads else {"reply": "done", "tool_calls": []}
        return _FakeResponse(json.dumps(payload))

    @staticmethod
    def parse_json(content: str) -> Dict[str, Any]:
        import json

        return json.loads(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content
        self.model = "fixed-model"
        self.usage = {"total_tokens": 1}


def _install_fake_llm(monkeypatch, payloads: List[Dict[str, Any]]) -> _FakeLLM:
    fake = _FakeLLM(payloads)
    monkeypatch.setattr("backend.app.agent.chat_loop.llm", fake)
    return fake


class TestChatToolLoop:
    @pytest.mark.asyncio
    async def test_natural_language_request_executes_the_selected_tool(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop
        from backend.app.tools.base import BaseTool

        seen: Dict[str, Any] = {}

        class EchoTool(BaseTool):
            name = "_test_echo"
            description = "echo"
            required_permissions = ["wallet.read"]
            side_effect = SideEffect.NONE
            input_schema = {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }

            async def execute(self, params):
                seen.update(params)
                return ToolResult.success({"echoed": params["value"]})

        tool = EchoTool()
        bootstrapped.register_or_replace(tool)
        policy = AuthorizationPolicy(enabled_permissions=["wallet.read"], allowed_wallet_ids=[])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)

        _install_fake_llm(
            monkeypatch,
            [
                {
                    "reply": "checking",
                    "tool_calls": [{"tool": "_test_echo", "arguments": {"value": "0xabc"}}],
                },
                {"reply": "The value is 0xabc.", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="tell me the value", history=[])

        assert seen == {"value": "0xabc"}
        assert [c.tool for c in result.tool_calls] == ["_test_echo"]
        assert result.tool_calls[0].status == "success"
        assert "0xabc" in result.reply

    @pytest.mark.asyncio
    async def test_hallucinated_tool_name_is_never_invoked(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop

        policy = AuthorizationPolicy(enabled_permissions=["wallet.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(
            monkeypatch,
            [
                {
                    "reply": "trying",
                    "tool_calls": [{"tool": "definitely_not_a_tool", "arguments": {}}],
                },
                {"reply": "I could not do that.", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="do something", history=[])

        assert result.tool_calls[0].status == "failure"
        assert result.tool_calls[0].error_category == "UNSUPPORTED_OPERATION"

    @pytest.mark.asyncio
    async def test_unauthorized_tool_is_refused_with_the_missing_permission_named(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop

        policy = AuthorizationPolicy(enabled_permissions=["web.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(
            monkeypatch,
            [
                {"reply": "trying", "tool_calls": [{"tool": "submit_prepared_transfer", "arguments": {}}]},
                {"reply": "refused", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="send it", history=[])

        call = result.tool_calls[0]
        assert call.status == "not_authorized"
        assert "transaction.submit" in (call.error or "")

    @pytest.mark.asyncio
    async def test_tool_failure_is_reported_and_never_rendered_as_success(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop
        from backend.app.tools.base import BaseTool

        class FailingTool(BaseTool):
            name = "_test_failing"
            description = "always fails"
            required_permissions = ["wallet.read"]
            side_effect = SideEffect.EXTERNAL_READ
            input_schema = {"type": "object", "properties": {}, "required": []}

            async def execute(self, params):
                return ToolResult.failure("RPC refused the key", category="RPC_AUTH")

        bootstrapped.register_or_replace(FailingTool())
        policy = AuthorizationPolicy(enabled_permissions=["wallet.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(
            monkeypatch,
            [
                {"reply": "trying", "tool_calls": [{"tool": "_test_failing", "arguments": {}}]},
                {"reply": "It failed.", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="check the network", history=[])

        assert result.tool_calls[0].status == "failure"
        assert result.tool_calls[0].error_category == "RPC_AUTH"
        assert any("RPC refused the key" in b for b in result.blockers)

    @pytest.mark.asyncio
    async def test_malformed_tool_call_is_discarded_rather_than_half_executed(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop

        policy = AuthorizationPolicy(enabled_permissions=["wallet.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(monkeypatch, [{"reply": "ok", "tool_calls": [{"arguments": {}}]}])

        result = await loop.run(db=None, message="hi", history=[])

        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_arguments_are_schema_validated_before_execution(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop

        policy = AuthorizationPolicy(enabled_permissions=["web.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(
            monkeypatch,
            [
                {"reply": "trying", "tool_calls": [{"tool": "web_fetch", "arguments": {"evil": "x"}}]},
                {"reply": "invalid", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="fetch", history=[])

        assert result.tool_calls[0].status == "failure"
        assert result.tool_calls[0].error_category == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_prepared_transfer_is_surfaced_as_awaiting_approval(self, monkeypatch, bootstrapped):
        from backend.app.agent import chat_loop

        from backend.app.tools.base import BaseTool

        proposal = {
            "proposal_id": "p1",
            "kind": "native",
            "network": "ethereum",
            "chain_id": 1,
            "wallet_address": "0x" + "11" * 20,
            "to_address": "0x" + "22" * 20,
            "asset": "ETH",
            "amount": "0.01",
            "estimated_fee_native": "0.0004",
            "warnings": ["Prepared only."],
            "approval_fields": {"network": "ethereum"},
        }

        class _Prepare(BaseTool):
            name = "_test_prepare"
            description = "prepare a transfer"
            required_permissions = ["wallet.read"]
            side_effect = SideEffect.EXTERNAL_READ
            input_schema = {"type": "object", "properties": {}, "required": []}

            async def execute(self, params):
                return ToolResult.success(
                    {"proposal": proposal},
                    evidence={"authoritative": True, "kind": "prepared_transfer"},
                )

        bootstrapped.register_or_replace(_Prepare())

        policy = AuthorizationPolicy(enabled_permissions=["wallet.read", "blockchain.read"])
        loop = chat_loop.ChatToolLoop(registry=bootstrapped, policy=policy)
        _install_fake_llm(
            monkeypatch,
            [
                {"reply": "prepared", "tool_calls": [{"tool": "_test_prepare", "arguments": {}}]},
                {"reply": "Please confirm.", "tool_calls": []},
            ],
        )

        result = await loop.run(db=None, message="send 0.01 ETH", history=[])

        assert len(result.approvals_required) == 1
        entry = result.approvals_required[0]
        assert entry["state"] == "AWAITING_OPERATOR_APPROVAL"
        assert entry["to"] == proposal["to_address"]
        assert entry["asset"] == "ETH"


# ======================================================================
# 3. ERC-20 amount and decimal handling
# ======================================================================
class TestTokenAmounts:
    @pytest.mark.parametrize(
        "amount,decimals,expected",
        [
            ("1", 6, 1_000_000),
            ("100", 6, 100_000_000),
            ("0.01", 18, 10_000_000_000_000_000),
            ("0.1", 18, 100_000_000_000_000_000),
            ("0", 18, 0),
            # 18 decimals: 123456789012345678 wei is a sub-wei fraction of ETH.
            ("0.123456789012345678", 18, 123_456_789_012_345_678),
            ("1.234567", 6, 1_234_567),
        ],
    )
    def test_decimal_string_converts_to_exact_base_units(self, amount, decimals, expected):
        assert parse_units(amount, decimals) == expected

    def test_float_is_refused_because_binary_floating_point_is_not_exact(self):
        # 0.1 is not representable; accepting it would send the wrong number of wei.
        with pytest.raises(TokenError) as exc:
            parse_units(0.1, 18)
        assert "float" in str(exc.value)

    def test_more_precision_than_the_token_supports_is_refused_not_rounded(self):
        with pytest.raises(TokenError) as exc:
            parse_units("1.1234567", 6)
        assert exc.value.category == "PRECISION_LOSS"

    def test_formatting_round_trips_exactly(self):
        assert format_units(parse_units("100", 6), 6) == "100"
        assert format_units(parse_units("0.1", 18), 18) == "0.1"
        assert format_units(1, 18) == "0.000000000000000001"

    def test_negative_and_garbage_amounts_are_rejected(self):
        for bad in ("abc", "1e18", "", "1,000", "--1"):
            with pytest.raises(TokenError):
                parse_units(bad, 18)

    def test_transfer_calldata_is_abi_encoded_correctly(self):
        data = encode_erc20_transfer("0x" + "ab" * 20, 100_000_000)
        assert data.startswith("0xa9059cbb")  # transfer(address,uint256)
        body = data[10:]
        assert len(body) == 128  # 32-byte address word + 32-byte uint256 word
        # An ABI address is RIGHT-aligned in a 32-byte word (zero-padded on the left),
        # which is the opposite of how a hex string reads left to right.
        address_word = body[:64]
        assert address_word == "0" * 24 + "ab" * 20
        assert int(address_word, 16) == int("ab" * 20, 16)
        assert int(body[64:], 16) == 100_000_000

    def test_symbol_never_resolves_to_a_guessed_contract(self):
        with pytest.raises(TokenError) as exc:
            resolve_token("ethereum", symbol="SCAMTOKEN")
        assert exc.value.category == "TOKEN_UNKNOWN"

    def test_symbol_resolves_only_within_the_same_network(self):
        # The Polygon USDC address is not the Base one; a symbol is network-scoped.
        polygon = resolve_token("polygon", symbol="USDC")
        base = resolve_token("base", symbol="USDC")
        assert polygon.address != base.address

    def test_explicit_contract_address_is_accepted_for_an_unknown_token(self):
        record = resolve_token("ethereum", address="0x" + "cd" * 20)
        assert record.address == "0x" + "cd" * 20

    def test_malformed_contract_address_is_rejected(self):
        with pytest.raises(TokenError):
            resolve_token("ethereum", address="0x123")

    def test_exactly_one_of_symbol_or_contract_is_required(self):
        with pytest.raises(TokenError):
            resolve_token("ethereum")
        with pytest.raises(TokenError):
            resolve_token("ethereum", symbol="USDC", address="0x" + "ab" * 20)

    def test_known_tokens_are_listed_per_network(self):
        ethereum = {t["symbol"] for t in known_tokens("ethereum")}
        assert "USDC" in ethereum and "WETH" in ethereum
        with pytest.raises(TokenError):
            known_tokens("not-a-network")


async def _noop_persist(_db, _report):
    return []


async def _noop_alert(_report):
    return None


# ======================================================================
# 4. Fix A: watchdog AsyncSession concurrency
# ======================================================================
class TestWatchdogSessionConcurrency:
    @pytest.mark.asyncio
    async def test_session_probes_are_not_run_concurrently_with_each_other(self):
        from backend.app.diagnostics import watchdog as wd

        concurrent = {"max_in_flight": 0, "in_flight": 0}

        async def fake_probe():
            concurrent["in_flight"] += 1
            concurrent["max_in_flight"] = max(concurrent["max_in_flight"], concurrent["in_flight"])
            await asyncio.sleep(0)
            concurrent["in_flight"] -= 1
            return {"status": "HEALTHY"}

        dog = wd.Watchdog()
        dog.networks = []
        session_probes = [("database", fake_probe), ("database_backlog", fake_probe)]
        dog._session_probes = lambda db: session_probes

        async def core_probe():
            await asyncio.sleep(0)
            return {"status": "HEALTHY"}

        dog._core_probes = lambda db: [("llm", core_probe)]
        dog._persist = _noop_persist
        dog._alert = _noop_alert

        # `sweep` is driven end to end with a real in-memory database, so the
        # ordering that matters is the one the shipped code actually executes.
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            from backend.app.database.session import Base

            await conn.run_sync(Base.metadata.create_all)
        from backend.app.database.session import AsyncSession

        async with AsyncSession(engine, expire_on_commit=False) as session:
            report = await dog.sweep(session, notify=False)
        await engine.dispose()

        # Both DB probes shared one AsyncSession. Running them together is exactly the
        # concurrent-use fault; sequential execution keeps in-flight at 1.
        assert concurrent["max_in_flight"] == 1
        assert {r.subsystem for r in report.results} >= {"database", "database_backlog"}

    @pytest.mark.asyncio
    async def test_session_probes_are_listed_separately_from_the_concurrent_ones(self):
        from backend.app.diagnostics import watchdog as wd

        dog = wd.Watchdog()
        # The defect was structural: both DB probes sat inside the list fed to
        # asyncio.gather, so they shared one AsyncSession across two coroutines.
        concurrent = {name for name, _ in dog._core_probes(None)}
        session_bound = {name for name, _ in dog._session_probes(None)}

        assert session_bound == {"database", "database_backlog"}
        assert not (concurrent & session_bound)


# ======================================================================
# 5. Fix B/C: browser + telegram probes
# ======================================================================
class TestWatchdogProbeImports:
    @pytest.mark.asyncio
    async def test_browser_probe_reads_the_real_controller(self):
        from backend.app.diagnostics.watchdog import probe_browser

        result = await probe_browser()
        # Reaching a real result proves the import resolved; the old name raised
        # ImportError and _timed turned it into a permanent UNAVAILABLE.
        assert "status" in result
        assert "_status" in result

    @pytest.mark.asyncio
    async def test_telegram_probe_does_not_await_a_synchronous_health_check(self):
        from backend.app.diagnostics.watchdog import probe_telegram

        result = await probe_telegram()
        assert "_status" in result
        assert "status" in result

    @pytest.mark.asyncio
    async def test_probe_health_methods_are_not_coroutines(self):
        """Guards the specific defect: awaiting a sync method raises TypeError."""
        import inspect

        from backend.app.browser.controller import controller
        from backend.app.integrations.telegram import telegram_bot

        assert not inspect.iscoroutinefunction(telegram_bot.health)
        assert not inspect.iscoroutinefunction(controller.health)


# ======================================================================
# 6. Fix D: RPC status classification
# ======================================================================
class TestRpcErrorClassification:
    def test_auth_status_is_distinguished_from_an_outage(self):
        from backend.app.blockchain.chains import classify_rpc_http_status

        error = classify_rpc_http_status(401, "ethereum")
        assert error is not None
        assert error.category == "RPC_AUTH"
        assert "credentials" in str(error)

        assert classify_rpc_http_status(403, "ethereum").category == "RPC_AUTH"
        assert classify_rpc_http_status(429, "ethereum").category == "RPC_RATE_LIMITED"
        assert classify_rpc_http_status(503, "ethereum").category == "RPC_SERVER_ERROR"
        assert classify_rpc_http_status(200, "ethereum") is None

    def test_the_rpc_url_is_never_echoed_in_an_error(self):
        from backend.app.blockchain.chains import classify_rpc_http_status

        message = str(classify_rpc_http_status(401, "ethereum"))
        # No scheme, host, path or key: only the network NAME and the status code.
        assert "://" not in message
        assert "rpc.example" not in message
        assert "ethereum" in message
        assert "401" in message

    @pytest.mark.asyncio
    async def test_engine_translates_a_401_into_a_configuration_error(self, monkeypatch):
        from backend.app.transactions.engine import TransactionEngine, TransactionRejected

        engine = TransactionEngine()
        monkeypatch.setattr(engine, "rpc_url", lambda network: "https://rpc.example/v1")

        class _Response:
            status_code = 401
            text = "<html>unauthorized</html>"

            def json(self):
                raise ValueError("not json")

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                return _Response()

        monkeypatch.setattr("backend.app.transactions.engine.httpx.AsyncClient", _Client)

        with pytest.raises(TransactionRejected) as exc:
            await engine._rpc("ethereum", "eth_getBalance", ["0x0"])
        # The old behaviour raised "JSONDecodeError" and lost the cause entirely.
        assert exc.value.category == "RPC_AUTH"

    @pytest.mark.asyncio
    async def test_estimate_gas_never_quotes_zero_fees(self, monkeypatch):
        """A gas price we could not read must be an error, not a free transaction."""
        from backend.app.transactions.engine import TransactionEngine, TransactionIntent, TransactionRejected

        engine = TransactionEngine()

        async def fake_rpc(network, method, params):
            if method == "eth_estimateGas":
                return "0x5208"
            raise TransactionRejected("endpoint rejected the key", "RPC_AUTH")

        monkeypatch.setattr(engine, "_rpc", fake_rpc)
        intent = TransactionIntent(
            network="ethereum",
            chain_id=1,
            wallet_address="0x" + "11" * 20,
            to_address="0x" + "22" * 20,
            value_wei=1,
        )
        with pytest.raises(TransactionRejected) as exc:
            await engine.estimate_gas(intent)
        assert exc.value.category == "RPC_AUTH"


# ======================================================================
# 7. Fix E: source HTTP status handling (CoinGecko 403)
# ======================================================================
class TestSourceHttpStatus:
    def test_403_is_reported_as_a_refusal_not_a_transient_network_error(self):
        from backend.app.tools.web_tools import http_error_result

        result = http_error_result("https://coingecko.com/en/airdrops", 403)
        assert result.error_category == "SOURCE_FORBIDDEN"
        # Retrying an anti-bot refusal just burns the retry budget.
        assert result.retryable is False

    @pytest.mark.parametrize(
        "status,category,retryable",
        [
            (401, "SOURCE_FORBIDDEN", False),
            (403, "SOURCE_FORBIDDEN", False),
            (404, "SOURCE_NOT_FOUND", False),
            (429, "SOURCE_RATE_LIMITED", True),
            (503, "SOURCE_SERVER_ERROR", True),
        ],
    )
    def test_each_status_maps_to_a_specific_category(self, status, category, retryable):
        from backend.app.tools.web_tools import http_error_result

        result = http_error_result("https://example.test", status)
        assert result.error_category == category
        assert result.retryable is retryable

    @pytest.mark.asyncio
    async def test_discovery_records_a_source_it_could_not_read(self, monkeypatch):
        from backend.app.airdrops import discovery

        class _Forbidden:
            async def execute(self, params):
                from backend.app.tools.base import ToolResult

                return ToolResult.failure(
                    "HTTP 403 fetching https://coingecko.com/en/airdrops",
                    category="SOURCE_FORBIDDEN",
                )

        monkeypatch.setattr(discovery.registry, "get", lambda name: _Forbidden() if name == "site_search" else None)

        failures: List[Dict[str, Any]] = []
        campaigns = await discovery.discover_from_sources(
            ["https://coingecko.com/en/airdrops"], failures=failures
        )

        assert campaigns == []
        # A cycle that could not read a source used to look identical to one that
        # genuinely found nothing.
        assert len(failures) == 1
        assert failures[0]["category"] == "SOURCE_FORBIDDEN"
        assert failures[0]["url"] == "https://coingecko.com/en/airdrops"

    @pytest.mark.asyncio
    async def test_discovery_cycle_reports_partial_completion(self, monkeypatch):
        from backend.app.airdrops import discovery
        from backend.app.tools.base import ToolResult

        class _Forbidden:
            async def execute(self, params):
                return ToolResult.failure("HTTP 403", category="SOURCE_FORBIDDEN")

        monkeypatch.setattr(discovery.registry, "get", lambda name: _Forbidden() if name == "site_search" else None)

        cycle = await discovery.run_discovery_cycle({"community": ["https://coingecko.com/en/airdrops"]})

        assert cycle["status"] == "completed_with_source_failures"
        assert cycle["sources_failed"] == 1
        assert cycle["source_failures"]


# ======================================================================
# 8. Fix F: planner prerequisite parsing
# ======================================================================
class TestPrerequisiteParsing:
    @staticmethod
    def _task(**over):
        from datetime import datetime, timezone

        class _T:
            task_id = "AIR-2026-000001"
            status = "READY"
            priority = 5
            progress = 0.25
            retry_count = 0
            max_retries = 3
            # A real datetime, so a comparison against it is attempted and found
            # incomparable rather than short-circuited by None.
            deadline_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
            execution_started_at = None

        t = _T()
        for key, value in over.items():
            setattr(t, key, value)
        return t

    def test_numeric_comparison(self):
        from backend.app.planning.planner import Planner

        task = self._task(progress=0.5)
        assert Planner._check_expression(task, "progress > 0.2") is True
        assert Planner._check_expression(task, "progress > 0.9") is False
        assert Planner._check_expression(task, "retry_count == 0") is True
        assert Planner._check_expression(task, "retry_count != 3") is True

    def test_string_comparison(self):
        from backend.app.planning.planner import Planner

        task = self._task(status="READY")
        assert Planner._check_expression(task, "status == READY") is True
        assert Planner._check_expression(task, "status == FAILED") is False

    def test_a_datetime_field_raises_valueerror_not_typeerror(self):
        """float(datetime) raises TypeError, which used to escape the caller."""
        from datetime import datetime, timezone

        from backend.app.planning.planner import Planner

        task = self._task(deadline_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(ValueError):
            Planner._check_expression(task, "deadline_at > 2026-01-01")

    def test_unknown_field_is_unparseable_rather_than_permanently_false(self):
        """Returning False parked the step in WAITING forever."""
        from backend.app.planning.planner import Planner

        with pytest.raises(ValueError) as exc:
            Planner._check_expression(self._task(), "nonexistent_field == 1")
        assert "Unknown task field" in str(exc.value)

    @pytest.mark.asyncio
    async def test_unparseable_prerequisite_is_skipped_not_fatal(self):
        from backend.app.planning.planner import Planner, PlanStep, StepKind

        planner = Planner(db=None)
        step = PlanStep(
            index=0,
            description="x",
            kind=StepKind.RESEARCH,
            prerequisites=[
                "operator has approved the campaign",   # free text, no operator
                "some_invented_field > 5",              # unknown field
                "deadline_at > 2026-01-01",             # incomparable
                "priority <= 10",                       # valid
            ],
        )
        satisfied, unmet = await planner.evaluate_prerequisites(self._task(priority=5), step)
        # The document contract: an unparseable prerequisite must not block forever,
        # and must certainly not abort the execution loop.
        assert satisfied is True
        assert unmet is None

    @pytest.mark.asyncio
    async def test_a_genuinely_unmet_prerequisite_still_blocks(self):
        from backend.app.planning.planner import Planner, PlanStep, StepKind

        planner = Planner(db=None)
        step = PlanStep(
            index=0,
            description="x",
            kind=StepKind.RESEARCH,
            prerequisites=["retry_count >= 3"],
        )
        satisfied, unmet = await planner.evaluate_prerequisites(self._task(retry_count=0), step)
        assert satisfied is False
        assert unmet == "retry_count >= 3"


# ======================================================================
# 9. Fix G: evidence must match the declared verification method
# ======================================================================
class TestEvidenceVerification:
    @staticmethod
    def _controller():
        from backend.app.agent.controller import AgentController

        return AgentController(db=None)

    @staticmethod
    def _step(method: str):
        class _S:
            index = 0
            verification_method = method

        return _S()

    @staticmethod
    def _result(kind: str, authoritative: bool = True, data=None):
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            data=data or {},
            evidence={"authoritative": authoritative, "kind": kind},
        )

    @pytest.mark.asyncio
    async def test_simulation_does_not_satisfy_a_receipt_requirement(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("chain_receipt"), self._result("eth_call_simulation")
        )
        # Previously: any authoritative evidence satisfied any method.
        assert verified is False

    @pytest.mark.asyncio
    async def test_a_real_receipt_does_satisfy_a_receipt_requirement(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, count = await controller._verify_step(
            _T(), self._step("chain_receipt"), self._result("chain_receipt")
        )
        assert verified is True
        assert count == 1

    @pytest.mark.asyncio
    async def test_screenshot_does_not_satisfy_a_receipt_requirement(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("chain_receipt"), self._result("screenshot")
        )
        assert verified is False

    @pytest.mark.asyncio
    async def test_prepared_transfer_does_not_satisfy_a_receipt_requirement(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("chain_receipt"), self._result("prepared_transfer")
        )
        assert verified is False

    @pytest.mark.asyncio
    async def test_generic_tool_evidence_method_is_still_satisfied(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("tool_evidence"), self._result("page_observation")
        )
        assert verified is True

    @pytest.mark.asyncio
    async def test_unspecified_method_is_never_auto_verified(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("unspecified"), self._result("chain_receipt")
        )
        assert verified is False

    @pytest.mark.asyncio
    async def test_non_authoritative_evidence_verifies_nothing(self):
        controller = self._controller()

        class _T:
            task_id = "AIR-2026-000001"

        verified, _ = await controller._verify_step(
            _T(), self._step("chain_receipt"), self._result("chain_receipt", authoritative=False)
        )
        assert verified is False


# ======================================================================
# 10. Emergency stop blocks value-moving tools
# ======================================================================
class TestEmergencyStopGating:
    @pytest.mark.asyncio
    async def test_emergency_stop_refuses_the_broadcast_tool(self, bootstrapped):
        from backend.app.security.emergency_stop import emergency_stop

        registry = ToolRegistry()
        from backend.app.tools.transfer_tools import (
            ApproveTransferTool,
            PrepareNativeTransferTool,
            SubmitPreparedTransferTool,
        )

        registry.register_or_replace_many(
            [PrepareNativeTransferTool(), ApproveTransferTool(), SubmitPreparedTransferTool()]
        )
        registry.policy = AuthorizationPolicy(
            enabled_permissions=[
                "wallet.read", "blockchain.read", "transaction.approve",
                "wallet.sign", "transaction.submit",
            ],
            allowed_wallet_ids=["0x" + "11" * 20],
            allow_transaction_submission=True,
            allow_wallet_signing=True,
        )

        emergency_stop.activate("test", "pytest")

        try:
            submit = await registry.invoke(
                "submit_prepared_transfer",
                {"network": "ethereum", "approval_id": "nope", "approved_by": "operator"},
                task_id="AIR-2026-000001",
            )
            assert submit.status is ToolResultStatus.NOT_AUTHORIZED
            assert submit.error_category == "EMERGENCY_STOP"

            approve = await registry.invoke(
                "approve_transfer",
                {"network": "ethereum", "wallet_address": "0x" + "11" * 20,
                 "to_address": "0x" + "22" * 20, "approved_by": "operator"},
                task_id="AIR-2026-000001",
            )
            assert approve.status is ToolResultStatus.NOT_AUTHORIZED
        finally:
            emergency_stop.resume("test cleanup")


# ======================================================================
# 11. Approval binding
# ======================================================================
class TestApprovalBinding:
    def test_bindings_compare_on_the_fields_the_operator_was_shown(self):
        from backend.app.transactions.approvals import canonical_binding

        approved = canonical_binding(
            {"network": "ethereum", "wallet": "0xAB", "to": "0xCD", "value_wei": "1000", "data": "0x"}
        )
        # Case must not decide whether an approval matches: a checksummed address from
        # the decoder and a lowercase one from configuration are the same address.
        offered = canonical_binding(
            {"network": "ethereum", "wallet": "0xab", "to": "0xcd", "value_wei": "1000",
             "data": "0x", "gas_limit": "21000", "extra": "ignored"}
        )
        for key, value in approved.items():
            assert offered[key] == value

    def test_a_different_destination_does_not_match(self):
        from backend.app.transactions.approvals import canonical_binding

        approved = canonical_binding({"network": "ethereum", "to": "0xaa"})
        other = canonical_binding({"network": "ethereum", "to": "0xbb"})
        assert approved != other

    def test_a_different_amount_does_not_match(self):
        from backend.app.transactions.approvals import canonical_binding

        approved = canonical_binding({"network": "ethereum", "value_wei": "1000000000000000000"})
        other = canonical_binding({"network": "ethereum", "value_wei": "1000000000000000001"})
        assert approved != other

    @pytest.mark.asyncio
    async def test_the_service_refuses_a_non_human_approver(self):
        from backend.app.database.session import session_scope
        from backend.app.transactions.approvals import ApprovalError, ApprovalService

        binding = {
            "network": "ethereum", "chain_id": 1,
            "wallet": "0x" + "11" * 20, "to": "0x" + "22" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with session_scope() as db:
            service = ApprovalService(db)
            for identity in ("agent", "system", "llm", "assistant", "auto", ""):
                with pytest.raises(ApprovalError):
                    await service.request(
                        approved_by=identity, binding=binding, summary="test"
                    )

    @pytest.mark.asyncio
    async def test_an_approval_cannot_be_consumed_for_a_different_transaction(self):
        from backend.app.database.session import session_scope
        from backend.app.transactions.approvals import ApprovalService

        approved = {
            "network": "ethereum", "chain_id": 1,
            "wallet": "0x" + "11" * 20, "to": "0x" + "22" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with session_scope() as db:
            service = ApprovalService(db)
            row = await service.request(
                approved_by="operator@example.test", binding=approved, summary="test"
            )
            # The same fields: accepted.
            ok = await service.verify_and_consume(
                approval_id=row.approval_id,
                approved_by="operator@example.test",
                binding={**approved, "extra_field": "the engine computes more"},
            )
            assert ok is True

            # A different destination is not.
            row2 = await service.request(
                approved_by="operator@example.test", binding=approved, summary="test"
            )
            assert await service.verify_and_consume(
                approval_id=row2.approval_id,
                approved_by="operator@example.test",
                binding={**approved, "to": "0x" + "99" * 20},
            ) is False

    @pytest.mark.asyncio
    async def test_an_approval_can_only_be_consumed_once(self):
        from backend.app.database.session import session_scope
        from backend.app.transactions.approvals import ApprovalService

        approved = {
            "network": "ethereum", "chain_id": 1,
            "wallet": "0x" + "11" * 20, "to": "0x" + "22" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with session_scope() as db:
            service = ApprovalService(db)
            row = await service.request(
                approved_by="operator@example.test", binding=approved, summary="test"
            )
            assert await service.verify_and_consume(
                approval_id=row.approval_id,
                approved_by="operator@example.test",
                binding=approved,
            ) is True
            # Replaying the same approval must not authorise a second broadcast.
            assert await service.verify_and_consume(
                approval_id=row.approval_id,
                approved_by="operator@example.test",
                binding=approved,
            ) is False

    @pytest.mark.asyncio
    async def test_a_rejected_approval_can_never_be_used(self):
        from backend.app.database.session import session_scope
        from backend.app.transactions.approvals import ApprovalService

        approved = {
            "network": "ethereum", "chain_id": 1,
            "wallet": "0x" + "11" * 20, "to": "0x" + "22" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with session_scope() as db:
            service = ApprovalService(db)
            row = await service.request(
                approved_by="operator@example.test", binding=approved, summary="test"
            )
            await service.reject(row.approval_id, "changed my mind")
            usable, reason = await service.is_usable(row.approval_id)
            assert usable is False
            assert "rejected" in reason.lower()
            assert await service.verify_and_consume(
                approval_id=row.approval_id,
                approved_by="operator@example.test",
                binding=approved,
            ) is False

    @pytest.mark.asyncio
    async def test_an_expired_approval_is_refused(self):
        from backend.app.database.session import session_scope
        from backend.app.transactions.approvals import ApprovalService

        approved = {
            "network": "ethereum", "chain_id": 1,
            "wallet": "0x" + "11" * 20, "to": "0x" + "22" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with session_scope() as db:
            service = ApprovalService(db)
            row = await service.request(
                approved_by="operator@example.test", binding=approved, summary="test", ttl_minutes=1
            )
            from datetime import datetime, timedelta, timezone

            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.flush()

            usable, reason = await service.is_usable(row.approval_id)
            assert usable is False
            assert "expired" in reason.lower()


# ======================================================================
# 12. Task status report no longer raises on tasks with transactions
# ======================================================================
def test_status_report_field_names_match_the_model():
    """`value_usd`/`confirmations` do not exist on BlockchainTransaction."""
    from backend.app.models import BlockchainTransaction

    columns = {c.name for c in BlockchainTransaction.__table__.columns}
    assert "usd_value_transacted" in columns
    assert "confirmation_count" in columns
    assert "value_usd" not in columns
    assert "confirmations" not in columns