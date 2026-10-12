"""Regression tests for the Part 3 security fixes.

Covers: production configuration guards and vault-key validation, browser SSRF gaps (IPv6
smuggling, WebSockets), deployment files, Alembic configuration, and raw-transaction broadcast
(server-verified approval and destination validation).

Nothing here touches the network, a database, a real browser or a blockchain.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from pathlib import Path

import pytest
from eth_account import Account

from backend.app.browser import controller as ctl
from backend.app.core.config import Settings
from backend.app.security.emergency_stop import emergency_stop  # noqa: F401  (import order)
from backend.app.transactions.engine import (
    SubmissionAuthorization,
    TransactionEngine,
    TransactionRejected,
    decode_raw_transaction,
)
from backend.app.transactions.spend_ledger import SpendLedger
from backend.app.wallets.vault import KeyVault, VaultError

REPO = Path(__file__).resolve().parents[2]

GOOD_SECRET = "Zq8mT4" * 8  # 48 chars, not the default, not a placeholder
GOOD_VAULT = "9f" * 32
HASH = "pbkdf2$600000$salt$hash"


def make_settings(**kw) -> Settings:
    base = dict(
        ENVIRONMENT="production",
        SECRET_KEY=GOOD_SECRET,
        WALLET_VAULT_MASTER_KEY=GOOD_VAULT,
        OPERATOR_PASSWORD_HASH=HASH,
        DATABASE_URL="postgresql+asyncpg://airdropos:Xk29fj@db:5432/airdropos_db",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


# ----------------------------------------------------------------------------
# 1. Configuration and vault
# ----------------------------------------------------------------------------
class TestProductionConfig:
    def test_valid_production_settings_load(self):
        assert make_settings().ENVIRONMENT == "production"

    @pytest.mark.parametrize(
        "override, fragment",
        [
            ({"SECRET_KEY": "0123456789abcdef" * 4}, "SECRET_KEY"),
            ({"SECRET_KEY": "replace-with-a-secure-random-secret-key-min-32-chars"}, "SECRET_KEY"),
            ({"SECRET_KEY": "short"}, "SECRET_KEY"),
            ({"WALLET_VAULT_MASTER_KEY": "0123456789abcdef" * 4}, "WALLET_VAULT_MASTER_KEY"),
            ({"WALLET_VAULT_MASTER_KEY": "0x" + "0123456789abcdef" * 4}, "WALLET_VAULT_MASTER_KEY"),
            ({"OPERATOR_PASSWORD_HASH": None}, "OPERATOR_PASSWORD_HASH"),
            ({"OPERATOR_PASSWORD_HASH": "  "}, "OPERATOR_PASSWORD_HASH"),
            ({"AUTH_ENABLED": False}, "AUTH_ENABLED"),
            ({"DATABASE_URL": "postgresql+asyncpg://airdropos:airdropos_password@db/x"}, "DATABASE_URL"),
        ],
    )
    def test_production_rejects_insecure_values(self, override, fragment):
        with pytest.raises(ValueError, match=fragment):
            make_settings(**override)

    def test_development_still_allows_defaults(self):
        s = Settings(_env_file=None, ENVIRONMENT="development")
        assert s.ENVIRONMENT == "development"

    @pytest.mark.parametrize("raw", ["Production", " PRODUCTION ", "prod", "PROD"])
    def test_environment_spellings_cannot_switch_the_guard_off(self, raw):
        with pytest.raises(ValueError, match="Refusing to start"):
            Settings(_env_file=None, ENVIRONMENT=raw)

    def test_environment_is_normalised(self):
        assert make_settings(ENVIRONMENT="Prod").ENVIRONMENT == "production"


class TestVaultKeyFormat:
    @pytest.mark.parametrize(
        "bad",
        ["g" * 64, "ab" * 31, "ab" * 33, "ab" * 31 + "zz", "", "0x" + "ab" * 31, "ab" * 32 + "x"],
    )
    def test_config_rejects_bad_vault_keys_without_echoing_them(self, bad):
        with pytest.raises(ValueError) as exc:
            Settings(_env_file=None, WALLET_VAULT_MASTER_KEY=bad)
        assert "64 hexadecimal" in str(exc.value)
        if bad:
            assert bad not in str(exc.value)

    def test_32_char_key_is_no_longer_accepted(self):
        # The old validator accepted >= 32 characters, then KeyVault() failed at import time.
        with pytest.raises(ValueError):
            Settings(_env_file=None, WALLET_VAULT_MASTER_KEY="ab" * 16)

    def test_config_accepts_0x_prefixed_key(self):
        assert Settings(_env_file=None, WALLET_VAULT_MASTER_KEY="0x" + "ab" * 32)

    @pytest.mark.parametrize("bad", ["g" * 64, "ab" * 31, "ab" * 33, "zz" * 32])
    def test_vault_rejects_non_hex_or_wrong_length(self, bad):
        with pytest.raises(VaultError, match="64 hexadecimal"):
            KeyVault(bad)

    def test_vault_round_trip_with_valid_key(self):
        v = KeyVault("0x" + GOOD_VAULT)
        assert v.decrypt(*v.encrypt("secret")) == "secret"

    def test_vault_refuses_the_dev_key_in_production(self, monkeypatch):
        monkeypatch.setattr("backend.app.wallets.vault.settings.ENVIRONMENT", "production")
        with pytest.raises(VaultError, match="development key"):
            KeyVault("0123456789abcdef" * 4)
        assert KeyVault(GOOD_VAULT)  # a real key still works

    def test_dev_key_still_works_outside_production(self, monkeypatch):
        monkeypatch.setattr("backend.app.wallets.vault.settings.ENVIRONMENT", "development")
        assert KeyVault("0123456789abcdef" * 4)

    def test_env_example_does_not_ship_the_dev_vault_key(self):
        text = (REPO / ".env.example").read_text()
        active = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        assert not any("0123456789abcdef" in l for l in active)
        assert not any(l.startswith(("SECRET_KEY=", "WALLET_VAULT_MASTER_KEY=")) for l in active)


# ----------------------------------------------------------------------------
# 2. Browser SSRF
# ----------------------------------------------------------------------------
class TestIpClassification:
    @pytest.mark.parametrize(
        "addr",
        [
            "::127.0.0.1",          # IPv4-compatible loopback (Python calls it global)
            "::10.0.0.1",
            "::169.254.169.254",
            "fec0::1",              # site-local
            "2002:7f00:1::1",       # 6to4 wrapping 127.0.0.1
            "2002:a9fe:a9fe::1",    # 6to4 wrapping the metadata address
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",      # NAT64 wrapping loopback
            "::1",
            "fd00:ec2::254",
        ],
    )
    def test_smuggled_internal_addresses_are_blocked(self, addr):
        assert ctl._ip_block_reason(ipaddress.ip_address(addr))

    @pytest.mark.parametrize("addr", ["::127.0.0.1", "2002:7f00:1::1", "fec0::1"])
    def test_still_blocked_when_private_network_is_allowed(self, addr, monkeypatch):
        monkeypatch.setattr(ctl.settings, "BROWSER_ALLOW_PRIVATE_NETWORK", True)
        assert ctl._ip_block_reason(ipaddress.ip_address(addr))

    @pytest.mark.parametrize("addr", ["93.184.216.34", "2606:4700::1111"])
    def test_public_addresses_remain_reachable(self, addr):
        assert ctl._ip_block_reason(ipaddress.ip_address(addr)) is None

    def test_literal_bracketed_urls_are_blocked(self):
        assert ctl.url_block_reason("http://[::127.0.0.1]/")
        assert ctl.url_block_reason("http://[fec0::1]/")


class FakeWs:
    def __init__(self, url: str):
        self.url = url
        self.connected = False
        self.closed_with = None

    def connect_to_server(self):
        self.connected = True

    async def close(self, *, code=None, reason=None):
        self.closed_with = (code, reason)


class TestWebSocketGuard:
    def run(self, url: str) -> FakeWs:
        ws = FakeWs(url)
        asyncio.run(ctl.BrowserController()._make_ws_guard("k")(ws))
        return ws

    @pytest.mark.parametrize(
        "url",
        [
            "ws://127.0.0.1:8765/",
            "wss://127.0.0.1/",
            "ws://localhost/x",
            "ws://169.254.169.254/",
            "ws://[::127.0.0.1]/",
            "ws://10.0.0.5/",
            "ws://redis.internal/",
            "file:///etc/passwd",
            "http://93.184.216.34/",   # not a ws scheme
        ],
    )
    def test_internal_or_non_ws_targets_are_closed_not_connected(self, url):
        ws = self.run(url)
        assert not ws.connected
        assert ws.closed_with == (1008, "blocked by policy")

    @pytest.mark.parametrize("url", ["ws://93.184.216.34/socket", "wss://93.184.216.34:8443/s"])
    def test_public_targets_are_passed_through(self, url):
        ws = self.run(url)
        assert ws.connected and ws.closed_with is None

    def test_unresolvable_hostname_fails_closed(self, monkeypatch):
        async def boom(url):
            raise RuntimeError("resolver down")

        monkeypatch.setattr(ctl, "destination_block_reason", boom)
        ws = self.run("wss://example.test/")
        assert not ws.connected and ws.closed_with


class FakeContext:
    def __init__(self, with_ws: bool):
        self.routes, self.ws_routes, self.closed = [], [], False
        if with_ws:
            self.route_web_socket = self._route_ws

    def set_default_timeout(self, *_):
        pass

    set_default_navigation_timeout = set_default_timeout

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def _route_ws(self, pattern, handler):
        self.ws_routes.append((pattern, handler))

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **_):
        return self.context

    def is_connected(self):
        return True


def _controller_with(context) -> ctl.BrowserController:
    c = ctl.BrowserController()
    c._healthy = True
    c._browser = FakeBrowser(context)
    return c


class TestContextRegistersBothGuards:
    def test_http_and_websocket_guards_are_both_installed(self):
        ctx = FakeContext(with_ws=True)
        got = asyncio.run(_controller_with(ctx)._ensure_context("ws-guard-ok", persistent=False))
        assert got is ctx
        assert ctx.routes and ctx.ws_routes

    def test_context_is_refused_when_websockets_cannot_be_intercepted(self):
        ctx = FakeContext(with_ws=False)  # an old Playwright without route_web_socket
        got = asyncio.run(_controller_with(ctx)._ensure_context("ws-guard-missing", persistent=False))
        assert got is None, "must fail closed rather than run with an unguarded WebSocket path"
        assert ctx.closed, "the half-configured context must be closed"

    def test_playwright_floor_supports_route_web_socket(self):
        for f in ("backend/requirements.txt", "backend/pyproject.toml"):
            m = re.search(r"playwright>=(\d+)\.(\d+)", (REPO / f).read_text())
            assert m and (int(m[1]), int(m[2])) >= (1, 48), f


# ----------------------------------------------------------------------------
# 3. Deployment files
# ----------------------------------------------------------------------------
class TestDeploymentFiles:
    compose = (REPO / "docker-compose.yml").read_text()

    def test_no_hardcoded_database_password(self):
        assert "airdropos_password" not in self.compose
        assert self.compose.count("${POSTGRES_PASSWORD:?") >= 3  # postgres, backend, worker

    def test_default_password_exists_only_as_the_dev_constant(self):
        offenders = []
        for path in REPO.rglob("*"):
            if not path.is_file() or any(p in path.parts for p in (".git", "node_modules", "tests", "docs")):
                continue
            if path.suffix in {".py", ".yml", ".yaml", ".conf", ".example", ".ini", ".json", ""} or path.name == "Dockerfile":
                try:
                    if "airdropos_password" in path.read_text(errors="ignore"):
                        offenders.append(path.relative_to(REPO).as_posix())
                except OSError:
                    pass
        assert offenders == ["backend/app/core/config.py"], offenders

    def test_backend_does_not_run_with_autoreload(self):
        assert "--reload" not in self.compose

    def test_frontend_bind_address_is_operator_configurable(self):
        assert '"${FRONTEND_BIND:-0.0.0.0}:3000:80"' in self.compose

    def test_internal_services_are_not_published_on_all_interfaces(self):
        for m in re.finditer(r'^\s*-\s*"([^"]*:\d+:\d+)"', self.compose, re.M):
            spec = m.group(1)
            assert spec.startswith(("127.0.0.1:", "${FRONTEND_BIND")), spec
        assert "8765" not in re.findall(r'^\s*-\s*"[^"]*"', self.compose, re.M).__str__()

    def test_nginx_hardening(self):
        conf = (REPO / "frontend" / "nginx.conf").read_text()
        for needle in ("server_tokens off", "X-Content-Type-Options", "X-Frame-Options", "$connection_upgrade"):
            assert needle in conf
        assert "Connection 'upgrade'" not in conf

    def test_run_py_generates_a_database_password_that_compose_and_the_app_share(self, tmp_path):
        import importlib.util

        spec = importlib.util.spec_from_file_location("airdropos_run", REPO / "run.py")
        run_py = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_py)
        env = tmp_path / ".env"
        run_py.create_env_file(env)
        vals = run_py.read_env(env)
        pw = vals["POSTGRES_PASSWORD"]
        assert len(pw) == 48 and pw != "airdropos_password"
        assert vals["DATABASE_URL"] == f"postgresql+asyncpg://airdropos:{pw}@localhost:5432/airdropos_db"
        assert len(vals["SECRET_KEY"]) == 64 and len(vals["WALLET_VAULT_MASTER_KEY"]) == 64
        # And the generated file is a valid production-shaped configuration apart from the login hash.
        Settings(_env_file=None, ENVIRONMENT="production", OPERATOR_PASSWORD_HASH=HASH,
                 SECRET_KEY=vals["SECRET_KEY"], WALLET_VAULT_MASTER_KEY=vals["WALLET_VAULT_MASTER_KEY"],
                 DATABASE_URL=vals["DATABASE_URL"])

    @pytest.mark.parametrize("f", [".dockerignore", "frontend/.dockerignore"])
    def test_docker_contexts_exclude_env_files(self, f):
        lines = (REPO / f).read_text().splitlines()
        assert ".env" in lines


# ----------------------------------------------------------------------------
# 4. Alembic
# ----------------------------------------------------------------------------
class TestAlembicConfig:
    ini = (REPO / "backend" / "alembic.ini").read_text()

    def test_paths_do_not_depend_on_the_working_directory(self):
        assert re.search(r"^script_location\s*=\s*%\(here\)s/app/migrations\s*$", self.ini, re.M)
        assert re.search(r"^prepend_sys_path\s*=\s*%\(here\)s/\.\.\s*$", self.ini, re.M)

    def test_script_directory_loads_from_any_cwd_with_a_single_head(self, tmp_path, monkeypatch):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        monkeypatch.chdir(tmp_path)
        cfg = Config(str(REPO / "backend" / "alembic.ini"))
        script = ScriptDirectory.from_config(cfg)
        assert Path(script.dir) == REPO / "backend" / "app" / "migrations"
        assert script.get_heads() == ["2_reconcile_schema_with_models"]

    def test_stale_duplicate_directory_is_marked_and_not_referenced(self):
        stale = REPO / "backend" / "migrations"
        assert (stale / "DEPRECATED.md").exists()
        assert "backend/migrations" not in self.ini and "migrations/versions" not in self.ini.replace(
            "app/migrations", ""
        )
        assert (stale / "versions" / "1_initial_schema.py").exists(), "migrations must not be deleted"


# ----------------------------------------------------------------------------
# 5. Raw-transaction broadcast: server-verified approval + destination validation
# ----------------------------------------------------------------------------
KEY = "0x" + "11" * 32
WALLET = Account.from_key(KEY).address
DEST = "0x" + "00" * 19 + "01"
FAKE_TX_HASH = "0x" + "ab" * 32


def sign(*, to=DEST, chain_id=1, data="0x", value=0, nonce=0, tx_type="legacy"):
    tx = {"nonce": nonce, "gas": 100_000, "value": value, "data": data}
    if to is not None:
        tx["to"] = to
    if chain_id is not None:
        tx["chainId"] = chain_id
    if tx_type == "legacy":
        tx["gasPrice"] = 1
    else:
        tx.update(maxFeePerGas=2, maxPriorityFeePerGas=1, type=2)
    raw = Account.from_key(KEY).sign_transaction(tx).raw_transaction
    return "0x" + raw.hex().removeprefix("0x")


def auth(**kw) -> SubmissionAuthorization:
    base = dict(
        wallet_address=WALLET,
        authorized_addresses=frozenset({WALLET}),
        estimated_cost_usd=1.0,
        approved_by="operator-alice",
        approval_id="appr-1",
        human_approved=True,
        task_id="t",
        allowed_destinations=frozenset({DEST}),
    )
    base.update(kw)
    return SubmissionAuthorization(**base)


class RecordingVerifier:
    """A one-shot approval store: approves a known id once, for the exact binding it was issued for."""

    def __init__(self, approve=True, error=None):
        self.approve, self.error, self.calls, self.used = approve, error, [], set()

    async def verify_and_consume(self, *, approval_id, approved_by, binding):
        self.calls.append(dict(approval_id=approval_id, approved_by=approved_by, binding=dict(binding)))
        if self.error:
            raise self.error
        if not self.approve or approval_id in self.used:
            return False
        self.used.add(approval_id)
        return True


class Rpc:
    def __init__(self):
        self.calls = []

    async def __call__(self, network, method, params):
        self.calls.append((network, method, params))
        return FAKE_TX_HASH


def make_engine(tmp_path, monkeypatch, verifier):
    monkeypatch.setattr("backend.app.transactions.engine.settings.MAX_TRANSACTION_VALUE_USD", 50.0)
    monkeypatch.setattr("backend.app.transactions.engine.settings.MAX_DAILY_GAS_SPEND_USD", 25.0)
    eng = TransactionEngine(ledger=SpendLedger(str(tmp_path / "l.db")), approval_verifier=verifier)
    rpc = Rpc()
    monkeypatch.setattr(eng, "_rpc", rpc)
    return eng, rpc


def run(coro):
    return asyncio.run(coro)


class TestServerVerifiedApproval:
    def test_caller_supplied_flag_alone_never_broadcasts(self, tmp_path, monkeypatch):
        eng, rpc = make_engine(tmp_path, monkeypatch, verifier=None)
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", sign(), authorization=auth(human_approved=True)))
        assert e.value.category == "HUMAN_APPROVAL_REQUIRED"
        assert rpc.calls == [] and eng.daily_spend_usd == 0

    def test_unverified_approval_is_refused_before_any_spend_or_rpc(self, tmp_path, monkeypatch):
        v = RecordingVerifier(approve=False)
        eng, rpc = make_engine(tmp_path, monkeypatch, v)
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", sign(), authorization=auth()))
        assert e.value.category == "HUMAN_APPROVAL_REQUIRED"
        assert rpc.calls == [] and eng.daily_spend_usd == 0

    def test_verifier_errors_fail_closed(self, tmp_path, monkeypatch):
        eng, rpc = make_engine(tmp_path, monkeypatch, RecordingVerifier(error=RuntimeError("store down")))
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", sign(), authorization=auth()))
        assert e.value.category == "HUMAN_APPROVAL_REQUIRED" and rpc.calls == []

    @pytest.mark.parametrize("truthy", [1, "yes", object(), {"ok": True}])
    def test_only_an_explicit_true_counts(self, tmp_path, monkeypatch, truthy):
        class Loose:
            async def verify_and_consume(self, **_):
                return truthy

        eng, rpc = make_engine(tmp_path, monkeypatch, Loose())
        with pytest.raises(TransactionRejected):
            run(eng.submit_raw("ethereum", sign(), authorization=auth()))
        assert rpc.calls == []

    def test_verified_approval_broadcasts_and_binds_the_exact_transaction(self, tmp_path, monkeypatch):
        v = RecordingVerifier()
        eng, rpc = make_engine(tmp_path, monkeypatch, v)
        raw = sign(value=7)
        assert run(eng.submit_raw("ethereum", raw, authorization=auth())) == FAKE_TX_HASH
        (call,) = v.calls
        b = call["binding"]
        from eth_utils import keccak

        assert call["approval_id"] == "appr-1" and call["approved_by"] == "operator-alice"
        assert b["tx_hash"] == "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        assert (b["network"], b["chain_id"], b["to"], b["value_wei"], b["wallet"]) == (
            "ethereum", "1", DEST, "7", WALLET.lower()
        )
        assert [c[1] for c in rpc.calls] == ["eth_sendRawTransaction"]

    def test_an_approval_authorises_one_broadcast_only(self, tmp_path, monkeypatch):
        eng, rpc = make_engine(tmp_path, monkeypatch, RecordingVerifier())
        run(eng.submit_raw("ethereum", sign(nonce=0), authorization=auth()))
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", sign(nonce=1), authorization=auth()))
        assert e.value.category == "HUMAN_APPROVAL_REQUIRED"
        assert len(rpc.calls) == 1

    @pytest.mark.parametrize("who", ["agent", "SYSTEM", " llm ", "", "claude"])
    def test_non_human_approvers_are_still_refused(self, tmp_path, monkeypatch, who):
        v = RecordingVerifier()
        eng, rpc = make_engine(tmp_path, monkeypatch, v)
        with pytest.raises(TransactionRejected):
            run(eng.submit_raw("ethereum", sign(), authorization=auth(approved_by=who)))
        assert v.calls == [] and rpc.calls == []

    def test_default_engine_has_no_verifier_so_broadcast_is_disabled(self):
        assert TransactionEngine.__init__.__defaults__[1] is None


class TestRawDestinationValidation:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        v = RecordingVerifier()
        eng, rpc = make_engine(tmp_path, monkeypatch, v)
        return eng, rpc, v

    def reject(self, env, raw, expected, **auth_kw):
        eng, rpc, v = env
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", raw, authorization=auth(**auth_kw)))
        assert e.value.category == expected, str(e.value)
        assert rpc.calls == [], "nothing may be broadcast"
        assert v.calls == [], "a rejected destination must not burn the approval"
        assert eng.daily_spend_usd == 0

    def test_destination_must_be_in_the_approved_set(self, env):
        self.reject(env, sign(to="0x" + "00" * 19 + "02"), "DESTINATION_NOT_ALLOWED")

    @pytest.mark.parametrize("empty", [frozenset(), None])
    def test_empty_destination_set_means_nothing_is_allowed(self, env, empty):
        self.reject(env, sign(), "DESTINATION_NOT_ALLOWED", allowed_destinations=empty)

    def test_destination_match_is_case_insensitive(self, tmp_path, monkeypatch):
        eng, rpc = make_engine(tmp_path, monkeypatch, RecordingVerifier())
        checksummed = Account.from_key("0x" + "33" * 32).address  # mixed case
        run(eng.submit_raw(
            "ethereum", sign(to=checksummed),
            authorization=auth(allowed_destinations=frozenset({checksummed.lower()})),
        ))
        assert len(rpc.calls) == 1

    def test_contract_creation_is_refused(self, env):
        self.reject(env, sign(to=None, data="0x6000"), "DESTINATION_NOT_ALLOWED")

    def test_chain_id_must_match_the_network(self, env):
        self.reject(env, sign(chain_id=137), "CHAIN_ID_MISMATCH")

    def test_unprotected_pre_eip155_transactions_are_refused(self, env):
        self.reject(env, sign(chain_id=None), "CHAIN_ID_MISMATCH")

    def test_denylisted_destination_is_refused_even_if_listed_as_allowed(self, env):
        zero = "0x" + "00" * 20
        self.reject(env, sign(to=zero), "DESTINATION_DENIED", allowed_destinations=frozenset({zero}))

    @pytest.mark.parametrize("selector", ["f2fde38b", "715018a6", "8bda01ab"])
    def test_restricted_selectors_are_refused_like_the_vault_does(self, env, selector):
        self.reject(env, sign(data="0x" + selector + "00" * 32), "RESTRICTED_OPERATION")

    def test_oversized_calldata_is_refused(self, env):
        self.reject(env, sign(data="0x" + "00" * 100_001), "RESTRICTED_OPERATION")

    def test_unknown_network_is_refused(self, env):
        eng, rpc, v = env
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("not-a-chain", sign(), authorization=auth()))
        assert e.value.category == "UNKNOWN_NETWORK" and rpc.calls == [] and v.calls == []

    @pytest.mark.parametrize("raw", ["0xdeadbeef", "0x", "zz", "0x03" + "00" * 40, "0x04c0", "0xc0"])
    def test_undecodable_or_unsupported_payloads_are_refused(self, env, raw):
        self.reject(env, raw, "UNDECODABLE_TX")

    def test_eip1559_transactions_decode_and_broadcast(self, tmp_path, monkeypatch):
        eng, rpc = make_engine(tmp_path, monkeypatch, RecordingVerifier())
        run(eng.submit_raw("ethereum", sign(tx_type="1559", value=5), authorization=auth()))
        assert len(rpc.calls) == 1

    def test_decoder_reads_both_formats(self):
        legacy = decode_raw_transaction(sign(value=3, data="0xdead"))
        typed = decode_raw_transaction(sign(tx_type="1559", value=3, data="0xdead"))
        for d, kind in ((legacy, 0), (typed, 2)):
            assert (d.tx_type, d.chain_id, d.to, d.value, d.data) == (kind, 1, DEST, 3, bytes.fromhex("dead"))
        assert decode_raw_transaction(sign(to=None, data="0x6000")).to is None

    def test_signer_check_still_precedes_destination_check(self, env):
        eng, rpc, v = env
        other = Account.from_key("0x" + "22" * 32).address
        with pytest.raises(TransactionRejected) as e:
            run(eng.submit_raw("ethereum", sign(), authorization=auth(wallet_address=other, authorized_addresses=frozenset({other}))))
        assert e.value.category == "NOT_AUTHORIZED" and rpc.calls == []
