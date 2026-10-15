"""A Chrome Web Store install blocker must be verified against the profile, not left UNKNOWN."""

from types import SimpleNamespace

import pytest

from backend.app.inbox import verification
from backend.app.inbox.service import (
    EXTENSION_INSTALL_REQUIRED,
    MACHINE_VERIFIABLE,
    UNKNOWN_REASON,
    classify_reason,
)

STORE_TEXT = (
    "The Chrome Web Store listing is open in your browser. Chrome requires you to click "
    "'Add to Chrome' and accept the permissions prompt yourself."
)
EXT_ID = "a" * 32


def _request(reason=EXTENSION_INSTALL_REQUIRED):
    return SimpleNamespace(
        reason_code=reason,
        problem_summary=STORE_TEXT,
        blocking_step="Step 0 (browser_extensions): Open Psy Wallet Chrome Web Store page",
        integration_ref=None,
        request_id="IR-1",
    )


def _plan(**params):
    step = SimpleNamespace(tool="browser_extensions", parameters={"action": "web_store", **params})
    return SimpleNamespace(steps=[step])


@pytest.fixture
def patched(monkeypatch):
    from backend.app.browser import profiles
    from backend.app.planning import planner
    from backend.app.tools import browser_tools

    state = {"plan": _plan(store_url=f"https://chromewebstore.google.com/detail/psy-wallet/{EXT_ID}"),
             "installed": []}

    async def load_plan_spec(self, task):
        return state["plan"]

    async def extension_status(key):
        return {"ok": True, "profile_dir": "C:/profiles/default", "installed": state["installed"]}

    monkeypatch.setattr(planner.Planner, "load_plan_spec", load_plan_spec)
    monkeypatch.setattr(browser_tools.controller, "extension_status", extension_status)
    monkeypatch.setattr(profiles, "resolve_session_key", lambda k: k or "default")
    return state


def test_store_text_is_classified_and_machine_verifiable():
    assert classify_reason(STORE_TEXT) == EXTENSION_INSTALL_REQUIRED
    assert EXTENSION_INSTALL_REQUIRED in MACHINE_VERIFIABLE


@pytest.mark.asyncio
async def test_not_installed_stays_blocked(patched):
    result = await verification._verify_extension_installed(None, None, _request())
    assert not result.ok and result.checkable
    assert "AirdropOS" in result.question


@pytest.mark.asyncio
async def test_installed_by_store_id_passes(patched):
    patched["installed"] = [{"id": EXT_ID, "name": "Psy Wallet"}]
    result = await verification._verify_extension_installed(None, None, _request())
    assert result.ok and result.method == "extension_installed"


@pytest.mark.asyncio
async def test_a_similarly_named_other_extension_does_not_pass(patched):
    patched["installed"] = [{"id": "b" * 32, "name": "Psy Wallet"}]
    result = await verification._verify_extension_installed(None, None, _request())
    assert not result.ok


@pytest.mark.asyncio
async def test_name_query_is_used_when_no_store_id(patched):
    patched["plan"] = _plan(query="Psy Wallet")
    patched["installed"] = [{"id": "c" * 32, "name": "Psy Wallet", "description": ""}]
    result = await verification._verify_extension_installed(None, None, _request())
    assert result.ok


@pytest.mark.asyncio
async def test_no_store_step_is_unchecked_not_passed(patched):
    patched["plan"] = SimpleNamespace(steps=[])
    result = await verification._verify_extension_installed(None, None, _request())
    assert not result.ok and not result.checkable


@pytest.mark.asyncio
async def test_old_unknown_request_is_rederived_and_checked(patched):
    patched["installed"] = [{"id": EXT_ID, "name": "Psy Wallet"}]
    result = await verification.verify(None, None, _request(reason=UNKNOWN_REASON))
    assert result.ok and result.method == "extension_installed"


@pytest.mark.asyncio
async def test_other_unknown_requests_are_not_rederived():
    req = _request(reason=UNKNOWN_REASON)
    req.problem_summary = "something odd happened"
    req.blocking_step = "Step 2"
    result = await verification.verify(None, None, req)
    assert not result.ok and not result.checkable and result.method == verification.NOT_CHECKED
