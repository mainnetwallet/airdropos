"""Telegram group / channel tools: leaving, membership proof and invite inspection.

Companion to :mod:`backend.app.tools.telegram_message_tools`, built on the same shared
service layer (:mod:`backend.app.tools.telegram_service`). The safety model is the one
from ``telegram_user_tools`` and is unchanged: disabled unless configured, everything
through the shared per-account session, untrusted third-party text flagged and never
followed, and no secret (phone number, API hash, session string, login code,
``access_hash``) ever returned.

The whole point of this module is HONESTY about membership
---------------------------------------------------------
``telegram_join_chat`` proves a join *took*. These tools prove the state is *true now*,
against live Telegram. The distinction is carried explicitly in every result:

  * ``is_member``  -- True / False / **None** ("could not be determined")
  * ``verified``   -- True **only** when Telegram itself returned an authoritative
                       participant record for this account

A caller must test ``verified``, never ``is_member``. ``None`` is recorded as
unverified, never as success, and never as evidence: :func:`_evidence_only_if_verified`
is the single place evidence is attached, so an unproven join can never satisfy a
declared verification method.

``telegram_leave_chat`` is the only tool in the whole Telegram surface that
unsubscribes. It is idempotent, it reports honestly when the account was not a member
in the first place, and its description tells the model to use it only when the user
actually asked to leave.

``telegram_get_invite_link_info`` NEVER joins. It inspects a ``t.me/+hash`` link with
Telegram's read-only ``messages.checkChatInvite`` and, when Telegram will only return a
peek for the link, says ``metadata_requires_join: true`` rather than joining to find out.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.app.core.config import settings
from backend.app.tools import telegram_service as service
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.telegram_user_tools import (
    ACCOUNT_PROPERTY,
    TELETHON_AVAILABLE,
    UnknownAccount,
    _UNTRUSTED_NOTE,
    _evidence,
    _is_group_or_channel,
    _ref_payload,
    parse_target,
    session_for,
)

logger = logging.getLogger(__name__)

# The admin-right flags reported by telegram_get_chat_permissions. Read defensively:
# which ones exist depends on the Telethon layer, and a missing flag is reported as
# unknown rather than as False.
_ADMIN_RIGHT_FLAGS = (
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "manage_call",
    "manage_topics",
    "manage_direct_messages",
)

#: Tri-state membership explanation, repeated in results so no caller can over-read it.
MEMBERSHIP_NOTE_KEY = "membership_note"
MEMBERSHIP_NOTE = (
    "is_member is a TRI-STATE: True, False, or null when Telegram would not say. Test "
    "'verified', never 'is_member': verified is true only when Telegram itself returned an "
    "authoritative participant record for this account."
)


def _service_failure(exc: "service.TelegramServiceError") -> ToolResult:
    if exc.category == "SECRET_BLOCKED":
        return ToolResult(
            status=ToolResultStatus.NOT_AUTHORIZED,
            error=str(exc),
            error_category="SECRET_BLOCKED",
            retryable=False,
        )
    return ToolResult.failure(str(exc), category=exc.category, retryable=bool(exc.retryable))


def _service_guarded(fn):
    async def wrapper(client: Any) -> ToolResult:
        try:
            return await fn(client)
        except service.TelegramServiceError as exc:
            return _service_failure(exc)

    return wrapper


def _evidence_only_if_verified(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Membership evidence exists only for a PROVEN state.

    ``_evidence`` always marks ``authoritative: True``. Emitting it for a ``None`` state
    would let an unproven join satisfy a declared verification method, which is exactly
    the failure mode this module exists to prevent. A proven ``False`` (authoritatively
    NOT a member) is emitted too, but carries ``is_member: False`` so a consumer can
    never read it as "joined".
    """
    if not payload.get("verified"):
        return None
    extra: Dict[str, Any] = {
        "chat_id": payload.get("chat_id"),
        "membership_method": payload.get("membership_method"),
    }
    if "is_member" in payload:
        extra["is_member"] = payload.get("is_member")
    return _evidence("membership_verified", **extra)


def _admin_rights(participant: Any) -> Dict[str, Any]:
    """The admin-right flags actually present on a participant record."""
    rights = getattr(participant, "admin_rights", None)
    if rights is None:
        return {}
    out: Dict[str, Any] = {}
    for flag in _ADMIN_RIGHT_FLAGS:
        value = getattr(rights, flag, None)
        if isinstance(value, bool):
            out[flag] = value
    return out


def _participant_role(participant: Any) -> str:
    """A role derived from the participant record only -- never guessed."""
    name = type(participant).__name__.lower()
    if "creator" in name or getattr(participant, "creator", False):
        return "creator"
    if "left" in name:
        return "left"
    if "banned" in name:
        return "banned"
    if getattr(participant, "admin_rights", None) is not None:
        return "admin"
    banned_rights = getattr(participant, "banned_rights", None)
    if banned_rights is not None:
        # Restricted but present: some rights taken away.
        return "restricted"
    if getattr(participant, "user", None) is not None:
        return "member"
    return "unknown"


def _participant_is_member(participant: Any, role: str) -> Optional[bool]:
    """Membership implied by the participant record, or None when it implies nothing."""
    if role in ("creator", "admin", "member"):
        return True
    if role in ("left", "banned"):
        return False
    if role == "restricted":
        rights = getattr(participant, "banned_rights", None)
        if getattr(rights, "left", False):
            return False
        if getattr(rights, "view_messages", False):
            return False
        return True
    return None


class _GroupTool(BaseTool):
    """Shared execution shape: account lookup, session run, typed errors, never raise."""

    interactive: bool = False

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            session = session_for(params.get("account"))
        except UnknownAccount as exc:
            return ToolResult.failure(str(exc), category="INVALID_INPUT")
        ctx: Dict[str, Any] = {"session": session}
        try:
            return await session.run(
                _service_guarded(lambda client: self.action(client, params, ctx)),
                interactive=self.interactive,
                label=self.label_for(params),
            )
        except service.TelegramServiceError as exc:
            return _service_failure(exc)
        except Exception as exc:
            logger.warning("%s failed unexpectedly (%s)", self.name, type(exc).__name__)
            return ToolResult.failure(
                f"{self.name} could not complete ({type(exc).__name__}).", category="PROVIDER_ERROR"
            )

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    def label_for(self, params: Dict[str, Any]) -> str:
        return f"{self.name} ({str(params.get('chat') or '')[:40]})"

    async def resolve_chat(
        self, client: Any, session: Any, chat: Any, ctx: Dict[str, Any]
    ):
        """Resolve a chat reference to ``(entity, safe_ref)``; refuses invite links."""
        raw = str(chat or "").strip()
        if not raw:
            raise service.TelegramServiceError("A chat reference is required.", category="INVALID_INPUT")
        known = session.resolve_ref(raw)
        if known is not None and known.get("chat_id") is not None:
            raw = str(known["chat_id"])
        if not raw.lstrip("-").isdigit():
            try:
                target = parse_target(raw)
            except ValueError as exc:
                raise service.TelegramServiceError(str(exc), category="INVALID_INPUT") from exc
            if target["kind"] == "invite":
                raise service.TelegramServiceError(
                    "An invitation link is not a chat identity. Join it with telegram_join_chat "
                    "first, then use the chat id or @username it returns.",
                    category="INVALID_INPUT",
                )
        entity, ref = await service.resolve_entity(client, raw)
        session.remember_chat(ref)
        ctx["chat_id"] = ref.get("chat_id")
        return entity, ref


# ----------------------------------------------------------------------
# Leaving
# ----------------------------------------------------------------------


class TelegramLeaveChatTool(_GroupTool):
    """The one unsubscribing tool. Idempotent, and honest about non-membership."""

    interactive = True
    name = "telegram_leave_chat"
    version = "1.0.0"
    description = (
        "Leave (unsubscribe from) a Telegram channel or group. Use it ONLY when the user asked to "
        "leave/unsubscribe from that specific chat -- it is irreversible and it is the only tool "
        "that does this. Leaving a chat you are not in is a harmless no-op and is reported as "
        "already_left. It reports membership BEFORE and AFTER, and never claims success unless "
        "Telegram confirmed the account is out."
    )
    required_permissions = ["telegram.interact"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 45
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300,
                     "description": "@channel, t.me link, or a numeric chat id resolved earlier"},
            "reason": {"type": "string", "maxLength": 200,
                       "description": "Optional short note about why the user asked to leave, "
                                      "kept for the operator's audit trail"},
        },
        "required": ["chat"],
    }

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        entity, ref = await self.resolve_chat(client, session, params["chat"], ctx)

        before_state, before_method = await service.membership_state(client, entity)
        base: Dict[str, Any] = {
            **_ref_payload(ref),
            "membership_before": before_state,
            "membership_method": before_method,
            "reason": str(params.get("reason") or "")[:200] or None,
        }
        if before_state is False:
            # Idempotent no-op. Reported as success because the desired end state --
            # not being a member -- is already true, and nothing was changed.
            data = {
                **base,
                "left": False,
                "already_left": True,
                "verified": True,
                "is_member": False,
                "membership_after": False,
                "note": "The account was not a member, so nothing was changed.",
                MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
            }
            return ToolResult.success(data=data, evidence=_evidence_only_if_verified(data))

        try:
            from telethon.tl.functions.channels import LeaveChannelRequest  # type: ignore
        except Exception:
            return ToolResult.failure(
                "This Telethon build does not expose channels.LeaveChannelRequest, so leaving "
                "cannot be performed programmatically.",
                category="UNSUPPORTED_OPERATION",
                retryable=False,
                data=base,
            )

        if not _is_group_or_channel(entity):
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That chat'} is not a channel or a "
                "group, so there is nothing to leave.",
                category="INVALID_INPUT",
                data=base,
            )
        # Telethon 1.x only exposes channels.LeaveChannelRequest, which needs a Channel.
        # A basic group (a plain chat) cannot be left with it: say so rather than calling
        # something that cannot work.
        if ref.get("entity_kind") == "chat" and not (
            getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)
        ):
            return ToolResult.failure(
                "This is a basic group and the installed Telethon has no request to leave one "
                "(channels.LeaveChannelRequest only covers channels and supergroups). The operator "
                "must leave it in the Telegram app.",
                category="UNSUPPORTED_OPERATION",
                retryable=False,
                data=base,
            )

        already = False
        try:
            await client(LeaveChannelRequest(entity))
        except Exception as exc:
            name = type(exc).__name__
            if name == "UserNotParticipantError":
                already = True
            elif name in ("ChatWriteForbiddenError", "UserBannedInChannelError", "ChannelPrivateError",
                          "ChatAdminRequiredError"):
                return ToolResult.failure(
                    f"Telegram refused to leave this chat ({name}).", category="FORBIDDEN",
                    retryable=False, data=base,
                )
            else:
                raise

        after_state, after_method = await service.membership_state(client, entity)
        verified = after_state is False
        data = {
            **base,
            "left": not already,
            "already_left": already,
            "is_member": False,
            "membership_after": after_state,
            "membership_method": after_method,
            "verified": verified,
            "note": (
                "Left the chat and Telegram confirms the account is no longer a member."
                if verified
                else "The leave call returned, but Telegram did not confirm the account is out, "
                     "so this is NOT verified and must not be reported as done."
            ),
            MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
        }
        if not verified:
            return ToolResult.failure(
                "The leave call returned but membership afterwards could not be confirmed, so "
                "leaving is NOT verified.",
                category="VERIFICATION_FAILED",
                retryable=True,
                data=data,
            )
        return ToolResult.success(
            data=data,
            evidence=_evidence_only_if_verified(data),
        )


# ----------------------------------------------------------------------
# Membership
# ----------------------------------------------------------------------


class TelegramGetChatMembershipTool(_GroupTool):
    """The general tri-state membership read."""

    name = "telegram_get_chat_membership"
    version = "1.0.0"
    description = (
        "Check against live Telegram state whether the account is a member of a group or "
        "channel. Returns is_member (true / false / null when Telegram would not say) and "
        "verified, which is true ONLY when Telegram itself proved it -- always test 'verified', "
        "never 'is_member'. Use it to confirm a join actually took, or to avoid joining twice."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300,
                     "description": "@name, t.me link, or a numeric chat id resolved earlier"},
        },
        "required": ["chat"],
    }

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        entity, ref = await self.resolve_chat(client, session, params["chat"], ctx)
        payload = await service.verify_membership(client, entity, ref)
        payload.update(
            {
                "membership_determinable": payload.get("is_member") is not None,
                "verified": bool(payload.get("verified")),
                MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
            }
        )
        return ToolResult.success(
            data=payload, evidence=_evidence_only_if_verified(payload)
        )


class TelegramVerifyGroupMembershipTool(_GroupTool):
    """Membership of a group/supergroup, with the entity kind checked first."""

    name = "telegram_verify_group_membership"
    version = "1.0.0"
    description = (
        "Verify that the account is a member of a Telegram GROUP or SUPERGROUP (not a channel), "
        "against live Telegram state. Fails with INVALID_INPUT when the reference is a channel, a "
        "bot or a user, so a channel can never be 'verified' as a group. Returns is_member "
        "(true/false/null) and verified -- always test 'verified', never 'is_member'."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300},
        },
        "required": ["chat"],
    }

    #: Overridden by the channel variant.
    expected_kinds = ("group", "supergroup", "chat")

    #: The tool a caller with the other entity kind should use instead.
    other_tool_name = "telegram_verify_channel_membership"

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        entity, ref = await self.resolve_chat(client, session, params["chat"], ctx)

        kind = ref.get("entity_kind") or "chat"
        is_group = bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)) \
            or kind in ("group", "supergroup", "chat")
        is_channel = bool(getattr(entity, "broadcast", False)) or kind == "channel"
        if not _is_group_or_channel(entity) or getattr(entity, "bot", False):
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That reference'} is not a group or "
                "channel, so it cannot be verified as a group.",
                category="INVALID_INPUT",
                retryable=False,
                data={**_ref_payload(ref)},
            )
        if is_channel and not is_group:
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That reference'} is a broadcast "
                f"channel, not a {'/'.join(self.expected_kinds)}. Use {self.other_tool_name} instead.",
                category="INVALID_INPUT",
                retryable=False,
                data={**_ref_payload(ref)},
            )
        if not is_group:
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That reference'} resolved to a "
                f"{kind}, not a {'/'.join(self.expected_kinds)}. Use {self.other_tool_name} instead.",
                category="INVALID_INPUT",
                retryable=False,
                data={**_ref_payload(ref)},
            )

        payload = await service.verify_membership(client, entity, ref)
        payload.update(
            {
                "entity_kind": kind,
                "membership_determinable": payload.get("is_member") is not None,
                MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
            }
        )
        return ToolResult.success(
            data=payload, evidence=_evidence_only_if_verified(payload)
        )


class TelegramVerifyChannelMembershipTool(TelegramVerifyGroupMembershipTool):
    """The same check, specialised for broadcast channels."""

    name = "telegram_verify_channel_membership"
    version = "1.0.0"
    description = (
        "Verify that the account is a member/subscribed to a Telegram CHANNEL (broadcast, not a "
        "group), against live Telegram state. Fails with INVALID_INPUT when the reference is a "
        "group or a bot, so a group can never be 'verified' as a channel. Returns is_member "
        "(true/false/null) and verified -- always test 'verified', never 'is_member'."
    )
    expected_kinds = ("channel",)
    other_tool_name = "telegram_verify_group_membership"

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        entity, ref = await self.resolve_chat(client, session, params["chat"], ctx)
        kind = ref.get("entity_kind") or "chat"
        is_channel = bool(getattr(entity, "broadcast", False)) or kind == "channel"
        if not _is_group_or_channel(entity) or getattr(entity, "bot", False):
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That reference'} is not a group or "
                "channel, so it cannot be verified as a channel.",
                category="INVALID_INPUT",
                retryable=False,
                data={**_ref_payload(ref)},
            )
        if not is_channel:
            return ToolResult.failure(
                f"{ref.get('title') or ref.get('username') or 'That reference'} resolved to a "
                f"{kind}, not a broadcast channel. Use telegram_verify_group_membership instead.",
                category="INVALID_INPUT",
                retryable=False,
                data={**_ref_payload(ref)},
            )
        payload = await service.verify_membership(client, entity, ref)
        payload.update(
            {
                "entity_kind": kind,
                "membership_determinable": payload.get("is_member") is not None,
                MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
            }
        )
        return ToolResult.success(
            data=payload, evidence=_evidence_only_if_verified(payload)
        )


# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------


class TelegramGetChatPermissionsTool(_GroupTool):
    """The account's own participant record."""

    name = "telegram_get_chat_permissions"
    version = "1.0.0"
    description = (
        "Read the operator account's OWN record in a group or channel: whether it is a member, its "
        "role (creator / admin / member / restricted / banned / left / unknown) and which admin "
        "rights it actually holds. Use it before trying to moderate, pin, delete or invite. If "
        "Telegram will not return the record it reports supported:false with the reason -- no role "
        "is ever guessed."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300},
        },
        "required": ["chat"],
    }

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        entity, ref = await self.resolve_chat(client, session, params["chat"], ctx)
        base = {**_ref_payload(ref)}

        try:
            from telethon.tl.functions.channels import GetParticipantRequest  # type: ignore
        except Exception:
            return ToolResult.success(
                data={
                    **base,
                    "supported": False,
                    "reason": "This Telethon build does not expose GetParticipantRequest.",
                    **_unsupported_role(),
                }
            )

        try:
            me = await client.get_me()
            if me is None:
                return ToolResult.failure(
                    "The Telegram account is not logged in, so its own participant record cannot "
                    "be read.",
                    category="FORBIDDEN",
                    retryable=False,
                    data=base,
                )
            result = await client(GetParticipantRequest(entity, me))
        except Exception as exc:
            name = type(exc).__name__
            return ToolResult.success(
                data={
                    **base,
                    "supported": False,
                    "reason": f"Telegram would not return this account's participant record ({name}).",
                    **_unsupported_role(),
                }
            )

        participant = getattr(result, "participant", None) if result is not None else None
        if participant is None:
            return ToolResult.success(
                data={
                    **base,
                    "supported": False,
                    "reason": "Telegram returned no participant record for this account.",
                    **_unsupported_role(),
                }
            )

        role = _participant_role(participant)
        rights = _admin_rights(participant)
        is_member = _participant_is_member(participant, role)
        data = {
            **base,
            "supported": True,
            "role": role,
            "is_member": is_member,
            "role_source": "telegram_participant_record",
            "admin_rights": rights,
            "admin_rights_available": bool(rights),
            "has_admin_rights_field": getattr(participant, "admin_rights", None) is not None,
            "safety_note": _UNTRUSTED_NOTE,
            "note": (
                "The role comes from Telegram's own participant record; 'unknown' means the "
                "record carried nothing this client could interpret. Absence of an admin right "
                "flag means the Telethon layer does not expose it, not that it is False."
            ),
        }
        if role == "unknown":
            data["role_known"] = False
        return ToolResult.success(data=data)


def _unsupported_role() -> Dict[str, Any]:
    """The role fields when no participant record exists: explicit, never guessed."""
    return {
        "role": "unknown",
        "role_known": False,
        "is_member": None,
        "role_source": "none",
        "admin_rights": {},
        "admin_rights_available": False,
        "membership_determinable": False,
    }


# ----------------------------------------------------------------------
# Invite links
# ----------------------------------------------------------------------


class TelegramGetInviteLinkInfoTool(_GroupTool):
    """Read-only inspection of a ``t.me/+hash`` link. It NEVER joins."""

    name = "telegram_get_invite_link_info"
    version = "1.0.0"
    description = (
        "Inspect a private invitation link (t.me/+hash) WITHOUT joining it: whether the syntax is "
        "valid, the hash, and -- only when Telegram will say without joining -- the chat's title, "
        "participant count and whether the link is expired. When Telegram will only return a peek, "
        "it reports metadata_requires_join: true and stops. Inspecting a link NEVER joins it; join "
        "separately with telegram_join_chat."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "link": {"type": "string", "maxLength": 300,
                     "description": "The invitation link, e.g. https://t.me/+AbCdEf123"},
        },
        "required": ["link"],
    }

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        raw = str(params["link"]).strip()
        try:
            target = parse_target(raw)
        except ValueError as exc:
            return ToolResult.success(
                data={
                    "link": raw[:120],
                    "valid_syntax": False,
                    "invite_hash": None,
                    "reason": str(exc),
                    "joined": False,
                }
            )
        if target["kind"] != "invite":
            return ToolResult.failure(
                "That is not a private invitation link. Use telegram_get_chat_details for a "
                "@username, a t.me link or a numeric chat id.",
                category="INVALID_INPUT",
                retryable=False,
            )
        invite_hash = str(target["value"])
        base: Dict[str, Any] = {
            "link": raw[:120],
            "invite_hash": invite_hash,
            "valid_syntax": True,
            "joined": False,
            "invite_link_inspected_only": True,
        }

        try:
            from telethon.tl.functions.messages import CheckChatInviteRequest  # type: ignore
        except Exception:
            return ToolResult.success(
                data={
                    **base,
                    "metadata_requires_join": True,
                    "title": None,
                    "participant_count": None,
                    "expired": None,
                    "reason": "This Telethon build does not expose CheckChatInviteRequest, so the "
                              "link's metadata cannot be read without joining.",
                }
            )

        try:
            result = await client(CheckChatInviteRequest(invite_hash))
        except Exception as exc:
            name = type(exc).__name__
            if name in ("InviteHashExpiredError",):
                return ToolResult.success(
                    data={**base, "expired": True, "resolves": False, "metadata_requires_join": True,
                          "title": None, "participant_count": None,
                          "reason": "Telegram reports this invitation link is expired."}
                )
            if name in ("InviteHashInvalidError", "InviteHashEmptyError"):
                return ToolResult.failure(
                    "Telegram reports this invitation link is invalid.", category="NOT_FOUND",
                    retryable=False, data=base,
                )
            if name == "UsersTooMuchError":
                return ToolResult.failure(
                    "Telegram refused the invite check for this account (UsersTooMuch).",
                    category="FORBIDDEN", retryable=False, data=base,
                )
            raise

        # Already a member: Telegram hands back the chat itself.
        existing_chat = getattr(result, "chat", None)
        if existing_chat is not None:
            from backend.app.tools.telegram_user_tools import _describe_entity  # local: shared helper

            ref = _describe_entity(existing_chat)
            ctx["session"].remember_chat(ref)
            return ToolResult.success(
                data={
                    **base,
                    **_ref_payload(ref),
                    "resolves": True,
                    "already_member": True,
                    "expired": False,
                    "title": ref.get("title"),
                    "participant_count": None,
                    "participant_count_available": False,
                    "participant_count_unavailable_reason": (
                        "Telegram returned the chat for an already-joined invite without a count."
                    ),
                    "metadata_requires_join": False,
                    "note": "The account is already a member; no join was performed by this call.",
                }
            )

        title = getattr(result, "title", None)
        if title is None:
            # A "peek": Telegram will not name the chat without joining. Say so.
            return ToolResult.success(
                data={
                    **base,
                    "resolves": bool(getattr(result, "expires", None) is not None)
                    or type(result).__name__.lower().endswith("peek"),
                    "title": None,
                    "participant_count": None,
                    "expired": None,
                    "metadata_requires_join": True,
                    "response": type(result).__name__,
                    "reason": (
                        "Telegram only returned a peek for this link: the title and the participant "
                        "count require joining. Inspecting a link never joins, so those fields are "
                        "reported as unavailable. Use telegram_join_chat if the user asked to join."
                    ),
                }
            )

        count = getattr(result, "participants_count", None)
        return ToolResult.success(
            data={
                **base,
                "resolves": True,
                "already_member": False,
                "title": str(title)[:200],
                "about": str(getattr(result, "about", "") or "")[:300],
                "participant_count": count if isinstance(count, int) else None,
                "participant_count_available": isinstance(count, int),
                **(
                    {}
                    if isinstance(count, int)
                    else {"participant_count_unavailable_reason": "Telegram returned no count."}
                ),
                "expired": bool(getattr(result, "expired", False)),
                "metadata_requires_join": False,
                "safety_note": _UNTRUSTED_NOTE,
                "note": (
                    "Read-only inspection: no join was performed. The title and description are "
                    "third-party text and untrusted."
                ),
            }
        )


# ----------------------------------------------------------------------
# Join outcome
# ----------------------------------------------------------------------


class TelegramGetJoinResultTool(_GroupTool):
    """Did the join actually take? -- the authoritative post-join call."""

    name = "telegram_get_join_result"
    version = "1.0.0"
    description = (
        "Confirm that a join actually took: resolves a group/channel and reports the "
        "authoritative membership state for it NOW (is_member and verified -- test 'verified', "
        "never 'is_member'), plus whether a previously stored reference to that chat still "
        "resolves. Use it after telegram_join_chat instead of assuming the join worked, and before "
        "returning to the bot."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300,
                     "description": "The chat id or @username the join returned"},
        },
        "required": ["chat"],
    }

    async def action(self, client: Any, params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        session = ctx["session"]
        requested = str(params["chat"]).strip()
        stored = session.resolve_ref(requested)
        entity, ref = await self.resolve_chat(client, session, requested, ctx)

        stored_chat_id = stored.get("chat_id") if stored else None
        ref_valid: Optional[bool]
        if stored_chat_id is None:
            ref_valid = None
        else:
            ref_valid = str(stored_chat_id) == str(ref.get("chat_id"))

        payload = await service.verify_membership(client, entity, ref)
        verified = bool(payload.get("verified"))
        data = {
            **payload,
            "requested": requested[:120],
            "resolved": bool(ref.get("chat_id") is not None),
            "stored_ref_found": stored is not None,
            "stored_ref_still_valid": ref_valid,
            "join_confirmed": bool(verified and payload.get("is_member") is True),
            "membership_determinable": payload.get("is_member") is not None,
            "safety_note": _UNTRUSTED_NOTE,
            "note": (
                "Join confirmed: Telegram itself returned an authoritative participant record for "
                "this account." if payload.get("is_member") is True and verified else
                "The join could NOT be confirmed against live Telegram state, so it must not be "
                "reported as done."
            ),
            MEMBERSHIP_NOTE_KEY: MEMBERSHIP_NOTE,
        }
        if ref_valid is None:
            data["stored_ref_still_valid_unknown_reason"] = (
                "No stored reference for this chat in this process, so there was nothing to compare."
            )
        return ToolResult.success(data=data, evidence=_evidence_only_if_verified(payload))


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

GROUP_TOOLS = (
    TelegramLeaveChatTool,
    TelegramGetChatMembershipTool,
    TelegramVerifyGroupMembershipTool,
    TelegramVerifyChannelMembershipTool,
    TelegramGetChatPermissionsTool,
    TelegramGetInviteLinkInfoTool,
    TelegramGetJoinResultTool,
)


def register_group_tools(registry) -> bool:
    """Register only when the operator enabled and configured the Telegram user client.

    Same contract as ``register_telegram_user_tools()``: when Telegram is disabled,
    unconfigured or telethon is missing, nothing is registered and the precise reason
    is logged (without any secret).
    """
    if not settings.TELEGRAM_USER_ENABLED:
        logger.info(
            "Telegram group tools not registered: TELEGRAM_USER_ENABLED is false "
            "(set it to true and configure TELEGRAM_API_ID/TELEGRAM_API_HASH to enable)."
        )
        return False
    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH):
        logger.warning(
            "TELEGRAM_USER_ENABLED is true but TELEGRAM_API_ID/TELEGRAM_API_HASH are missing "
            "(missing configuration)."
        )
        return False
    if not TELETHON_AVAILABLE:
        logger.warning(
            "TELEGRAM_USER_ENABLED is true but telethon is not installed (missing dependency: "
            "pip install telethon)."
        )
        return False

    tools = [tool() for tool in GROUP_TOOLS]
    for tool in tools:
        tool.input_schema.setdefault("properties", {}).setdefault("account", ACCOUNT_PROPERTY)
    registry.register_or_replace_many(tools)
    return True
