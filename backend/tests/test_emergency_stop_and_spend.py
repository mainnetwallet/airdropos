"""Regression tests: shared/persistent Emergency Stop and persistent, atomic spend limits.

Confirmed bugs these guard against:

1. Emergency Stop lived in per-process memory. A stop engaged through the API
   was invisible to the worker process and was lost on restart.
2. The daily spend counter was a per-process float that reset on restart; the
   check and the update were separate steps (concurrent callers could all pass
   the check); nothing ever reserved or released spend.
3. `submit_raw` broadcast with no authorization, human approval, signer check
   or spend limit.
4. A TRANSACTION_SUBMISSION tool was gated only on the signing policy.

No test touches a real network: `_rpc` is replaced with a recording fake, and
transactions are signed offline with a throwaway key that funds nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap

import pytest
from eth_account import Account

from backend.app.core.config import settings
from backend.app.security.emergency_stop import (
    EmergencyStopController,
    EmergencyStopError,
    StopPolicy,
    emergency_stop,
)
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry
from backend.app.transactions.engine import (
    SubmissionAuthorization,
    TransactionEngine,
    TransactionRejected,
)
from backend.app.transactions.spend_ledger import SpendLedger

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_KEY = "0x" + "11" * 32
OTHER_KEY = "0x" + "22" * 32
WALLET = Account.from_key(TEST_KEY).address
OTHER_WALLET = Account.from_key(OTHER_KEY).address
FAKE_HASH = "0x" + "ab" * 32


def _raw_tx(key: str = TEST_KEY, nonce: int = 0) -> str:
    signed = Account.from_key(key).sign_transaction(
        {
            "nonce": nonce,
            "to": "0x" + "00" * 19 + "01",
            "value": 0,
            "gas": 21000,
            "gasPrice": 1,
            "chainId": 1,
        }
    )
    raw = signed.raw_transaction
    return "0x" + raw.hex().removeprefix("0x")


def _auth(**overrides) -> SubmissionAuthorization:
    base = dict(
        wallet_address=WALLET,
        authorized_addresses=frozenset({WALLET}),
        estimated_cost_usd=1.0,
        approved_by="operator-alice",
        approval_id="appr-1",
        human_approved=True,
        task_id="t-1",
        allowed_destinations=frozenset({"0x" + "00" * 19 + "01"}),
    )
    base.update(overrides)
    return SubmissionAuthorization(**base)


class FakeRpc:
    """Stands in for the network. Records every call; never opens a socket."""

    def __init__(self, result=FAKE_HASH, error: Exception | None = None, delay: float = 0.0):
        self.calls: list[tuple] = []
        self.result = result
        self.error = error
        self.delay = delay

    async def __call__(self, network, method, params):
        self.calls.append((network, method, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", 50.0)
    monkeypatch.setattr(settings, "MAX_DAILY_GAS_SPEND_USD", 25.0)


class _AcceptingVerifier:
    """Stands in for the server-side approval store in tests of the OTHER broadcast guards.
    Approval verification itself is covered in test_security_part3.py."""

    async def verify_and_consume(self, *, approval_id, approved_by, binding) -> bool:
        return True


@pytest.fixture
def eng(tmp_path, limits):
    return TransactionEngine(
        ledger=SpendLedger(str(tmp_path / "ledger.db")), approval_verifier=_AcceptingVerifier()
    )


@pytest.fixture
def rpc(eng, monkeypatch):
    fake = FakeRpc()
    monkeypatch.setattr(eng, "_rpc", fake)
    return fake


# ----------------------------------------------------------------------
# 1. Emergency Stop: shared across processes and persistent
# ----------------------------------------------------------------------
class TestEmergencyStopSharedAndPersistent:
    def test_stop_engaged_in_one_instance_is_seen_by_another(self, tmp_path):
        path = str(tmp_path / "stop.json")
        api, worker = EmergencyStopController(path), EmergencyStopController(path)
        assert worker.can_sign() and worker.can_execute_tasks()

        api.activate("incident", "operator")

        assert worker.is_stopped
        assert not worker.can_execute_tasks()
        assert not worker.can_submit_transactions()
        assert not worker.can_sign()
        assert not worker.can_run_jobs()

    def test_stop_survives_restart(self, tmp_path):
        path = str(tmp_path / "stop.json")
        EmergencyStopController(path).activate("incident", "operator")

        restarted = EmergencyStopController(path)

        assert restarted.is_stopped
        assert restarted.status()["reason"] == "incident"
        assert restarted.status()["activated_by"] == "operator"

    def test_resume_is_shared_and_survives_restart(self, tmp_path):
        path = str(tmp_path / "stop.json")
        api, worker = EmergencyStopController(path), EmergencyStopController(path)
        api.activate("incident", "operator")
        assert worker.is_stopped

        api.resume("operator")

        assert not worker.is_stopped
        assert not EmergencyStopController(path).is_stopped

    def test_policy_is_persisted(self, tmp_path):
        path = str(tmp_path / "stop.json")
        EmergencyStopController(path).activate(
            "narrow", "operator", policy={StopPolicy.HALT_TRANSACTION_SUBMISSION}
        )

        other = EmergencyStopController(path)

        assert not other.can_submit_transactions()
        assert other.can_sign()
        assert other.can_execute_tasks()

    def test_separate_python_process_sees_the_stop(self, tmp_path):
        """A real second process (as the worker is) honours a stop set here."""
        path = str(tmp_path / "stop.json")
        EmergencyStopController(path).activate("incident", "operator")
        code = textwrap.dedent(
            """
            from backend.app.security.emergency_stop import EmergencyStopController
            import sys
            c = EmergencyStopController(sys.argv[1])
            print("STOPPED" if (c.is_stopped and not c.can_execute_tasks()) else "RUNNING")
            """
        )
        out = subprocess.run(
            [sys.executable, "-c", code, path],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert out.stdout.strip() == "STOPPED", out.stderr

    def test_corrupt_state_file_fails_closed(self, tmp_path):
        path = tmp_path / "stop.json"
        path.write_text("{not json")
        controller = EmergencyStopController(str(path))
        assert controller.is_stopped
        assert not controller.can_sign()

    def test_deleting_the_file_does_not_lift_a_running_stop(self, tmp_path):
        path = tmp_path / "stop.json"
        controller = EmergencyStopController(str(path))
        controller.activate("incident", "operator")
        path.unlink()
        assert controller.is_stopped

    def test_environment_flag_seeds_persisted_state_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "EMERGENCY_STOP_ENABLED", True)
        path = str(tmp_path / "stop.json")

        first = EmergencyStopController(path)
        assert first.is_stopped

        first.resume("operator")
        # The operator's resume is the source of truth after restart; the env
        # flag only seeds a fresh install.
        assert not EmergencyStopController(path).is_stopped

    def test_resume_that_cannot_be_persisted_keeps_the_stop(self, tmp_path, monkeypatch):
        controller = EmergencyStopController(str(tmp_path / "stop.json"))
        controller.activate("incident", "operator")
        monkeypatch.setattr(controller, "_persist", lambda: (_ for _ in ()).throw(OSError("disk full")))

        with pytest.raises(OSError):
            controller.resume("operator")

        assert controller.is_stopped

    def test_assert_running_uses_exact_operation_match(self, tmp_path):
        controller = EmergencyStopController(str(tmp_path / "stop.json"))
        controller.activate("incident", "operator")
        # "sign" is a substring of "halt_wallet_signing"; it must not be treated as that operation.
        with pytest.raises(EmergencyStopError) as generic:
            controller.assert_running("sign")
        with pytest.raises(EmergencyStopError) as exact:
            controller.assert_running(StopPolicy.HALT_WALLET_SIGNING)
        assert "cannot be recalled" not in generic.value.reason
        assert "cannot be recalled" in exact.value.reason


# ----------------------------------------------------------------------
# 2. Emergency Stop is enforced before execution / signing / broadcast
# ----------------------------------------------------------------------
class _SubmitTool(BaseTool):
    name = "submit_tool"
    description = "pretend submission"
    required_permissions: list = ["tx.submit"]
    side_effect = SideEffect.TRANSACTION_SUBMISSION
    input_schema = {"type": "object", "properties": {}, "required": []}
    executed = 0

    async def execute(self, params):
        type(self).executed += 1
        return ToolResult.success(data={})


class TestEmergencyStopEnforcement:
    @pytest.mark.asyncio
    async def test_submission_tool_blocked_when_only_submission_is_halted(self):
        """The registry used to gate submission tools on can_sign() alone."""
        _SubmitTool.executed = 0
        registry = ToolRegistry()
        registry.register(_SubmitTool())
        emergency_stop.activate("narrow", "test", policy={StopPolicy.HALT_TRANSACTION_SUBMISSION})
        assert emergency_stop.can_sign()  # signing itself is still allowed

        result = await registry.invoke("submit_tool", {}, policy=AuthorizationPolicy(enabled_permissions=["tx.submit"]))

        assert result.error_category == "EMERGENCY_STOP"
        assert _SubmitTool.executed == 0

    @pytest.mark.asyncio
    async def test_submission_tool_runs_when_not_stopped(self):
        _SubmitTool.executed = 0
        registry = ToolRegistry()
        registry.register(_SubmitTool())
        result = await registry.invoke("submit_tool", {}, policy=AuthorizationPolicy(enabled_permissions=["tx.submit"]))
        assert result.ok
        assert _SubmitTool.executed == 1

    @pytest.mark.asyncio
    async def test_stop_from_another_instance_blocks_submit_raw(self, eng, rpc):
        # Engage via a second controller on the shared file, as the API process would.
        other = EmergencyStopController(os.environ["EMERGENCY_STOP_STATE_FILE"])
        other.activate("incident", "api-process")

        with pytest.raises(EmergencyStopError):
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth())

        assert rpc.calls == []

    def test_vault_signing_refused_when_stopped_by_another_instance(self):
        from backend.app.wallets.vault import SigningDenied, SigningRequest, KeyVault

        EmergencyStopController(os.environ["EMERGENCY_STOP_STATE_FILE"]).activate("incident", "api-process")
        vault = KeyVault("0" * 64)
        request = SigningRequest(wallet_address=WALLET, chain_id=1, message="hello")
        with pytest.raises(SigningDenied):
            vault.validate_request(request, {WALLET})


# ----------------------------------------------------------------------
# 3. Spend limits: persistent, atomic, and not reset by restart
# ----------------------------------------------------------------------
class TestSpendLedger:
    @pytest.mark.asyncio
    async def test_spend_survives_restart(self, tmp_path, limits):
        path = str(tmp_path / "ledger.db")
        first = TransactionEngine(ledger=SpendLedger(path))
        await first.reserve_spend(20.0)

        restarted = TransactionEngine(ledger=SpendLedger(path))

        assert restarted.daily_spend_usd == pytest.approx(20.0)
        with pytest.raises(TransactionRejected) as exc:
            await restarted.reserve_spend(10.0)
        assert exc.value.category == "DAILY_LIMIT_EXCEEDED"

    @pytest.mark.asyncio
    async def test_concurrent_reservations_cannot_exceed_the_daily_limit(self, eng):
        results = await asyncio.gather(
            *[eng.reserve_spend(10.0) for _ in range(20)], return_exceptions=True
        )
        granted = [r for r in results if isinstance(r, str)]
        refused = [r for r in results if isinstance(r, TransactionRejected)]

        assert len(granted) == 2  # 2 x $10 fits in $25; a third would not
        assert len(refused) == 18
        assert eng.daily_spend_usd == pytest.approx(20.0)

    def test_separate_processes_cannot_jointly_exceed_the_daily_limit(self, tmp_path):
        ledger_path = str(tmp_path / "shared.db")
        code = textwrap.dedent(
            """
            import sys
            from backend.app.transactions.spend_ledger import SpendLedger, SpendLimitExceeded
            ledger = SpendLedger(sys.argv[1])
            try:
                ledger.reserve(10.0, per_transaction_limit=50.0, daily_limit=25.0)
                print("OK")
            except SpendLimitExceeded:
                print("DENIED")
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, ledger_path],
                cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(8)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs.append(out.strip())

        assert outputs.count("OK") == 2
        assert outputs.count("DENIED") == 6
        assert SpendLedger(ledger_path).spent_last_24h() == pytest.approx(20.0)

    def test_window_is_rolling_24_hours(self, tmp_path):
        now = [1_000_000.0]
        ledger = SpendLedger(str(tmp_path / "l.db"), clock=lambda: now[0])
        ledger.reserve(20.0, per_transaction_limit=50, daily_limit=25)
        assert ledger.spent_last_24h() == pytest.approx(20.0)

        now[0] += 24 * 3600 + 1
        assert ledger.spent_last_24h() == 0.0
        ledger.reserve(20.0, per_transaction_limit=50, daily_limit=25)  # allowed again

    @pytest.mark.asyncio
    async def test_per_transaction_limit_enforced(self, eng):
        with pytest.raises(TransactionRejected) as exc:
            await eng.reserve_spend(50.01)
        assert exc.value.category == "LIMIT_EXCEEDED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [-1.0, float("nan")])
    async def test_invalid_cost_is_refused(self, eng, bad):
        with pytest.raises(TransactionRejected):
            await eng.reserve_spend(bad)

    @pytest.mark.asyncio
    async def test_release_returns_a_reservation_but_never_committed_spend(self, eng):
        reserved = await eng.reserve_spend(10.0)
        assert await eng.release_spend(reserved) is True
        assert eng.daily_spend_usd == 0.0

        committed = await eng.reserve_spend(10.0)
        await eng.commit_spend(committed)
        assert await eng.release_spend(committed) is False
        assert eng.daily_spend_usd == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_commit_can_correct_the_amount(self, eng):
        rid = await eng.reserve_spend(10.0)
        await eng.commit_spend(rid, 4.0)
        assert eng.daily_spend_usd == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_record_spend_is_durable_and_synchronous(self, eng):
        eng.record_spend(7.5)  # used to be a fire-and-forget task that needed a running loop
        assert eng.daily_spend_usd == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_check_spend_limits_sees_persisted_spend(self, eng):
        await eng.reserve_spend(20.0)
        with pytest.raises(TransactionRejected):
            await eng.check_spend_limits(10.0)
        await eng.check_spend_limits(5.0)


# ----------------------------------------------------------------------
# 4. Failed / cancelled submissions: reservation accounting
# ----------------------------------------------------------------------
class TestReservationLifecycle:
    @pytest.mark.asyncio
    async def test_successful_broadcast_commits_spend(self, eng, rpc):
        tx_hash = await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=3.0))
        assert tx_hash == FAKE_HASH
        assert eng.daily_spend_usd == pytest.approx(3.0)
        assert [c[1] for c in rpc.calls] == ["eth_sendRawTransaction"]

    @pytest.mark.asyncio
    async def test_definite_rpc_rejection_releases_the_reservation(self, eng, monkeypatch):
        monkeypatch.setattr(
            eng, "_rpc", FakeRpc(error=TransactionRejected("nonce too low", "RPC_ERROR"))
        )
        with pytest.raises(TransactionRejected):
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=10.0))
        assert eng.daily_spend_usd == 0.0

    @pytest.mark.asyncio
    async def test_ambiguous_rpc_failure_keeps_counting(self, eng, monkeypatch):
        """A timeout may still have reached the node; releasing would under-count."""
        monkeypatch.setattr(
            eng, "_rpc", FakeRpc(error=TransactionRejected("timed out", "RPC_FAILURE"))
        )
        with pytest.raises(TransactionRejected):
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=10.0))
        assert eng.daily_spend_usd == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_missing_hash_keeps_counting(self, eng, monkeypatch):
        monkeypatch.setattr(eng, "_rpc", FakeRpc(result=None))
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=10.0))
        assert exc.value.category == "NO_TX_HASH"
        assert eng.daily_spend_usd == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_cancelled_broadcast_keeps_counting(self, eng, monkeypatch):
        monkeypatch.setattr(eng, "_rpc", FakeRpc(delay=5))
        task = asyncio.create_task(
            eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=10.0))
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert eng.daily_spend_usd == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_stop_engaged_during_reservation_releases_and_does_not_broadcast(self, eng, rpc, monkeypatch):
        original = eng.reserve_spend

        async def reserve_then_stop(*args, **kwargs):
            rid = await original(*args, **kwargs)
            emergency_stop.activate("raced", "test")
            return rid

        monkeypatch.setattr(eng, "reserve_spend", reserve_then_stop)
        with pytest.raises(EmergencyStopError):
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=10.0))

        assert rpc.calls == []
        assert eng.daily_spend_usd == 0.0


# ----------------------------------------------------------------------
# 5. submit_raw cannot bypass authorization, approval, limits or the stop
# ----------------------------------------------------------------------
class TestSubmitRawGuards:
    @pytest.mark.asyncio
    async def test_authorization_is_required(self, eng, rpc):
        with pytest.raises(TypeError):
            await eng.submit_raw("ethereum", _raw_tx())  # type: ignore[call-arg]
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(), authorization=None)  # type: ignore[arg-type]
        assert exc.value.category == "NOT_AUTHORIZED"
        assert rpc.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"human_approved": False},
            {"approved_by": "agent"},
            {"approved_by": "  SYSTEM "},
            {"approved_by": ""},
            {"approval_id": ""},
        ],
    )
    async def test_human_approval_is_required(self, eng, rpc, overrides):
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(**overrides))
        assert exc.value.category == "HUMAN_APPROVAL_REQUIRED"
        assert rpc.calls == []
        assert eng.daily_spend_usd == 0.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authorized", [frozenset(), frozenset({OTHER_WALLET})])
    async def test_wallet_must_be_authorized(self, eng, rpc, authorized):
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(authorized_addresses=authorized))
        assert exc.value.category == "NOT_AUTHORIZED"
        assert rpc.calls == []

    @pytest.mark.asyncio
    async def test_signer_must_match_the_authorized_wallet(self, eng, rpc):
        """An authorised wallet name cannot be used to broadcast someone else's signed tx."""
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw(
                "ethereum",
                _raw_tx(key=OTHER_KEY),
                authorization=_auth(authorized_addresses=frozenset({WALLET, OTHER_WALLET})),
            )
        assert exc.value.category == "NOT_AUTHORIZED"
        assert rpc.calls == []

    @pytest.mark.asyncio
    async def test_undecodable_transaction_is_refused(self, eng, rpc):
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", "0xdeadbeef", authorization=_auth())
        assert exc.value.category == "UNDECODABLE_TX"
        assert rpc.calls == []
        assert eng.daily_spend_usd == 0.0

    @pytest.mark.asyncio
    async def test_per_transaction_limit_applies(self, eng, rpc):
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth(estimated_cost_usd=50.01))
        assert exc.value.category == "LIMIT_EXCEEDED"
        assert rpc.calls == []

    @pytest.mark.asyncio
    async def test_daily_limit_applies(self, eng, rpc):
        await eng.submit_raw("ethereum", _raw_tx(nonce=0), authorization=_auth(estimated_cost_usd=20.0))
        with pytest.raises(TransactionRejected) as exc:
            await eng.submit_raw("ethereum", _raw_tx(nonce=1), authorization=_auth(estimated_cost_usd=10.0))
        assert exc.value.category == "DAILY_LIMIT_EXCEEDED"
        assert len(rpc.calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_submissions_cannot_bypass_the_daily_limit(self, eng, rpc):
        results = await asyncio.gather(
            *[
                eng.submit_raw("ethereum", _raw_tx(nonce=n), authorization=_auth(estimated_cost_usd=10.0))
                for n in range(10)
            ],
            return_exceptions=True,
        )
        assert sum(1 for r in results if r == FAKE_HASH) == 2
        assert len(rpc.calls) == 2
        assert eng.daily_spend_usd == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_emergency_stop_blocks_before_any_other_work(self, eng, rpc):
        emergency_stop.activate("incident", "test")
        with pytest.raises(EmergencyStopError):
            await eng.submit_raw("ethereum", _raw_tx(), authorization=_auth())
        assert rpc.calls == []
        assert eng.daily_spend_usd == 0.0

    def test_no_tool_or_api_exposes_submit_raw_without_authorization(self):
        """submit_raw's only callers must be explicit; nothing in app/ calls it today."""
        import pathlib

        offenders = []
        for path in pathlib.Path(REPO_ROOT, "backend", "app").rglob("*.py"):
            if path.name == "engine.py":
                continue
            if ".submit_raw(" in path.read_text(encoding="utf-8"):
                offenders.append(str(path))
        assert offenders == []
