"""Test isolation for shared runtime state.

Two kinds of state leak out of a test process unless they are pinned:

* The Emergency Stop and the spend ledger are persisted to files so the API and
  worker processes share them. Both paths are pointed at a throwaway directory
  BEFORE the app (and its settings) is imported, and the files are wiped between
  tests.
* The database session factory. Tools, services and endpoints frequently open their
  own short-lived session (`database.session.session_scope`), so without isolation a
  test writes to the operator's real `airdropos.db`, and a test that creates a wallet
  or an approval leaves it there. Every test now gets its own in-memory database,
  built from the live models.

The in-memory engine pins one connection with StaticPool. Without that,
`sqlite:///:memory:` hands every new connection a *different* empty database, and a
tool opening its own session would query one the test never wrote to.
"""

import os
import tempfile
from pathlib import Path
from typing import Any

_STATE_DIR = Path(tempfile.mkdtemp(prefix="airdropos-test-state-"))
os.environ["EMERGENCY_STOP_STATE_FILE"] = str(_STATE_DIR / "emergency_stop.json")
os.environ["SPEND_LEDGER_PATH"] = str(_STATE_DIR / "spend_ledger.db")

import pytest  # noqa: E402


def _reset_shared_state() -> None:
    for name in ("emergency_stop.json", "emergency_stop.json.initialized", "spend_ledger.db"):
        try:
            (_STATE_DIR / name).unlink()
        except FileNotFoundError:
            pass
    # Deleting the file deliberately does NOT resume a running controller (a
    # removed state file must not be a way to lift a stop), so reset the
    # process-wide singleton explicitly between tests.
    from backend.app.security.emergency_stop import StopPolicy, emergency_stop

    emergency_stop._stopped = False
    emergency_stop._activated_at = None
    emergency_stop._activated_by = None
    emergency_stop._reason = None
    emergency_stop._policy = set(StopPolicy.ALLOWED)
    emergency_stop._history = []
    emergency_stop._file_sig = None
    emergency_stop._persist_error = None

    # The autonomous policy is process-global and cached. A test that switches
    # autonomy on must not leave it on for the next one.
    from backend.app.security.autonomous_policy import reset_policy_cache

    reset_policy_cache()


@pytest.fixture(autouse=True)
def _clean_shared_runtime_state():
    _reset_shared_state()
    yield
    _reset_shared_state()


@pytest.fixture(autouse=True)
async def _isolated_database(monkeypatch):
    """Give every test its own database, so none can touch the real one.

    Patches the session factory rather than the engine, because that is the single
    indirection every non-endpoint code path uses (`session_scope`). Endpoint tests
    that build their own engine override `get_db` themselves and are unaffected.
    """
    import backend.app.database.session as session_module
    from backend.app.database.session import Base
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        session_module,
        "AsyncSessionLocal",
        lambda *a, **k: AsyncSession(engine, expire_on_commit=False),
    )
    yield engine
    await engine.dispose()

@pytest.fixture(autouse=True)
def _reply_interpretation_never_reaches_a_real_llm(monkeypatch):
    """A test must never make a real LLM call from the Agent Inbox.

    Reply interpretation talks to the one fixed LLM. Unless a test installs its own double,
    that call fails immediately and deterministically, which is exactly what the router
    must survive: the task stays parked. Tests that need a model answer replace
    `backend.app.inbox.interpretation.llm` themselves.
    """
    from backend.app.inbox import interpretation
    from backend.app.llm.client import LLMError

    class _DisabledLLM:
        async def complete_json(self, messages, **kwargs):
            raise LLMError(
                "The LLM is disabled in tests.", category="TEST_DISABLED", retryable=False
            )

    monkeypatch.setattr(interpretation, "llm", _DisabledLLM())


# ---------------------------------------------------------------------------
# Telegram test doubles
# ---------------------------------------------------------------------------
# `backend/tests/fake_telegram.py` used to be dead code: nothing imported it, and the
# `client` fixture its module docstring referred to was never written. These two
# fixtures make it usable.
#
# Neither is autouse. A Telegram test opts in by naming `client`; a test that wants the
# fake plus the state isolation can name `telegram_state` on its own. Existing tests
# therefore see exactly the same process state as before.


class _AttributeSnapshot:
    """Restore one object's attributes (shallow-copied containers) after a test."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj
        self._saved = {
            key: (value.copy() if isinstance(value, (dict, list, set)) else value)
            for key, value in vars(obj).items()
        }

    def restore(self) -> None:
        vars(self._obj).clear()
        vars(self._obj).update(self._saved)


def reset_default_telegram_session(session: Any) -> None:
    """Return the default account session to a pristine, unconnected state.

    ``_client`` goes first and matters most: ``_TelegramUserSession._connect()`` only
    builds a client when it is ``None``, so a client left behind by another test would
    be reused -- and, with real credentials in ``.env``, that would mean a Telegram
    test silently talking to the operator's real account over the network.

    The pause/stop gates and pacing counters are cleared for the same reason: a session
    an earlier test paused would otherwise make every later tool call return
    NEEDS_USER_ACTION for no visible cause.
    """
    session._client = None
    session.resolved_chats = {}
    session.resolved_by_username = {}
    session.current_bot = None
    session.ever_used = False
    session.started_at = None
    session.last_action_at = None
    session.paused = False
    session.stopped = False
    session.waiting_reason = None
    session.last_error = None
    session.task_id = None
    session.task_title = None
    session._count = 0
    session._day = None
    session._last_action = 0.0


def reset_telegram_module_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Reset the process-global Telegram state the tool modules keep.

    All of this is module-level and would otherwise leak from one test into the next:

    * ``telegram_user_tools._session`` -- the default account's own bookkeeping, reset by
      :func:`reset_default_telegram_session` (see above: its client is the one that must
      never survive a test);
    * ``telegram_user_tools._extra_sessions`` -- one session per extra account;
    * ``settings.TELEGRAM_SESSION_PATH`` -- the session file the tools would open;
    * ``telegram_message_tools._LAST_INTERACTIONS`` -- bounded last-interaction memory;
    * ``miniapp_tools._launch_store`` -- bounded Mini App launch context.

    Pacing is neutralised too (``MIN_ACTION_DELAY_SECONDS`` -> 0): the production delay
    is 3s between actions, which would make a five-call test take fifteen seconds and
    assert nothing.
    """
    from backend.app.core.config import settings
    from backend.app.tools import miniapp_tools, telegram_message_tools, telegram_user_tools

    telegram_user_tools._extra_sessions.clear()
    telegram_message_tools._LAST_INTERACTIONS.clear()
    miniapp_tools._launch_store.clear()
    reset_default_telegram_session(telegram_user_tools._session)

    monkeypatch.setattr(settings, "TELEGRAM_SESSION_PATH", str(tmp_path / "telegram" / "agent"))
    monkeypatch.setattr(settings, "TELEGRAM_USER_MIN_ACTION_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(settings, "TELEGRAM_USER_DAILY_ACTION_LIMIT", 100_000)


@pytest.fixture(autouse=True)
def _telegram_never_reaches_the_network():
    """Stop any test that forgets ``client`` from talking to real Telegram.

    A Telegram tool that is invoked without the ``client`` fixture builds a REAL
    Telethon client from the credentials in the repo's ``.env``. During development
    this happened: a "unit" test resolved ``@airdropchannel`` to a genuine live
    channel and read it with the operator's own account. A test must never be able
    to reach the operator's real Telegram account by accident.

    So the client factory the tools use is replaced for EVERY test with one that
    hands back the deterministic fake. A test that wants a specific fake still
    asks for the ``client`` fixture, which patches over this (fixtures apply in
    dependency order, so the more specific patch wins). No existing test can break:
    a test that never constructs a client is unaffected, and one that does now gets
    a fake instead of the network.
    """
    from backend.app.tools import telegram_user_tools
    from backend.tests import fake_telegram

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        telegram_user_tools,
        "TelegramClient",
        lambda *a, **k: fake_telegram.FakeClient(),
    )
    yield
    monkeypatch.undo()


@pytest.fixture
def telegram_state(monkeypatch, tmp_path):
    """Isolate the Telegram modules' process-global state for one test.

    Opt in directly when a test manipulates Telegram state without needing the fake
    client (for example a test that only exercises ``callback_ref`` or the in-process
    last-interaction cache). ``client`` depends on it, so naming ``client`` is enough
    for the common case.
    """
    from backend.app.tools import telegram_user_tools

    # Snapshotted BEFORE the reset, so teardown hands the process back exactly what it
    # had on entry: these fixtures isolate, they do not sanitise other tests' setup.
    snapshot = _AttributeSnapshot(telegram_user_tools._session)
    reset_telegram_module_state(monkeypatch, tmp_path)
    yield
    snapshot.restore()


@pytest.fixture
def client(monkeypatch, tmp_path, telegram_state):
    """Install ``fake_telegram.FakeClient`` as the client every Telegram tool gets.

    The seam patched is the one production builds the client at --
    ``telegram_user_tools.TelegramClient`` -- so ``_TelegramUserSession._connect()``
    hands the fake to every tool without a single production change. ``telegram_state``
    clears the session's own ``_client`` first, which is what guarantees the patched
    factory is actually reached instead of a client another test left behind.

    The fake is also published as ``fake_telegram.ACTIVE_CLIENT``, which is where
    buttons record their presses.

    Usage: ``async def test_x(client):`` -- ``backend/tests/conftest.py`` sets
    ``asyncio_mode = "auto"`` via ``backend/pyproject.toml``, so no marker is needed.
    """
    from backend.app.tools import telegram_user_tools
    from backend.tests import fake_telegram

    fake = fake_telegram.FakeClient()
    monkeypatch.setattr(fake_telegram, "ACTIVE_CLIENT", fake)
    monkeypatch.setattr(telegram_user_tools, "TelegramClient", lambda *a, **k: fake)
    return fake
