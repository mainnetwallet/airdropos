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
