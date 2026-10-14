"""Regression tests for Telegram automation (Phase 3 / 10: Tests A, B, C).

All Telegram interaction is driven by `fake_telegram.FakeClient`. No production
account, real airdrop or wallet is contacted, and nothing here needs credentials.
"""

from __future__ import annotations

import pytest

from backend.app.tools import telegram_user_tools as tg
from backend.app.tools.miniapp_tools import (
    classify_launch,
    looks_webview_dependent,
    safe_url,
)
from backend.app.tools.telegram_user_tools import (
    TelegramCheckMembershipTool,
    TelegramClickButtonTool,
    TelegramJoinChatTool,
    TelegramListDialogsTool,
    TelegramReadMessagesTool,
    parse_target,
)
from tests.fake_telegram import FakeButton


@pytest.fixture
def client(monkeypatch):
    """Install a fake Telethon client into the shared session and neutralise pacing."""
    from tests import fake_telegram
    from tests.fake_telegram import FakeClient

    fake = FakeClient()
    monkeypatch.setattr(fake_telegram, "ACTIVE_CLIENT", fake)

    tg._session._client = fake
    tg._session.resolved_chats.clear()
    tg._session.resolved_by_username.clear()
    tg._session.current_bot = None
    tg._session.stopped = False
    tg._session.paused = False

    async def _fake_connect():
        return fake

    async def _noop_pace():
        return None

    async def _fake_wait(client_, entity, after_id, timeout=10.0):
        """Deterministic replacement for the polling wait."""
        chat_id = getattr(entity, "id", entity)
        return [m for m in fake.messages.get(chat_id, []) if m.id > after_id and not m.out]

    monkeypatch.setattr(tg._session, "_connect", _fake_connect)
    monkeypatch.setattr(tg._session, "_pace", _noop_pace)
    monkeypatch.setattr(tg, "_wait_for_replies", _fake_wait)
    return fake


# ======================================================================
# Target parsing
# ======================================================================
@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("@airdropchat", "username", "airdropchat"),
        ("airdropchat", "username", "airdropchat"),
        ("https://t.me/airdropchat", "username", "airdropchat"),
        ("https://t.me/+AbCdEf123456", "invite", "AbCdEf123456"),
        ("https://t.me/joinchat/AbCdEf123456", "invite", "AbCdEf123456"),
    ],
)
def test_parse_target(raw, kind, value):
    parsed = parse_target(raw)
    assert parsed["kind"] == kind
    assert parsed["value"] == value


def test_start_param_is_extracted_from_link():
    assert parse_target("https://t.me/MyBot?start=ref999")["start_param"] == "ref999"


def test_invalid_target_is_rejected():
    with pytest.raises(ValueError):
        parse_target("!!! not a telegram target !!!")


def test_secret_like_outgoing_text_is_detected():
    assert tg.looks_like_secret("0x" + "a" * 64)
    assert tg.looks_like_secret("my private key is ...")
    # A 40-hex public address is not a key and must be sendable.
    assert not tg.looks_like_secret("0x742d35Cc6634C0532925a3b844Bc454e4438f44e")


# ======================================================================
# TEST A - Telegram join, then CONTINUE
# ======================================================================
async def test_join_verifies_membership_and_returns_usable_identity(client):
    """A join must prove membership, not merely report that the call returned."""
    result = await TelegramJoinChatTool().execute({"chat": "@airdropchat"})

    assert result.status.value == "success"
    assert result.data["joined"] is True
    assert result.data["membership_verified"] is True
    assert result.data["already_member"] is False
    # The resolved identity is what lets the NEXT step read this chat.
    assert result.data["chat_id"] == client.group.id
    assert result.data["username"] == "airdropchat"
    assert result.evidence["kind"] == "membership_verified"
    assert client.group.id in client.joined


async def test_join_is_idempotent_and_detects_existing_membership(client):
    """Joining a chat the account is already in must not re-join or fail."""
    client.joined.add(client.group.id)

    first = await TelegramJoinChatTool().execute({"chat": "@airdropchat"})
    second = await TelegramJoinChatTool().execute({"chat": "@airdropchat"})

    assert first.data["already_member"] is True
    assert second.data["already_member"] is True
    assert second.data["membership_verified"] is True


async def test_join_that_cannot_be_verified_is_reported_as_failure(client):
    """`success: true` from the API is not evidence. If membership cannot be proven,
    the step must NOT be reported as done."""
    import backend.app.tools.telegram_user_tools as module

    original = module._membership_state

    async def _undeterminable(client_, entity):
        return None, "unknown"

    module._membership_state = _undeterminable
    try:
        result = await TelegramJoinChatTool().execute({"chat": "@airdropchat"})
    finally:
        module._membership_state = original

    assert result.status.value != "success"
    assert result.error_category == "VERIFICATION_FAILED"
    assert result.evidence is None


async def test_workflow_continues_after_join_back_to_the_bot(client):
    """THE reported defect: join a group, then RETURN to the bot and keep going.

    This asserts the full continuation chain end to end:
    open bot -> read -> join -> verify -> RETURN to bot -> next action.
    """
    from backend.app.tools.telegram_continuation_tools import TelegramReturnToBotTool

    # 1. Open and start the bot.
    opened = await tg.TelegramOpenBotTool().execute({"bot": "@AirdropOfficialBot"})
    assert opened.status.value == "success"
    assert opened.evidence["kind"] == "bot_started"
    # The bot is now anchored, so "return" needs no name.
    assert tg._session.current_bot == "AirdropOfficialBot"

    # 2. Join the required group and verify membership.
    joined = await TelegramJoinChatTool().execute({"chat": "@airdropchat"})
    assert joined.data["membership_verified"] is True

    # 3. Return to the originating bot WITHOUT re-sending /start.
    returned = await TelegramReturnToBotTool().execute({})
    assert returned.status.value == "success"
    assert returned.data["returned_to_bot"] is True
    assert returned.data["bot"] == "AirdropOfficialBot"
    # Buttons are surfaced so the next step can pick a supported one.
    assert "Check Eligibility" in returned.data["available_buttons"]
    # No extra /start was sent: only the original one exists.
    starts = [m for m in client.messages[client.bot.id] if m.message.startswith("/start")]
    assert len(starts) == 1

    # 4. Continue with the next eligible action.
    clicked = await TelegramClickButtonTool().execute(
        {"chat": "@AirdropOfficialBot", "message_id": 1, "button_text": "Check Eligibility"}
    )
    assert clicked.status.value == "success", (
        f"click failed: {clicked.error!r} ({clicked.error_category})"
    )
    assert clicked.evidence["kind"] == "button_clicked"


async def test_return_to_bot_without_a_known_bot_asks_for_one(client):
    from backend.app.tools.telegram_continuation_tools import TelegramReturnToBotTool

    result = await TelegramReturnToBotTool().execute({})
    assert result.status.value == "failure"
    assert result.error_category == "INVALID_INPUT"


# ======================================================================
# TEST B - Private invitation link
# ======================================================================
async def test_private_invite_resolves_entity_and_is_usable_afterwards(client):
    """An invite hash is not a history target. The chat must be resolved, and that
    resolved identity must be usable by the NEXT step."""
    joined = await TelegramJoinChatTool().execute({"chat": "https://t.me/+AbCdEf123456"})

    assert joined.status.value == "success", f"{joined.error!r} ({joined.error_category})"
    assert joined.data["membership_verified"] is True
    assert joined.data["chat_id"] == client.private_group.id
    # A private group has no public username; that must not break resolution.
    assert joined.data["username"] is None

    # The resolved chat is addressable by id, so its history CAN now be read.
    read = await TelegramReadMessagesTool().execute({"chat": str(client.private_group.id)})
    assert read.status.value == "success"
    assert read.data["count"] == 1
    assert read.evidence["kind"] == "message_observed"


async def test_reading_a_private_invite_link_directly_is_refused(client):
    """The invite link must never be treated as a message-history target."""
    result = await TelegramReadMessagesTool().execute({"chat": "https://t.me/+AbCdEf123456"})

    assert result.status.value == "failure"
    assert result.error_category == "INVALID_INPUT"
    assert "invitation link" in (result.error or "").lower()


async def test_expired_invite_fails_clearly(client, monkeypatch):
    """An expired or revoked invitation is reported, never retried forever."""
    from backend.app.tools import telegram_user_tools as module

    async def _expired(client_, invite_hash):
        raise type("InviteHashExpiredError", (Exception,), {})("expired")

    monkeypatch.setattr(module, "_join_by_invite", _expired)
    result = await TelegramJoinChatTool().execute({"chat": "https://t.me/+AbCdEf123456"})

    assert result.status.value == "failure"
    assert result.error_category == "NOT_FOUND"
    assert result.retryable is False
    assert "expired" in (result.error or "").lower()


async def test_invite_requiring_approval_asks_the_operator(client):
    """A join request awaiting admin approval is a pause, not a failure.

    Exercised directly against `_join_by_invite`, because that is where the
    classification lives.
    """
    outcome = await _classify_invite_error("InviteRequestSentError")

    assert outcome.status.value == "needs_user_action"
    assert "approv" in (outcome.error or "").lower()


@pytest.mark.parametrize(
    "error_name,expected_status,expected_category",
    [
        ("InviteHashExpiredError", "failure", "NOT_FOUND"),
        ("InviteHashInvalidError", "failure", "NOT_FOUND"),
        ("ChannelPrivateError", "failure", "FORBIDDEN"),
        ("UserBannedInChannelError", "failure", "FORBIDDEN"),
        ("InviteRequestSentError", "needs_user_action", "HUMAN_INTERVENTION_REQUIRED"),
    ],
)
async def test_invite_failures_are_classified_distinctly(
    client, error_name, expected_status, expected_category
):
    """Expired, revoked, forbidden and approval-required are different outcomes.

    Treating them all the same is what caused either endless retries of a dead link or
    a silent abandonment of an actionable one.
    """
    outcome = await _classify_invite_error(error_name)

    assert outcome.status.value == expected_status
    assert outcome.error_category == expected_category
    if expected_status == "failure":
        assert outcome.retryable is False


async def _classify_invite_error(name: str):
    """Drive `_join_by_invite` with a Telethon error of the given type name."""
    from backend.app.tools.telegram_user_tools import _join_by_invite

    error_type = type(name, (Exception,), {})

    async def _raising_client(request):
        raise error_type(name)

    try:
        resolved, outcome = await _join_by_invite(_raising_client, "AbCdEf123456")
    except Exception as exc:  # pragma: no cover - should be classified, not raised
        raise AssertionError(f"{name} was not classified: {type(exc).__name__}") from exc
    assert resolved == {} and outcome is not None, f"{name} produced no outcome"
    return outcome


# ======================================================================
# TEST C - Button interaction
# ======================================================================
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("callback", "callback"),
        ("text", "text"),
        ("url", "url"),
        ("webapp", "webapp"),
        ("payment", "payment"),
        ("RequestPhone", "request_phone"),
    ],
)
def test_button_kind_classification(kind, expected):
    """Button variants must be classified correctly.

    Regression guard for a real defect: classification used Telethon class names that no
    longer exist, so EVERY callback button was labelled "unsupported" and refused. A
    workflow could not press the first "Check eligibility" button.
    """
    from tests.fake_telegram import FakeButton

    assert tg._button_kind(FakeButton("Press me", kind)) == expected


async def test_callback_button_is_clicked_and_observed(client):
    result = await TelegramClickButtonTool().execute(
        {"chat": "@AirdropOfficialBot", "message_id": 1, "button_text": "Check Eligibility"}
    )

    assert result.status.value == "success"
    assert result.data["clicked"] is True
    assert result.data["button_kind"] == "callback"
    # The bot's answer is reported so the next step can observe the result.
    assert result.data["answer"] == "Accepted"
    assert result.evidence["kind"] == "button_clicked"
    assert client.last_clicked == "Check Eligibility"


async def test_url_button_is_reported_not_opened(client):
    """A link button is reported to the agent, never opened automatically."""
    result = await TelegramClickButtonTool().execute(
        {"chat": "@AirdropOfficialBot", "message_id": 1, "button_text": "Join Channel"}
    )

    assert result.status.value == "needs_user_action"
    assert result.data["button_kind"] == "url"
    assert client.last_clicked is None


async def test_miniapp_button_is_routed_to_the_miniapp_path(client):
    """A Mini App button must be neither refused outright nor clicked blindly."""
    result = await TelegramClickButtonTool().execute(
        {"chat": "@AirdropOfficialBot", "message_id": 1, "button_text": "Open Mini App"}
    )

    assert result.status.value == "needs_user_action"
    assert result.data["button_kind"] == "webapp"
    assert "telegram_detect_miniapp" in result.data["next_tools"]
    assert client.last_clicked is None


async def test_unknown_button_reports_what_is_available(client):
    result = await TelegramClickButtonTool().execute(
        {"chat": "@AirdropOfficialBot", "message_id": 1, "button_text": "No Such Button"}
    )

    assert result.status.value == "failure"
    assert result.error_category == "NOT_FOUND"
    # The error lists what IS available so the agent can choose correctly.
    assert "Check Eligibility" in (result.error or "")


async def test_buttons_are_inspected_before_any_click(client):
    """The agent must see button kinds before deciding which one to press."""
    read = await TelegramReadMessagesTool().execute({"chat": "@AirdropOfficialBot"})

    kinds = {
        b["text"]: b["kind"] for m in read.data["messages"] for b in (m.get("buttons") or [])
    }
    assert kinds["Join Channel"] == "url"
    assert kinds["Open Mini App"] == "webapp"
    assert kinds["Check Eligibility"] == "callback"



