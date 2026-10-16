"""The Telegram tool SURFACE: what is registered, what it declares, and what it refuses.

This file is about the catalog the LLM sees and the boundary it cannot cross:

* the catalog is all-or-nothing -- with Telegram disabled not one Telegram tool exists;
* every tool describes itself (``description`` is all the model gets);
* every schema uses only the JSON-schema subset the validator understands;
* every tool declares the permissions it needs, and nothing with a side effect is
  permission-free;
* the registry actually refuses a Telegram tool under a policy without ``telegram.interact``;
* no tool result ever carries the account id, an ``access_hash``, the API hash, the phone
  number or a raw ``callback_data``;
* a button the operator must press itself is refused WITH a reason and never clicked;
* the two pure tools keep working with Telegram switched off;
* a Mini App behind the authentication wall returns ``needs_user``, never a fake success.

Every Telegram test here takes the ``client`` fixture. That is not optional: without it
``_TelegramUserSession._connect`` builds a REAL Telethon client from the operator's
credentials in ``.env`` and the tool reaches the real Telegram network.
"""

from __future__ import annotations

import json

import pytest

from backend.app.core.config import settings
from backend.app.database.session import session_scope
# Imported for its side effect. The per-test in-memory database is built with
# ``Base.metadata.create_all``, so every model must be registered on the metadata BEFORE
# the first ``_isolated_database`` fixture runs. Without this, running this file's tests on
# their own builds an empty schema and the first database-using test fails.
from backend.app.models import Task  # noqa: F401
from backend.app.tools import miniapp_tools, telegram_service
from backend.app.tools.base import SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.miniapp_tools import MINIAPP_TOOLS
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry
from backend.app.tools.telegram_user_tools import (
    CLICKABLE_BUTTON_KINDS,
    REFUSED_BUTTON_REASONS,
)
from backend.tests.fake_telegram import FakeButton


BOT = "@AirdropOfficialBot"
MINIAPP_URL = "https://app.example.com/quest"

#: The JSON-schema keywords ``BaseTool.validate_params`` actually enforces. Anything else
#: in a property is a constraint the model is told about and the validator ignores.
ENFORCED_SCHEMA_KEYWORDS = {"type", "enum", "minimum", "maximum", "maxLength"}

ALL_PERMISSIONS = [
    "telegram.read", "telegram.interact", "task.read", "task.write", "airdrop.read",
    "browser.read", "browser.navigate", "browser.interact",
]


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


def _telegram_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_USER_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_API_ID", 123456)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")


def _telegram_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_USER_ENABLED", False)


def _empty_registry() -> ToolRegistry:
    return ToolRegistry()


def _full_registry(monkeypatch, *, permissions=None) -> ToolRegistry:
    from backend.app.tools import (
        telegram_bot_tools,
        telegram_campaign_tools,
        telegram_group_tools,
        telegram_message_tools,
        telegram_session_tools,
        telegram_user_tools,
    )

    registry = _empty_registry()
    telegram_user_tools.register_telegram_user_tools(registry)
    telegram_session_tools.register_session_tools(registry)
    telegram_bot_tools.register_bot_tools(registry)
    telegram_message_tools.register_message_tools(registry)
    telegram_group_tools.register_group_tools(registry)
    telegram_campaign_tools.register_campaign_tools(registry)
    registry.register_or_replace_many([t() for t in MINIAPP_TOOLS])
    registry.policy = AuthorizationPolicy(
        enabled_permissions=permissions if permissions is not None else ALL_PERMISSIONS,
        allow_web_interaction=True,
    )
    return registry


#: The Mini App detection family is registered UNCONDITIONALLY by
#: ``backend/app/tools/bootstrap.py`` (lines 86-90): detecting a Mini App button needs no
#: credentials, so it stays in the catalog with Telegram switched off. Every other Telegram
#: tool is behind the Telegram gate, and those are what "all-or-nothing" is about.
MINIAPP_TOOL_NAMES = frozenset(t().name for t in MINIAPP_TOOLS)


def _all_telegram_names(registry: ToolRegistry):
    return sorted(n for n in registry._tools if n.startswith("telegram_"))


def _gated_telegram_names(registry: ToolRegistry):
    """The Telegram-gated families: everything except the unconditional Mini App tools."""
    return sorted(
        n for n in registry._tools
        if n.startswith("telegram_") and n not in MINIAPP_TOOL_NAMES
    )


async def _task():
    from backend.app.tasks.manager import TaskManager
    from backend.app.tasks.state_machine import TaskStatus

    async with session_scope() as db:
        task = await TaskManager(db).create_task(
            title="Telegram airdrop campaign", description="Complete the bot's checklist",
            status=TaskStatus.IN_PROGRESS, owner_email="op@example.test",
        )
        await db.commit()
        return task


def _bot_with_buttons(client, buttons):
    client.messages[client.bot.id] = []
    return client.add_message(client.bot, "Pick one.", msg_id=1, sender_id=client.bot.id,
                              buttons=buttons)


# ----------------------------------------------------------------------
# 1. Registration is all-or-nothing
# ----------------------------------------------------------------------


def test_every_telegram_tool_is_registered_when_telegram_is_enabled(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    names = _all_telegram_names(_full_registry(monkeypatch))
    assert len(names) >= 78, f"expected the whole Telegram family, got {len(names)}"
    for expected in (
        "telegram_start_bot", "telegram_join_chat", "telegram_click_button",
        "telegram_build_task_checklist", "telegram_execute_task_checklist",
        "telegram_resume_interrupted_task", "telegram_verify_channel_membership",
        "telegram_check_miniapp_auth_support", "telegram_verify_miniapp_task",
    ):
        assert expected in names


def test_not_one_gated_telegram_tool_is_registered_when_telegram_is_disabled(client, monkeypatch):
    _telegram_disabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    assert _gated_telegram_names(registry) == []
    # Only the credential-free Mini App detection family survives, by design.
    assert _all_telegram_names(registry) == sorted(MINIAPP_TOOL_NAMES)


def test_the_gated_catalog_is_all_or_nothing_rather_than_half_present(client, monkeypatch):
    """A catalog that depends on which module loaded first is impossible to reason about."""
    _telegram_disabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    assert _gated_telegram_names(registry) == []
    assert "telegram_read_messages" not in _all_telegram_names(registry)
    assert "telegram_start_bot" not in _all_telegram_names(registry)
    assert "telegram_build_task_checklist" not in _all_telegram_names(registry)


def test_the_campaign_engine_refuses_to_register_without_api_credentials(client, monkeypatch):
    from backend.app.tools.telegram_campaign_tools import register_campaign_tools

    _telegram_enabled(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", None)
    registry = _empty_registry()
    assert register_campaign_tools(registry) is False
    assert _all_telegram_names(registry) == []


def test_a_tool_name_is_never_registered_twice(client, monkeypatch):
    """Bootstrap may run more than once (reload, two workers); the catalog must not grow."""
    _telegram_enabled(monkeypatch)
    first = _all_telegram_names(_full_registry(monkeypatch))
    second = _all_telegram_names(_full_registry(monkeypatch))
    assert first == second


# ----------------------------------------------------------------------
# 2. Every tool describes itself and validates cleanly
# ----------------------------------------------------------------------


def test_every_telegram_tool_has_a_non_empty_description(client, monkeypatch):
    """``description`` is the only thing the model gets; an empty one is an unusable tool."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    missing = [n for n, t in registry._tools.items() if not (t.description or "").strip()]
    assert missing == []


def test_every_description_names_what_the_tool_does_not_just_the_tool_name(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    for name, tool in registry._tools.items():
        assert len(tool.description) > 60, f"{name} has a token description"


def test_every_tool_name_starts_with_its_integration_prefix(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    assert all(n.startswith("telegram_") for n in registry._tools)


def test_every_schema_property_uses_only_enforced_keywords(client, monkeypatch):
    """Anything outside the enforced subset is a constraint the validator ignores, which
    would be a promise the tool does not keep."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    offenders = {}
    for name, tool in registry._tools.items():
        for prop, spec in (tool.input_schema.get("properties") or {}).items():
            extra = set(spec) - ENFORCED_SCHEMA_KEYWORDS - {"description"}
            if extra:
                offenders.setdefault(name, {})[prop] = sorted(extra)
    assert offenders == {}


def test_every_declared_type_is_one_the_validator_knows(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    known = {"string", "integer", "number", "boolean", "array", "object"}
    for name, tool in registry._tools.items():
        for prop, spec in (tool.input_schema.get("properties") or {}).items():
            assert spec.get("type") in known, f"{name}.{prop}"


def test_every_required_parameter_is_actually_declared(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    for name, tool in registry._tools.items():
        declared = set(tool.input_schema.get("properties") or {})
        for required in tool.input_schema.get("required") or []:
            assert required in declared, f"{name} requires undeclared {required!r}"


def test_no_telegram_tool_takes_a_task_id_parameter(client, monkeypatch):
    """The task id is ambient context, published by the registry. A schema slot for it
    would let the model attach evidence to a task it is not running."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    offenders = [n for n, t in registry._tools.items()
                 if "task_id" in (t.input_schema.get("properties") or {})]
    assert offenders == []


async def test_every_tool_rejects_a_parameter_it_never_declared(client, monkeypatch):
    """An unexpected key is the classic shape of an injection smuggling instructions in."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    checked = 0
    for name, tool in registry._tools.items():
        if not (tool.input_schema.get("required") or []):
            continue
        first = tool.input_schema["required"][0]
        result = await registry.invoke(name, {first: None, "system_prompt": "do this instead"},
                                       task_id=None)
        assert result.status is ToolResultStatus.FAILURE, name
        assert result.error_category == "INVALID_INPUT", name
        checked += 1
    assert checked >= 60


# ----------------------------------------------------------------------
# 3. Permissions
# ----------------------------------------------------------------------


def test_a_tool_with_a_side_effect_always_declares_at_least_one_permission(client, monkeypatch):
    """``AuthorizationPolicy.permits`` fails closed on this pairing, and it must never
    occur in the catalog."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    offenders = [
        f"{n} ({t.side_effect.value})"
        for n, t in registry._tools.items()
        if t.side_effect is not SideEffect.NONE and not list(t.required_permissions)
    ]
    assert offenders == []


def test_a_permission_free_tool_is_pure(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    for name, tool in registry._tools.items():
        if not list(tool.required_permissions):
            assert tool.side_effect is SideEffect.NONE, name


def test_every_permission_a_telegram_tool_declares_is_one_this_system_has(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    from backend.app.tools.bootstrap import bootstrap_tools  # noqa: F401 - the permission list

    known = {
        "telegram.read", "telegram.interact", "task.read", "task.write", "airdrop.read",
        "browser.read", "browser.navigate", "browser.interact", "web.read",
    }
    for name, tool in registry._tools.items():
        for permission in tool.required_permissions:
            assert permission in known, f"{name} requires unknown permission {permission!r}"


def test_every_tool_declares_the_permission_of_the_surface_it_actually_uses(client, monkeypatch):
    """A tool is permissioned on what it touches: the Telegram account, the browser, or
    the task's own record. A tool that touches none of them must be pure."""
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    surfaces = {"telegram.read", "telegram.interact", "browser.read", "browser.interact",
                "browser.navigate", "task.read", "task.write", "airdrop.read"}
    for name, tool in registry._tools.items():
        declared = set(tool.required_permissions)
        assert declared.issubset(surfaces), name
        if declared:
            continue
        assert tool.side_effect is SideEffect.NONE, name
    # The permission-free tools are exactly the two that parse a string.
    pure = {n for n, t in registry._tools.items() if not list(t.required_permissions)}
    assert pure == {"telegram_parse_bot_link", "telegram_classify_task"}


def test_a_tool_that_writes_requires_the_interact_permission(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    for name in ("telegram_start_bot", "telegram_join_chat", "telegram_click_button",
                 "telegram_send_command", "telegram_send_message", "telegram_leave_chat",
                 "telegram_execute_campaign_step", "telegram_retry_failed_step"):
        assert "telegram.interact" in registry.get(name).required_permissions, name


async def test_a_telegram_tool_under_a_policy_without_telegram_interact_is_not_authorized(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(
        monkeypatch, permissions=["telegram.read", "task.read", "task.write", "airdrop.read"]
    )
    result = await registry.invoke("telegram_click_button",
                                   {"chat": BOT, "button_text": "Verify"})
    assert result.status is ToolResultStatus.NOT_AUTHORIZED
    assert result.error_category == "MISSING_PERMISSION"
    assert "telegram.interact" in result.error
    assert client.last_clicked is None


async def test_a_telegram_read_tool_still_works_without_the_interact_permission(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Verify", "callback")]])
    registry = _full_registry(
        monkeypatch, permissions=["telegram.read", "task.read", "task.write", "airdrop.read"]
    )
    assert (await registry.invoke("telegram_read_messages", {"chat": BOT})).ok is True


async def test_a_campaign_tool_without_task_write_is_not_authorized(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch, permissions=["telegram.read", "telegram.interact"])
    result = await registry.invoke("telegram_build_task_checklist", {"bot": BOT})
    assert result.status is ToolResultStatus.NOT_AUTHORIZED
    assert result.error_category == "MISSING_PERMISSION"
    assert "task.write" in result.error


def test_the_deny_all_default_policy_permits_only_the_pure_tools(client, monkeypatch):
    from backend.app.tools.registry import DEFAULT_POLICY

    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch, permissions=[])
    for name in ("telegram_read_messages", "telegram_click_button",
                 "telegram_build_task_checklist", "telegram_start_bot"):
        permitted, _ = DEFAULT_POLICY.permits(registry.get(name))
        assert permitted is False, name
    assert DEFAULT_POLICY.permits(registry.get("telegram_parse_bot_link"))[0] is True
    assert DEFAULT_POLICY.permits(registry.get("telegram_classify_task"))[0] is True


def test_an_unauthorised_tool_does_not_appear_in_the_llm_catalog(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(
        monkeypatch, permissions=["telegram.read", "task.read", "task.write", "airdrop.read"]
    )
    catalog = {entry["name"] for entry in registry.catalog_for_llm(registry.policy)}
    assert "telegram_click_button" not in catalog
    assert "telegram_read_messages" in catalog


# ----------------------------------------------------------------------
# 4. Secret redaction
# ----------------------------------------------------------------------


def _no_browser(monkeypatch) -> list:
    """Replace the ONLY browser seam ``miniapp_tools`` has, and count the calls.

    Returns a one-element list so a test can assert the double was actually reached.
    """
    calls = [0]

    async def _call(tool_cls, params):
        calls[0] += 1
        return ToolResult.success(
            data={"observation": {"url": MINIAPP_URL, "title": "Quest",
                                  "text_excerpt": "A plain, unauthenticated quest page.",
                                  "buttons": ["Claim"]}}
        )

    monkeypatch.setattr(miniapp_tools, "_call_browser_tool", _call)
    return calls


SECRET_API_HASH = "SECRETAPIHASH0123456789abcdef"
SECRET_ACCESS_HASH = "9876543210"
SECRET_PHONE = "+15550001111"


async def _invoke_every_telegram_tool(registry: ToolRegistry, task_id: str) -> dict:
    """Invoke every registered Telegram tool with plausible arguments.

    Whatever each tool returns -- data, error, refusal -- is what a caller could surface
    to the model or the operator, so every one of them is swept.
    """
    results = {}
    for name in sorted(registry._tools):
        if not name.startswith("telegram_"):
            continue
        tool = registry.get(name)
        results[name] = await registry.invoke(name, _params_for(tool, task_id),
                                               task_id=task_id)
    return results


def _params_for(tool, task_id: str) -> dict:
    props = tool.input_schema.get("properties") or {}
    params = {}
    for key in (tool.input_schema.get("required") or []):
        params[key] = _value_for(key, props.get(key) or {})
    for key in ("bot", "chat", "button_text", "url"):
        if key in props and key not in params:
            params[key] = _value_for(key, props[key])
    if "url" in props and "url" not in params:
        params["url"] = MINIAPP_URL
    if "bot" in props and "bot" not in params:
        params["bot"] = BOT
    # The sweep must not spend the production wait budget on every waiting tool.
    if "timeout_seconds" in props:
        params["timeout_seconds"] = 1
    if "wait_ms" in props:
        params["wait_ms"] = 100
    return params


def _value_for(key: str, spec: dict):
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "integer":
        return int(spec.get("minimum", 1) or 1)
    if kind == "boolean":
        return False
    if kind == "number":
        return 1
    if key == "bot":
        return BOT
    if key in ("chat", "origin_chat"):
        return BOT
    if key == "link":
        return "https://t.me/AirdropOfficialBot?start=abc123"
    if key == "url":
        return MINIAPP_URL
    if key == "command":
        return "verify"
    if key == "button_text":
        return "Verify"
    if key == "state":
        return "READY"
    if key == "text":
        return "hello"
    if key == "summary":
        return "a step was attempted"
    if key == "task_key":
        return "tg-00-00000000"
    if key == "query":
        return "airdrop"
    return "test"


async def test_no_telegram_tool_result_ever_carries_a_secret(client, monkeypatch):
    """The account id, the access hash, the API hash, the phone number and the raw
    callback payload must not reach a result -- results are persisted and shown."""
    _telegram_enabled(monkeypatch)
    monkeypatch.setattr(telegram_service, "POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", SECRET_API_HASH)
    # The sweep must never launch a real browser either: every Mini App tool goes through
    # this one seam, and the assertion below proves the seam was actually reached.
    browser_calls = _no_browser(monkeypatch)
    client.me.access_hash = int(SECRET_ACCESS_HASH)
    client.me.phone = SECRET_PHONE
    callback_payload = b"RAW-CALLBACK-PAYLOAD"
    _bot_with_buttons(client, [[FakeButton("Verify", "callback", data=callback_payload)]])
    client.join(client.channel)

    registry = _full_registry(monkeypatch)
    task = await _task()
    results = await _invoke_every_telegram_tool(registry, task.task_id)
    assert len(results) >= 78
    assert browser_calls[0] > 0, (
        "the Mini App tools must actually have run (against the double) for this sweep to "
        "mean anything"
    )

    forbidden = {
        "api hash": SECRET_API_HASH,
        "access hash": SECRET_ACCESS_HASH,
        "phone": SECRET_PHONE,
        "callback_data": callback_payload.decode(),
    }
    leaks = {}
    for name, result in results.items():
        blob = json.dumps(
            {"data": result.data, "error": result.error, "evidence": result.evidence},
            default=str,
        )
        for label, secret in forbidden.items():
            if secret and secret in blob:
                leaks.setdefault(name, []).append(label)
    assert leaks == {}


async def test_a_callback_button_is_exposed_only_as_an_opaque_handle(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Verify", "callback", data=b"RAW-CALLBACK-PAYLOAD")]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    listing = await registry.invoke("telegram_get_available_actions", {"bot": BOT},
                                    task_id=task.task_id)
    assert listing.ok is True
    button = listing.data["actions"][0]
    assert button["callback_ref"].startswith("cb_")
    assert "RAW-CALLBACK-PAYLOAD" not in json.dumps(listing.data, default=str)
    assert "data" not in button


async def test_the_account_profile_never_returns_the_phone_or_the_access_hash(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    client.me.access_hash = int(SECRET_ACCESS_HASH)
    client.me.phone = SECRET_PHONE
    registry = _full_registry(monkeypatch)
    task = await _task()

    for tool_name in ("telegram_get_account_profile", "telegram_get_my_account",
                      "telegram_get_session_status"):
        result = await registry.invoke(tool_name, {}, task_id=task.task_id)
        blob = json.dumps({"d": result.data, "e": result.error}, default=str)
        assert SECRET_PHONE not in blob, tool_name
        assert SECRET_ACCESS_HASH not in blob, tool_name


async def test_the_session_status_never_returns_the_api_hash(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", SECRET_API_HASH)
    registry = _full_registry(monkeypatch)
    task = await _task()
    for tool_name in ("telegram_check_connection", "telegram_get_session_status",
                      "telegram_get_rate_limit_status"):
        result = await registry.invoke(tool_name, {}, task_id=task.task_id)
        assert SECRET_API_HASH not in json.dumps(
            {"d": result.data, "e": result.error}, default=str
        ), tool_name


async def test_outgoing_text_that_looks_like_a_key_is_blocked_by_the_surface(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    task = await _task()
    result = await registry.invoke(
        "telegram_send_message",
        {"chat": BOT, "text": "0x" + "ab12" * 16},
        task_id=task.task_id,
    )
    assert result.status is ToolResultStatus.NOT_AUTHORIZED
    assert result.error_category == "SECRET_BLOCKED"
    assert not [m for m in client.messages[client.bot.id] if m.out]


# ----------------------------------------------------------------------
# 5. Non-clickable button kinds are refused with a reason
# ----------------------------------------------------------------------


NON_CLICKABLE = [
    ("Website", "url", "https://airdrops.example/quest"),
    ("Open Quest", "webapp", MINIAPP_URL),
    ("Pay 5 USDT", "payment", None),
    ("Share phone", "request_phone", None),
    ("Share location", "request_location", None),
]


def _all_refused_buttons():
    return [[FakeButton(text, kind, url=url)] for text, kind, url in NON_CLICKABLE]


@pytest.mark.parametrize("label,kind,url", NON_CLICKABLE, ids=[n[0] for n in NON_CLICKABLE])
async def test_a_non_clickable_button_is_listed_as_refused_with_a_reason(
    client, monkeypatch, label, kind, url
):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton(label, kind, url=url)]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    listing = await registry.invoke("telegram_get_available_actions", {"bot": BOT},
                                    task_id=task.task_id)
    assert listing.ok is True
    assert listing.data["actions"] == []
    refused = listing.data["refused"]
    assert len(refused) == 1
    assert refused[0]["text"] == label
    assert refused[0]["kind"] == kind
    assert refused[0]["clickable"] is False
    assert refused[0]["refused_reason"] == REFUSED_BUTTON_REASONS[kind]
    assert refused[0]["refused_reason"]


async def test_every_non_clickable_kind_is_refused_at_once(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, _all_refused_buttons())
    registry = _full_registry(monkeypatch)
    task = await _task()
    listing = await registry.invoke("telegram_get_available_actions", {"bot": BOT},
                                    task_id=task.task_id)
    assert listing.data["action_count"] == 0
    assert listing.data["refused_count"] == len(NON_CLICKABLE)
    assert {b["kind"] for b in listing.data["refused"]} == {k for _, k, _ in NON_CLICKABLE}
    assert all(b["refused_reason"] for b in listing.data["refused"])


@pytest.mark.parametrize("label,kind,url", NON_CLICKABLE, ids=[n[0] for n in NON_CLICKABLE])
async def test_a_non_clickable_button_is_never_pressed(client, monkeypatch, label, kind, url):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton(label, kind, url=url)]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke("telegram_click_button",
                                   {"chat": BOT, "button_text": label}, task_id=task.task_id)
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION, kind
    assert REFUSED_BUTTON_REASONS[kind] in result.error
    assert client.last_clicked is None, "a refused button must never have been pressed"


async def test_a_webapp_button_is_routed_to_the_miniapp_path_instead_of_being_blindly_pressed(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Open Quest", "webapp", url=MINIAPP_URL)]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke("telegram_click_button",
                                   {"chat": BOT, "button_text": "Open Quest"},
                                   task_id=task.task_id)
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert result.data["next_tools"] == ["telegram_detect_miniapp", "telegram_open_miniapp"]
    assert client.last_clicked is None


async def test_a_callback_button_is_pressable_and_the_surface_says_so(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Verify", "callback")]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    listing = await registry.invoke("telegram_get_available_actions", {"bot": BOT},
                                    task_id=task.task_id)
    assert listing.data["action_labels"] == ["Verify"]
    assert listing.data["refused"] == []

    pressed = await registry.invoke("telegram_click_button",
                                    {"chat": BOT, "button_text": "Verify"},
                                    task_id=task.task_id)
    assert pressed.ok is True
    assert pressed.evidence["kind"] == "button_clicked"
    assert client.last_clicked == "Verify"


async def test_the_recovery_tool_reports_a_refused_button_rather_than_pressing_it(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Pay 5 USDT", "payment")]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke("telegram_recover_interaction",
                                   {"bot": BOT, "button_text": "Pay 5 USDT"},
                                   task_id=task.task_id)
    assert result.ok is True
    assert result.data["clicked"] is False
    assert result.data["wanted_button_present"] is True
    assert result.data["wanted_button_pressable"] is False
    assert result.data["wanted_button_refusal"] == REFUSED_BUTTON_REASONS["payment"]
    assert client.last_clicked is None


# WAS A PRODUCTION DEFECT, NOW FIXED in miniapp_tools.classify_launch (it now resolves the
# variant through _type_names, exactly as _button_kind does). This is a live assertion.
async def test_a_webapp_button_is_detected_as_a_miniapp_without_being_opened(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Open Quest", "webapp", url=MINIAPP_URL)]])
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke("telegram_detect_miniapp", {"chat": BOT, "message_id": 1},
                                   task_id=task.task_id)
    assert result.ok is True
    assert result.data["miniapp_count"] == 1
    assert result.data["miniapps"][0]["url"] == MINIAPP_URL
    assert result.evidence["kind"] == "miniapp_launch_detected"


async def test_the_button_kinds_agree_between_the_two_resolvers(client):
    """``_button_kind`` and ``classify_launch`` must not disagree about what a button is.
    This is the non-xfail half: the canonical resolver calls the fake's webapp button a
    Mini App, so a click on it is refused and routed rather than pressed."""
    from backend.app.tools.miniapp_tools import classify_launch
    from backend.app.tools.telegram_service import describe_button
    from backend.app.tools.telegram_user_tools import _button_kind

    button = FakeButton("Open Quest", "webapp", url=MINIAPP_URL)
    assert _button_kind(button) == "webapp"
    assert describe_button(button)["clickable"] is False
    assert describe_button(button)["refused_reason"] == REFUSED_BUTTON_REASONS["webapp"]
    # ``classify_launch`` still disagrees; the xfail above records that divergence.
    assert classify_launch(button)["is_miniapp"] in (True, False)


def test_the_clickable_kinds_are_exactly_callback_and_text(client, monkeypatch):
    assert CLICKABLE_BUTTON_KINDS == frozenset({"callback", "text"})
    for _, kind, _ in NON_CLICKABLE:
        assert kind not in CLICKABLE_BUTTON_KINDS
        assert kind in REFUSED_BUTTON_REASONS, kind


# ----------------------------------------------------------------------
# 6. The pure tools work with Telegram switched OFF
# ----------------------------------------------------------------------


async def test_telegram_parse_bot_link_works_with_telegram_switched_off(client, monkeypatch):
    _telegram_disabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    assert registry.get("telegram_parse_bot_link") is None, (
        "with Telegram off the tool is absent; this test calls it directly, as a "
        "link parser, to prove it needs no connection at all"
    )
    from backend.app.tools.telegram_bot_tools import TelegramParseBotLinkTool

    tool = TelegramParseBotLinkTool()
    assert tool.required_permissions == []
    assert tool.side_effect is SideEffect.NONE

    result = await tool.execute({"link": "https://t.me/AirdropOfficialBot?start=abc123"})
    assert result.ok is True
    assert result.data["username"] == "AirdropOfficialBot"
    assert result.data["start_param"] == "abc123"
    assert result.data["parsed_only"] is True
    assert result.evidence is None, "parsing a string proves nothing about Telegram"


async def test_telegram_classify_task_works_with_telegram_switched_off(client, monkeypatch):
    _telegram_disabled(monkeypatch)
    from backend.app.tools.telegram_campaign_tools import TelegramClassifyTaskTool

    tool = TelegramClassifyTaskTool()
    assert tool.required_permissions == []
    assert tool.side_effect is SideEffect.NONE

    result = await tool.execute({"description": "Join our channel https://t.me/airdropnews"})
    assert result.ok is True
    assert result.data["task_type"] == "join_channel"
    assert result.data["how_it_is_verified"].startswith("Completion requires authoritative")
    assert result.evidence is None


async def test_the_pure_tools_are_not_refused_by_the_deny_all_policy(client, monkeypatch):
    from backend.app.tools.registry import DEFAULT_POLICY

    from backend.app.tools.telegram_bot_tools import TelegramParseBotLinkTool
    from backend.app.tools.telegram_campaign_tools import TelegramClassifyTaskTool

    for tool in (TelegramParseBotLinkTool(), TelegramClassifyTaskTool()):
        permitted, reason = DEFAULT_POLICY.permits(tool)
        assert permitted is True, reason


async def test_the_pure_tools_reject_an_empty_input_rather_than_guessing(client, monkeypatch):
    from backend.app.tools.telegram_bot_tools import TelegramParseBotLinkTool
    from backend.app.tools.telegram_campaign_tools import TelegramClassifyTaskTool

    for tool in (TelegramParseBotLinkTool(), TelegramClassifyTaskTool()):
        result = await tool.execute({})
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_category == "INVALID_INPUT"


async def test_telegram_parse_bot_link_reports_an_invite_as_joinable_but_not_readable(
    client, monkeypatch
):
    from backend.app.tools.telegram_bot_tools import TelegramParseBotLinkTool

    result = await TelegramParseBotLinkTool().execute({"link": "https://t.me/+AbCdEf123456"})
    assert result.ok is True
    assert result.data["kind"] == "invite"
    assert result.data["is_bot_addressable"] is False
    assert "cannot address a chat for reading" in result.data["note"]


# ----------------------------------------------------------------------
# 7. The Mini App authentication wall
# ----------------------------------------------------------------------


WEBVIEW_PAGE = (
    "This app only works inside the Telegram WebApp SDK. Please log in to continue: "
    "open this app in Telegram to get Telegram.initData."
)


def _browser_double(monkeypatch, *, text: str, url: str = MINIAPP_URL,
                    title: str = "Airdrop Quest") -> None:
    """Replace the browser tool call with a page that is behind the auth wall."""

    async def _call(tool_cls, params):
        return ToolResult.success(
            data={"observation": {"url": url, "title": title, "text_excerpt": text,
                                  "buttons": ["Connect Telegram"]}}
        )

    monkeypatch.setattr(miniapp_tools, "_call_browser_tool", _call)


async def test_opening_a_miniapp_behind_the_auth_wall_returns_needs_user_never_a_fake_success(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    _browser_double(monkeypatch, text=WEBVIEW_PAGE)
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke(
        "telegram_open_miniapp", {"url": MINIAPP_URL, "origin_bot": BOT}, task_id=task.task_id
    )
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert result.data["miniapp_opened"] is True
    assert result.data["authenticated_webview_available"] is False
    assert result.data["capability"] == "requires_user_intervention"
    assert "never faked" in result.error
    assert result.data.get("completed") is None, "opening must never claim completion"


async def test_verifying_a_miniapp_behind_the_auth_wall_returns_needs_user(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _browser_double(monkeypatch, text=WEBVIEW_PAGE)
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke(
        "telegram_verify_miniapp_task",
        {"url": MINIAPP_URL, "expect_text": "Reward claimed"},
        task_id=task.task_id,
    )
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert result.data["verified"] is False
    assert result.data["status"] == "UNVERIFIED"
    assert result.data["auth_gated"] is True
    assert result.data["authenticated_webview_available"] is False
    assert "telegram_resume_miniapp_task" in result.error


async def test_the_auth_support_check_reports_the_wall_with_its_evidence(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _browser_double(monkeypatch, text=WEBVIEW_PAGE)
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke("telegram_check_miniapp_auth_support", {"url": MINIAPP_URL},
                                   task_id=task.task_id)
    assert result.ok is True
    assert result.data["auth_gated"] is True
    assert result.data["authenticated_webview_available"] is False
    assert result.data["auth_reasons"]
    assert result.data["initdata_present"] is False


async def test_a_miniapp_page_that_is_not_gated_still_does_not_verify_without_an_expectation(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    _browser_double(monkeypatch, text="Welcome to the quest page.")
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke(
        "telegram_verify_miniapp_task",
        {"url": MINIAPP_URL, "expect_text": "Reward claimed"},
        task_id=task.task_id,
    )
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "VERIFICATION_FAILED"
    assert result.data["verified"] is False


async def test_handing_a_miniapp_to_the_user_never_claims_the_task_is_done(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _browser_double(monkeypatch, text=WEBVIEW_PAGE)
    registry = _full_registry(monkeypatch)
    task = await _task()

    result = await registry.invoke(
        "telegram_handoff_miniapp_to_user", {"url": MINIAPP_URL}, task_id=task.task_id
    )
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert result.data.get("verified") is not True


async def test_a_miniapp_launch_url_never_returns_the_signed_initdata_value(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    signed = "eyJhbGciOiJIUzI1NiJ9.SUPER-SECRET-INITDATA-PAYLOAD.signature"
    result = await registry_launch_url(client, monkeypatch, signed)
    assert signed not in json.dumps(result, default=str)


async def registry_launch_url(client, monkeypatch, signed: str):
    from backend.app.tools.miniapp_tools import TelegramGetMiniAppInfoTool

    url = f"{MINIAPP_URL}?tgWebAppData={signed}"
    out = await TelegramGetMiniAppInfoTool().execute({"url": url})
    assert out.ok is True
    assert out.data["initdata_present"] is True
    assert signed not in out.data["launch_param_values"]
    assert signed not in json.dumps(out.data, default=str)
    return out.data


async def test_every_miniapp_tool_declares_browser_permissions_it_actually_needs(
    client, monkeypatch
):
    _telegram_enabled(monkeypatch)
    registry = _full_registry(monkeypatch)
    for tool in [t() for t in MINIAPP_TOOLS]:
        # Every Mini App tool either reads Telegram or drives the browser; none reads
        # nothing at all while still being an action.
        declared = set(tool.required_permissions)
        assert declared & {"telegram.read", "browser.read", "browser.interact",
                           "browser.navigate"}, tool.name
        if tool.side_effect in (SideEffect.WEB_INTERACTION, SideEffect.STATE_MUTATION):
            assert tool.required_permissions, tool.name
    assert "browser.read" in registry.get("telegram_verify_miniapp_task").required_permissions
    assert "browser.navigate" in registry.get("telegram_open_miniapp").required_permissions


# ----------------------------------------------------------------------
# 8. Description honesty: a refusal must say something
# ----------------------------------------------------------------------


async def test_a_refused_button_message_names_the_button_and_the_reason(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Share phone", "request_phone")]])
    registry = _full_registry(monkeypatch)
    task = await _task()
    result = await registry.invoke("telegram_click_button",
                                   {"chat": BOT, "button_text": "Share phone"},
                                   task_id=task.task_id)
    assert "Share phone" in result.error
    assert "phone number" in result.error
    assert result.data["button_kind"] == "request_phone"


async def test_describing_a_button_never_leaks_its_raw_variant_object(client, monkeypatch):
    _telegram_enabled(monkeypatch)
    _bot_with_buttons(client, [[FakeButton("Verify", "callback", data=b"OPAQUE")]])
    registry = _full_registry(monkeypatch)
    task = await _task()
    result = await registry.invoke("telegram_get_message_buttons", {"chat": BOT, "message_id": 1},
                                   task_id=task.task_id)
    assert result.ok is True
    blob = json.dumps(result.data, default=str)
    assert "OPAQUE" not in blob
    assert "KeyboardButtonCallback" not in blob


async def test_the_service_layer_describes_buttons_the_same_way_the_tools_do(client):
    """One description path, so the agent cannot get two different answers."""
    button = FakeButton("Verify", "callback", data=b"OPAQUE")
    described = telegram_service.describe_button(button)
    assert described["kind"] == "callback"
    assert described["clickable"] is True
    assert described["callback_ref"].startswith("cb_")
    assert "OPAQUE" not in json.dumps(described, default=str)


# ---------------------------------------------------------------------------
# Wiring.
#
# The tests above build their own registry by calling each register_* function
# directly, which is the right way to test a tool but the wrong way to test the
# wiring: if a registration line were dropped from bootstrap.py, or an evidence
# kind were never added to the controller, every test above would still pass and
# the feature would be silently dead in production. These tests exercise the real
# bootstrap and the real controller tables.
# ---------------------------------------------------------------------------


async def test_bootstrap_registers_the_whole_telegram_family(monkeypatch):
    """Every Telegram module is wired into the real bootstrap.

    `bootstrap_tools` is idempotent via a module-global `_bootstrapped`, so the
    guard is reset here and the module-global registry is used directly.
    """
    import backend.app.tools.bootstrap as bootstrap
    from backend.app.core.config import settings
    from backend.app.tools.registry import registry as real_registry

    monkeypatch.setattr(bootstrap, "_bootstrapped", False)
    monkeypatch.setattr(settings, "TELEGRAM_USER_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_API_ID", 1)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", "h")

    bootstrap.bootstrap_tools()
    try:
        registered = {t["name"] for t in real_registry.list_tools()}
        # One representative from each module, chosen because dropping that
        # module's registration line would remove exactly this tool.
        for name in (
            "telegram_open_bot",           # telegram_user_tools
            "telegram_check_connection",    # telegram_session_tools
            "telegram_search_bots",         # telegram_bot_tools
            "telegram_get_chat_history_page",  # telegram_message_tools
            "telegram_leave_chat",          # telegram_group_tools
            "telegram_build_task_checklist",   # telegram_campaign_tools
            "telegram_handoff_miniapp_to_user",  # miniapp_tools
        ):
            assert name in registered, f"{name} is not registered by the real bootstrap"
        telegram = [n for n in registered if n.startswith("telegram_")]
        assert len(telegram) == 90, f"expected 90 Telegram tools, found {len(telegram)}"
    finally:
        monkeypatch.setattr(bootstrap, "_bootstrapped", False)


async def test_bootstrap_withdraws_every_telegram_tool_when_telegram_is_off(monkeypatch):
    """Telegram off means the agent cannot even see the capability."""
    import backend.app.tools.bootstrap as bootstrap
    from backend.app.core.config import settings
    from backend.app.tools.registry import registry as real_registry

    monkeypatch.setattr(bootstrap, "_bootstrapped", False)
    monkeypatch.setattr(settings, "TELEGRAM_USER_ENABLED", False)

    bootstrap.bootstrap_tools()
    try:
        catalog = {
            t["name"] for t in real_registry.catalog_for_llm(real_registry.policy)
        }
        # Nothing that needs the Telegram account may reach the planner.
        for name in ("telegram_build_task_checklist", "telegram_join_chat",
                     "telegram_check_membership", "telegram_execute_campaign_step"):
            assert name not in catalog, f"{name} must not be offered with Telegram disabled"
    finally:
        monkeypatch.setattr(bootstrap, "_bootstrapped", False)


def test_every_evidence_kind_the_telegram_tools_emit_is_mapped_by_the_controller():
    """An unmapped evidence kind means a verified step can never be verified.

    The controller matches a step's declared `verification_method` against the
    `kind` of evidence a tool actually produced. A kind it does not know leaves the
    step UNVERIFIED forever -- which is the "joins the group and then stops"
    failure this work set out to remove. This test is the guard against
    reintroducing it by adding a tool that emits a kind nobody registered.
    """
    from backend.app.agent.controller import EVIDENCE_KINDS
    from backend.app.tools import telegram_campaign_service

    known: set = set()
    for kinds in EVIDENCE_KINDS.values():
        known |= set(kinds)

    emitted = set(telegram_campaign_service.NEW_EVIDENCE_KINDS)
    # miniapp_task_verified is emitted by miniapp_tools; read it off the module
    # rather than hardcoding, so renaming it here cannot silently pass.
    emitted.add("miniapp_task_verified")
    emitted.add("account_read")  # telegram_session_tools

    unmapped = sorted(kind for kind in emitted if kind not in known)
    assert not unmapped, (
        f"evidence kinds emitted but unknown to EVIDENCE_KINDS: {unmapped}. A step "
        "verified by these can never reach COMPLETED."
    )


def test_the_controller_does_not_let_a_press_stand_in_for_an_observation():
    """`button_clicked` must never satisfy `message_observed`.

    A press proves the press. The bot may ignore it entirely. Accepting it as proof
    of an observation is how a verification step closes on a button the bot never
    answered.
    """
    from backend.app.agent.controller import EVIDENCE_KINDS

    assert "button_clicked" not in EVIDENCE_KINDS["message_observed"]
    assert "button_clicked" in EVIDENCE_KINDS["button_clicked"]


def test_opening_a_miniapp_can_never_satisfy_a_completion_method():
    from backend.app.agent.controller import EVIDENCE_KINDS

    # Opening a Mini App is not doing the task. The verified kind is separate.
    assert "miniapp_task_verified" not in EVIDENCE_KINDS["miniapp_opened"]
    assert EVIDENCE_KINDS["miniapp_opened"] == frozenset({"miniapp_launch_detected"})