"""Test isolation for shared, file-backed runtime state.

The Emergency Stop and the spend ledger are persisted to files so that the API
and worker processes share them. Tests must never read or write the real
`./data` files, so both paths are pointed at a throwaway directory BEFORE the
app (and its settings) is imported, and the files are wiped between tests.
"""

import os
import tempfile
from pathlib import Path

_STATE_DIR = Path(tempfile.mkdtemp(prefix="airdropos-test-state-"))
os.environ["EMERGENCY_STOP_STATE_FILE"] = str(_STATE_DIR / "emergency_stop.json")
os.environ["SPEND_LEDGER_PATH"] = str(_STATE_DIR / "spend_ledger.db")

import pytest  # noqa: E402


def _reset_shared_state() -> None:
    for name in ("emergency_stop.json", "spend_ledger.db"):
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


@pytest.fixture(autouse=True)
def _clean_shared_runtime_state():
    _reset_shared_state()
    yield
    _reset_shared_state()
