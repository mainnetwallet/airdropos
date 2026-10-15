"""A minimal, deterministic fake of the Telethon surface the AirdropOS tools use.

Every Telegram test drives this instead of a real account. It implements ONLY the
methods the tools actually call, so a tool that reaches for something un-mocked raises
`AssertionError` rather than silently appearing to succeed.

Deliberately simple: no network, no timing, no real crypto. `GetParticipantRequest`
answers from the set of chats the fake has actually joined, which is what makes the
membership assertions meaningful -- a join that never happened cannot fake a
participant record.

Entities are real TL objects (or duck-typed equivalents)
---------------------------------------------------------
The tools build *real* Telethon requests and hand them to ``await client(request)``
(``JoinChannelRequest(entity)``, ``LeaveChannelRequest(entity)``, ...). Those requests
declare their arguments as TL types -- ``JoinChannelRequest.channel`` is
``TypeInputChannel`` -- and every TL layer is free to enforce that at construction
time, so a duck-typed stand-in can fail *before* the request ever reaches this fake's
``__call__`` and the test then fails with an unrelated error. Two things prevent that:

* ``FakeChannel`` subclasses BOTH ``telethon.tl.types.Channel`` (what
  ``get_entity()`` really returns) and ``telethon.tl.types.InputChannel`` (what the
  requests take), so ``isinstance`` succeeds under either check and under any layer that
  makes one a subclass of the other. ``FakeUser`` does the same with
  ``InputPeerUser``/``User``.
* If Telethon is not importable at all, the base classes degrade to plain objects
  (see ``_TL_AVAILABLE``). The fake still mirrors the attribute shape, which is all the
  tools read.

Measured, not assumed: in the Telethon this repo pins (1.45.0) the generated request
constructors do NOT type-check their arguments -- ``JoinChannelRequest.__init__`` is
just ``self.channel = channel`` -- so an old-style duck-typed entity happened to
survive construction. That is a property of one layer, not a guarantee, which is why the
fake now satisfies the declared types instead of relying on it.

Requests are still intercepted in ``__call__`` (rather than by patching the tools'
client construction) because the alternative -- a seam that swallows requests before
they are built -- would mean changing production code, and would let a request the fake
cannot answer look like a silent success.

Naming: every request is dispatched on ``type(request).__name__``, so the fake does not
import the request classes and cannot break when one moves namespace. Errors are raised
as classes with the SAME names Telethon uses, because production matches on
``type(exc).__name__`` (``UserNotParticipantError`` is how "not a member" is reported).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# -- Telethon base classes ------------------------------------------------------
# The real TL types when Telethon is installed, plain objects when it is not. The fake
# must be importable either way: a test that only wants `FakeButton` should not explode
# because the optional dependency is missing.
try:  # pragma: no cover - exercised implicitly by the import itself
    from telethon.tl.types import Channel as _TLChannelFull
    from telethon.tl.types import InputChannel as _TLChannelInput
    from telethon.tl.types import InputPeerUser as _TLUserInput
    from telethon.tl.types import User as _TLUserFull

    _TL_AVAILABLE = True
    # A resolved entity is a FULL Channel, but the requests take an INPUT channel.
    # Whether one TL type satisfies the other's isinstance check depends on the layer,
    # so a resolved channel is a subclass of both and satisfies either check.
    _CHANNEL_BASES = (
        (_TLChannelFull,) if issubclass(_TLChannelInput, _TLChannelFull)
        else (_TLChannelFull, _TLChannelInput)
    )
    _USER_BASES = (
        (_TLUserInput,) if issubclass(_TLUserFull, _TLUserInput)
        else (_TLUserInput, _TLUserFull)
    )
except Exception:  # pragma: no cover - telethon missing
    _TL_AVAILABLE = False

    class _TLChannelFull:  # type: ignore[no-redef]
        """Stand-in for ``telethon.tl.types.Channel`` when Telethon is absent."""

    class _TLChannelInput(_TLChannelFull):  # type: ignore[no-redef]
        """Stand-in for ``telethon.tl.types.InputChannel``."""

    class _TLUserFull:  # type: ignore[no-redef]
        """Stand-in for ``telethon.tl.types.User`` when Telethon is absent."""

    class _TLUserInput(_TLUserFull):  # type: ignore[no-redef]
        """Stand-in for ``telethon.tl.types.InputPeerUser``."""

    _CHANNEL_BASES = (_TLChannelFull,)
    _USER_BASES = (_TLUserInput,)


# The client most recently installed by the `client` fixture. Buttons record presses
# here so a test can assert "nothing was clicked" without threading the client through
# every message and button object.
ACTIVE_CLIENT: Optional["FakeClient"] = None


# -- Telegram errors ------------------------------------------------------------
# Named exactly as Telethon names them: production branches on the class NAME.


class UserNotParticipantError(Exception):
    """The account is not a member of this chat."""


class UserAlreadyParticipantError(Exception):
    """The account is already a member of this chat."""


class InviteHashInvalidError(Exception):
    """No chat matches this invitation hash."""


class InviteHashExpiredError(Exception):
    """This invitation link has expired."""


class ChannelPrivateError(Exception):
    """Telegram will not disclose this channel."""


class MessageIdInvalidError(Exception):
    """No message with that id exists in this chat."""


class BotCommandsEmptyError(Exception):
    """This bot publishes no commands in the requested scope."""


# -- Button plumbing ------------------------------------------------------------


class _CallbackData:
    """Mirror of Telethon's ``CallbackData`` wrapper: bytes with a readable repr."""

    def __init__(self, data: bytes = b"cb-data") -> None:
        self.data = bytes(data)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _CallbackData) and other.data == self.data

    def __hash__(self) -> int:
        return hash(self.data)

    def __repr__(self) -> str:
        return f"CallbackData(data={self.data!r})"

    __str__ = __repr__


class _ButtonVariant:
    """A Telethon button variant (the ``type`` discriminator), identified by its TL name.

    Duck-typed on purpose: the class that actually carries ``data``/``url`` has moved
    namespace between Telethon layers, and ``_button_kind`` resolves the variant by
    NAME for exactly that reason. ``_`` carries the name the layer is expected to use.
    """

    def __init__(self, tl_name: str, **attrs: Any) -> None:
        self._ = tl_name
        for key, value in attrs.items():
            setattr(self, key, value)


class _UrlType(_ButtonVariant):
    def __init__(self, url: str) -> None:
        super().__init__("KeyboardButtonUrl", url=url)


class _CallbackType(_ButtonVariant):
    def __init__(self, data: bytes = b"cb-data") -> None:
        super().__init__("KeyboardButtonCallback", data=bytes(data))


class _WebViewType(_ButtonVariant):
    def __init__(self, url: str) -> None:
        super().__init__("KeyboardButtonSimpleWebView", url=url)


class _PlainType(_ButtonVariant):
    def __init__(self) -> None:
        super().__init__("KeyboardButton")


class _RawButton:
    """A stand-in for Telethon's raw keyboard button: ``(text, type)``.

    Uses plain attributes rather than the real TLObject so the fake cannot break on a
    Telethon refactor; `_button_kind` reads exactly these attributes.
    """

    def __init__(self, text: str, variant: _ButtonVariant) -> None:
        self.text = text
        self.type = variant


class FakeButton:
    """An inline keyboard button shaped like current Telethon: ``KeyboardButton(text, type)``.

    ``data`` is where the callback payload lives, exactly as Telethon's own button
    wrapper puts it, so ``telegram_service.callback_ref()`` finds it on its first probe
    and returns a real ``cb_...`` handle instead of ``None``.
    """

    def __init__(
        self,
        text: str,
        kind: str,
        url: Optional[str] = None,
        data: Optional[bytes] = None,
    ) -> None:
        self.text = text
        if kind == "callback":
            self.url = None
            # Default payload is derived from the label so two different callback
            # buttons in one message have two different refs, as they would in a real
            # bot's keyboard.
            payload = data if data is not None else str(text).encode("utf-8")
            self.data: Optional[_CallbackData] = _CallbackData(payload)
            self.button = _RawButton(text, _CallbackType(payload))
        else:
            self.data = None
            if kind == "url":
                self.url = url or "https://example.com"
                self.button = _RawButton(text, _UrlType(self.url))
            elif kind == "webapp":
                self.url = url or "https://example.com/miniapp"
                self.button = _RawButton(text, _WebViewType(self.url))
            elif kind == "text":
                self.url = None
                self.button = _RawButton(text, _PlainType())
            else:
                # payment / phone / location: a variant the agent must refuse to press.
                self.url = None
                self.button = _RawButton(text, _ButtonVariant(f"KeyboardButton{kind}"))

    async def click(self, text: Optional[str] = None) -> Any:
        """Record the press on the *installed* client, never on the class."""
        client = ACTIVE_CLIENT
        if client is not None:
            client.last_clicked = text if text is not None else self.text
        return type("Answer", (), {"message": "Accepted"})()


# -- Messages and entities ------------------------------------------------------


class _ReplyTo:
    """The ``reply_to`` header of a message (``reply_to_msg_id`` is what tools read)."""

    def __init__(self, reply_to_msg_id: int, reply_to_peer_id: Optional[int] = None) -> None:
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to_peer_id = reply_to_peer_id


class FakeTextUrlEntity:
    """A formatting entity that carries a URL, as a bot writes in its instructions.

    ``telegram_service.describe_entities`` reads exactly ``_``, ``url``, ``offset`` and
    ``length``, so anything with those attributes works; this is the convenient form.
    """

    def __init__(
        self, url: str, offset: int, length: int, tl_name: str = "MessageEntityTextUrl"
    ) -> None:
        self._ = tl_name
        self.url = url
        self.offset = offset
        self.length = length


class FakeMessage:
    """One message. Carries the attributes the describe/edit paths read.

    ``reply_to``, ``sender_id``, ``edit_date``, ``media``, ``photo``, ``document`` and
    ``entities`` exist because ``telegram_service.describe_messages`` and
    ``describe_entities`` touch them: an edit must be visible, a reply must be
    traceable, and formatting entities must survive into the payload.
    """

    def __init__(
        self,
        msg_id: int,
        text: str,
        buttons: Optional[List[List["FakeButton"]]] = None,
        out: bool = False,
        *,
        sender_id: Optional[int] = None,
        edit_date: Optional[datetime] = None,
        reply_to: Optional[_ReplyTo] = None,
        entities: Optional[List[Any]] = None,
        media: Any = None,
        photo: Any = None,
        document: Any = None,
        date: Optional[datetime] = None,
    ) -> None:
        self.id = msg_id
        self.message = text
        self.buttons = buttons or []
        self.out = out
        self.date = date if date is not None else datetime.now(timezone.utc)
        self.sender_id = sender_id
        self.edit_date = edit_date
        # Accept a message (or a bare id) as well as a header: a test should not have
        # to construct the wrapper to describe a reply.
        if isinstance(reply_to, FakeMessage):
            self.reply_to: Optional[_ReplyTo] = _ReplyTo(reply_to.id)
        elif isinstance(reply_to, int):
            self.reply_to = _ReplyTo(reply_to)
        else:
            self.reply_to = reply_to
        self.entities = entities or []
        self.media = media
        self.photo = photo
        self.document = document

    @property
    def text(self) -> str:
        """Telethon exposes the body as both ``message`` and ``text``."""
        return self.message

    async def click(self, text: Optional[str] = None) -> Any:
        """Mirror Telethon's `Message.click(text=...)`, which presses the matching button."""
        for row in self.buttons:
            for button in row:
                if str(getattr(button, "text", "")) == str(text):
                    return await button.click(text=text)
        return type("Answer", (), {"message": None})()

    def apply_edit(
        self,
        text: Optional[str] = None,
        *,
        entities: Optional[List[Any]] = None,
        edit_date: Optional[datetime] = None,
    ) -> None:
        """Rewrite in place, the way a bot that revises its instructions does."""
        if text is not None:
            self.message = text
        if entities is not None:
            self.entities = list(entities)
        self.edit_date = edit_date or datetime.now(timezone.utc)


class FakeChannel(*_CHANNEL_BASES):
    """A broadcast channel, a supergroup or a basic group.

    Defaults to a basic group (neither ``broadcast`` nor ``megagroup``); pass
    ``broadcast=True`` for a channel and ``megagroup=True`` for a supergroup, because
    the membership tools report a channel and a group differently and each kind needs
    one of its own to be testable.
    """

    def __init__(
        self,
        chat_id: int,
        title: str,
        username: Optional[str] = None,
        *,
        broadcast: bool = False,
        megagroup: bool = False,
        gigagroup: bool = False,
        access_hash: int = 0,
    ) -> None:
        self._ = "Channel"
        self.id = chat_id
        self.title = title
        self.username = username
        self.first_name = None
        self.bot = False
        self.broadcast = broadcast
        self.megagroup = megagroup
        self.gigagroup = gigagroup
        self.verified = False
        self.scam = False
        self.fake = False
        self.restricted = False
        # Present so nothing that reads it ever sees a missing attribute; never
        # surfaced by `_describe_entity`, which deliberately excludes it.
        self.access_hash = access_hash


class FakeBot(FakeChannel):
    """A bot chat. Bots are ``Channel`` objects too, flagged ``bot``."""

    def __init__(self, chat_id: int, username: str, first_name: str = "Airdrop") -> None:
        # Deliberately FakeChannel's own __init__, NOT super(): Telethon's Channel takes
        # a dozen required TL arguments this fake does not have, and the tools read plain
        # attributes only.
        FakeChannel.__init__(self, chat_id, first_name, username=username)
        self.bot = True


class FakeUser(*_USER_BASES):
    """The logged-in account, as ``get_me()`` returns it.

    Subclasses ``InputPeerUser`` so it also satisfies the requests that want an input
    peer (``GetParticipantRequest(entity, me)``).
    """

    def __init__(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: str = "Airdrop",
        last_name: str = "Tester",
    ) -> None:
        self._ = "InputPeerUser"
        self.user_id = user_id
        self.id = user_id
        self.access_hash = 0
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.title = None
        self.bot = False
        self.phone = None


class FakeDialog:
    def __init__(self, entity: Any, unread: int = 0) -> None:
        self.entity = entity
        self.id = entity.id
        self.unread_count = unread


# -- Small TL-ish results -------------------------------------------------------


class _Participant:
    """A ``ChannelParticipant``-shaped record: what the account's own row looks like.

    ``admin=True`` gives it an ``admin_rights`` object, which is how a test reaches the
    "role: admin" and rights paths in ``telegram_get_chat_permissions``.
    """

    def __init__(self, user: Any, *, admin: bool = False) -> None:
        self._ = "ChannelParticipant"
        self.user = user
        self.date = datetime.now(timezone.utc)
        self.admin_rights = _AdminRights() if admin else None
        self.banned_rights = None


class _AdminRights:
    """The admin-right flags a real record carries."""

    def __init__(self, **granted: bool) -> None:
        self.change_info = granted.get("change_info", True)
        self.post_messages = granted.get("post_messages", True)
        self.edit_messages = granted.get("edit_messages", True)
        self.delete_messages = granted.get("delete_messages", True)
        self.ban_users = granted.get("ban_users", False)
        self.invite_users = granted.get("invite_users", False)
        self.pin_messages = granted.get("pin_messages", True)
        self.manage_call = granted.get("manage_call", False)
        self.manage_topics = granted.get("manage_topics", False)
        self.manage_direct_messages = granted.get("manage_direct_messages", False)


class _ChatInvite:
    """``messages.ChatInvite`` -- the read-only peek an invite link returns."""

    def __init__(self, title: str, about: str, participants_count: int) -> None:
        self._ = "ChatInvite"
        self.title = title
        self.about = about
        self.participants_count = participants_count
        self.expired = False
        self.requests_pending = False
        self.schannel = None


class _ChatInviteAlready:
    """``messages.ChatInviteAlready`` -- Telegram hands back the chat for a member."""

    def __init__(self, chat: Any) -> None:
        self._ = "ChatInviteAlready"
        self.chat = chat
        self.title = getattr(chat, "title", None)
        self.participants_count = None
        self.expired = False


class _BotCommand:
    def __init__(self, command: str, description: str = "") -> None:
        self._ = "BotCommand"
        self.command = command
        self.description = description


class _MenuButton:
    """A bot menu button (``BotMenuButtonCommands`` shape)."""

    def __init__(self, commands: List[str], text: Optional[str] = None, url: Optional[str] = None) -> None:
        self._ = "MenuButtonCommands"
        self.text = text
        self.url = url
        self.commands = list(commands)


class _FullChat:
    def __init__(self, participants_count: int) -> None:
        self._ = "ChannelFull"
        self.participants_count = participants_count
        self.about = "A fake chat maintained by the AirdropOS test double."


# -- The client -----------------------------------------------------------------


class FakeClient:
    """The subset of Telethon's API that the AirdropOS Telegram tools exercise."""

    def __init__(self) -> None:
        # Instance-level, not class-level: a stale class attribute made "was anything
        # clicked?" assertions pass or fail based on a previous test's clicks.
        self.last_clicked: Optional[str] = None

        # A supergroup and a real broadcast channel, so `verify_group_membership` and
        # `verify_channel_membership` each have the right entity kind to be tested on.
        self.group = FakeChannel(-1001234567890, "Airdrop Announcements", "airdropchat", megagroup=True)
        self.channel = FakeChannel(-1002234567890, "Airdrop Channel", "airdropchannel", broadcast=True)
        # A private group has NO public username: only its id can address it.
        self.private_group = FakeChannel(-1009999999999, "Private Airdrop VIP", megagroup=True)
        self.bot = FakeBot(555000111, "AirdropOfficialBot")
        self.me = FakeUser(4242, "airdropos_tester")

        self.entities: Dict[Any, Any] = {
            "airdropchat": self.group,
            "@airdropchat": self.group,
            "airdropchannel": self.channel,
            "@airdropchannel": self.channel,
            "AirdropOfficialBot": self.bot,
            "@AirdropOfficialBot": self.bot,
            self.group.id: self.group,
            self.channel.id: self.channel,
            self.private_group.id: self.private_group,
            self.bot.id: self.bot,
            self.me.id: self.me,
        }

        # The account starts OUTSIDE every group/channel so a real join is observable.
        self.joined: set = set()

        #: invite hash -> the private chat it opens. An invite hash is NOT resolvable
        #: through get_entity (that is what makes it useless for reading messages), so
        #: it deliberately does not appear in ``entities``.
        self.invites: Dict[str, Any] = {"AbCdEf123456": self.private_group}
        #: hashes Telegram reports as expired, for the invite-inspection path.
        self.expired_invites: set = {"ExPiReD01"}

        self.messages: Dict[int, List[FakeMessage]] = {
            self.bot.id: [
                FakeMessage(
                    1,
                    "Welcome! Join our channel, then press Check.",
                    sender_id=self.bot.id,
                    buttons=[
                        [FakeButton("Join Channel", "url", "https://t.me/airdropchat")],
                        [FakeButton("Open Mini App", "webapp", "https://app.example.com/quest")],
                        [FakeButton("Check Eligibility", "callback")],
                    ],
                ),
            ],
            self.group.id: [FakeMessage(10, "Welcome to the announcements channel.",
                                        sender_id=self.me.id)],
            self.channel.id: [FakeMessage(30, "Channel announcement.", sender_id=self.me.id)],
            self.private_group.id: [FakeMessage(20, "Welcome to the private VIP group.",
                                                sender_id=self.me.id)],
        }
        self.next_id = 100

        #: What a bot menu read answers with, per chat id. Set to ``None`` to model a
        #: bot that has published no menu.
        self.bot_menu: Dict[int, Any] = {
            self.bot.id: _MenuButton(["start", "tasks", "claim"])
        }
        #: Bot commands, per chat id.
        self.bot_commands: Dict[int, List[_BotCommand]] = {
            self.bot.id: [
                _BotCommand("start", "Start the bot"),
                _BotCommand("tasks", "Show the task list"),
                _BotCommand("claim", "Claim your reward"),
            ]
        }
        #: Whether the account's participant record carries admin rights. Flip it to
        #: exercise the admin/role paths of telegram_get_chat_permissions.
        self.account_is_admin: bool = False

        #: Member/subscriber counts the full-channel read reports.
        self.participants_count: Dict[int, int] = {
            self.group.id: 1234,
            self.channel.id: 56789,
            self.private_group.id: 42,
        }

        #: Every raw request this client was asked to perform, by TL name.
        self.requests: List[str] = []
        #: Every ``get_messages`` keyword set, so paging can be asserted.
        self.get_messages_calls: List[Dict[str, Any]] = []

        self._connected = True
        self._authorized = True

    # -- connection state ------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> bool:
        self._connected = False
        return True

    async def is_user_authorized(self) -> bool:
        """What ``_TelegramUserSession._connect()`` asks before handing the client over.

        Without it the fake cannot stand in for a connected account at all: every tool
        would come back NEEDS_USER_ACTION with the login instructions.
        """
        return self._authorized

    # -- identity --------------------------------------------------------
    async def get_me(self) -> Any:
        return self.me if self._authorized else None

    # -- resolution ------------------------------------------------------
    async def get_entity(self, ref: Any) -> Any:
        # Mirror Telethon: an already-resolved TL object is accepted as-is; only
        # usernames, t.me links and bare ids need a lookup.
        if hasattr(ref, "id") and (hasattr(ref, "title") or hasattr(ref, "first_name")):
            return ref
        try:
            key = ref
            if hasattr(key, "id"):
                key = key.id
            if key in self.entities:
                return self.entities[key]
        except TypeError:
            pass
        raise ValueError(f"Cannot find any entity corresponding to {ref!r}")

    # -- reading ---------------------------------------------------------
    async def get_messages(
        self,
        entity: Any,
        ids: Any = None,
        limit: int = 10,
        *,
        min_id: Optional[int] = None,
        max_id: Optional[int] = None,
        search: Optional[str] = None,
        offset_id: Optional[int] = None,
    ) -> Any:
        """One page of history, newest FIRST -- the order Telethon returns.

        Paging keywords follow Telethon: ``min_id`` is EXCLUSIVE, ``max_id`` is
        INCLUSIVE, ``offset_id`` is inclusive like ``max_id``. ``ids=`` returns a single
        message (or a list for a list of ids), as the real client does.
        """
        chat_id = getattr(entity, "id", entity)
        msgs: List[FakeMessage] = list(self.messages.get(chat_id, []))
        self.get_messages_calls.append(
            {
                "chat_id": chat_id,
                "ids": ids,
                "limit": limit,
                "min_id": min_id,
                "max_id": max_id,
                "search": search,
                "offset_id": offset_id,
            }
        )
        if ids is not None:
            if isinstance(ids, (list, tuple)):
                return [next((m for m in msgs if m.id == i), None) for i in ids]
            return next((m for m in msgs if m.id == ids), None)
        if search:
            needle = str(search).lower()
            msgs = [m for m in msgs if needle in (m.message or "").lower()]
        if min_id is not None:
            msgs = [m for m in msgs if m.id > int(min_id)]
        if max_id is not None:
            msgs = [m for m in msgs if m.id <= int(max_id)]
        if offset_id is not None:
            msgs = [m for m in msgs if m.id <= int(offset_id)]
        msgs.sort(key=lambda m: m.id, reverse=True)
        return msgs[:limit]

    async def iter_dialogs(self, limit: Optional[int] = None):
        for entity in (self.bot, self.group, self.channel, self.private_group):
            yield FakeDialog(entity)

    # -- writing ---------------------------------------------------------
    async def send_message(
        self, entity: Any, text: str, reply_to: Optional[int] = None
    ) -> FakeMessage:
        chat_id = getattr(entity, "id", entity)
        self.next_id += 1
        sent = FakeMessage(
            self.next_id,
            text,
            out=True,
            sender_id=self.me.id,
            reply_to=reply_to,
        )
        self.messages.setdefault(chat_id, []).append(sent)
        if text.startswith("/start"):
            # The bot answers /start with its instructions and buttons.
            self.next_id += 1
            self.messages[chat_id].append(
                FakeMessage(
                    self.next_id,
                    "Welcome! Complete the tasks below.",
                    sender_id=self.bot.id,
                    buttons=[
                        [FakeButton("Join Channel", "url", "https://t.me/airdropchat")],
                        [FakeButton("Open Mini App", "webapp", "https://app.example.com/quest")],
                        [FakeButton("Check Eligibility", "callback")],
                    ],
                )
            )
        return sent

    # -- raw requests ----------------------------------------------------
    async def __call__(self, request: Any) -> Any:
        name = type(request).__name__
        self.requests.append(name)

        if name == "JoinChannelRequest":
            channel = getattr(request, "channel", None)
            chat_id = getattr(channel, "id", None)
            if chat_id is None:
                raise ChannelPrivateError("The channel could not be resolved.")
            if chat_id in self.joined:
                raise UserAlreadyParticipantError("The user is already a participant")
            self.joined.add(chat_id)
            return type("Updates", (), {"chats": [channel], "messages": []})()

        if name == "LeaveChannelRequest":
            channel = getattr(request, "channel", None)
            chat_id = getattr(channel, "id", None)
            if chat_id not in self.joined:
                raise UserNotParticipantError("The user is not a participant of this channel")
            # Leaving really removes membership: a later participant read must fail.
            self.joined.discard(chat_id)
            return type("Updates", (), {"chats": [channel], "messages": []})()

        if name == "ImportChatInviteRequest":
            invite_hash = str(getattr(request, "hash", ""))
            chat = self._invite_chat(invite_hash)
            if chat.id in self.joined:
                raise UserAlreadyParticipantError("The user is already a participant")
            self.joined.add(chat.id)
            return type(
                "Updates",
                (),
                {
                    "chats": [chat],
                    "messages": [
                        type(
                            "M",
                            (),
                            {
                                "peer_id": type(
                                    "P",
                                    (),
                                    {"channel_id": chat.id, "chat_id": None},
                                )()
                            },
                        )()
                    ],
                },
            )()

        if name == "CheckChatInviteRequest":
            # Read-only inspection: NEVER joins, exactly like the real request.
            chat = self._invite_chat(str(getattr(request, "hash", "")))
            if chat.id in self.joined:
                return _ChatInviteAlready(chat)
            return _ChatInvite(
                chat.title or chat.username or "Private chat",
                "Joined through a private invitation link.",
                self.participants_count.get(chat.id, 0),
            )

        if name == "GetParticipantRequest":
            channel = getattr(request, "channel", None)
            if getattr(channel, "id", None) in self.joined:
                # Real shape: the wrapper carries `.participant`, which is what the
                # chat-permissions tool reads to derive a role.
                return type(
                    "ChannelParticipant",
                    (),
                    {
                        "participant": _Participant(
                            getattr(request, "participant", None),
                            admin=self.account_is_admin,
                        )
                    },
                )()
            raise UserNotParticipantError("The user is not a participant of this channel")

        if name == "GetFullChannelRequest":
            # Channels and supergroups only: a basic group would need
            # messages.GetFullChatRequest, which no tool calls.
            channel = getattr(request, "channel", None)
            chat_id = getattr(channel, "id", None)
            count = self.participants_count.get(chat_id)
            if count is None:
                raise ChannelPrivateError("The channel is private.")
            return type("ChannelFull", (), {"full_chat": _FullChat(count)})()

        if name == "GetBotCommandsRequest":
            # The DEFAULT scope carries no peer: Telegram resolves "my commands" from
            # the session, not from the request. The fake has exactly one bot, so the
            # default scope answers for it.
            scope = getattr(request, "scope", None)
            peer = getattr(scope, "peer", None)
            chat_id = (
                getattr(peer, "user_id", None)
                or getattr(peer, "channel_id", None)
                or self.bot.id
            )
            commands = self.bot_commands.get(chat_id)
            if commands is None:
                raise BotCommandsEmptyError()
            return type("BotCommands", (), {"commands": list(commands)})()

        if name == "GetBotMenuButtonRequest":
            # Telethon names this argument `user_id`, other layers `peer`; the caller
            # passes either the bot entity or its bare id.
            peer = getattr(request, "user_id", None) or getattr(request, "peer", None)
            chat_id = (
                peer if isinstance(peer, int)
                else getattr(peer, "id", None) or getattr(peer, "channel_id", None)
            )
            return type("BotMenuButton", (), {"button": self.bot_menu.get(chat_id)})()

        if name == "EditMessageRequest":
            # No tool issues this today, but a bot rewriting its instructions in place
            # is exactly what `describe_messages`' edit state exists to surface.
            peer = getattr(request, "peer", None)
            chat_id = getattr(peer, "channel_id", None) or getattr(peer, "user_id", None)
            msgs = self.messages.get(chat_id, [])
            target = next((m for m in msgs if m.id == getattr(request, "id", None)), None)
            if target is None:
                raise MessageIdInvalidError("No message with that id exists in this chat")
            target.apply_edit(
                getattr(request, "message", None),
                entities=getattr(request, "entities", None),
                edit_date=datetime.now(timezone.utc),
            )
            return target

        raise AssertionError(f"Un-mocked Telegram request reached the fake client: {name}")

    def _invite_chat(self, invite_hash: str) -> Any:
        """Resolve an invite hash, or raise the error Telegram would raise."""
        if invite_hash in self.expired_invites:
            raise InviteHashExpiredError("The invitation link has expired")
        chat = self.invites.get(invite_hash)
        if chat is None:
            raise InviteHashInvalidError("The invitation link is invalid")
        return chat

    # -- test helpers ----------------------------------------------------

    def add_message(self, chat: Any, text: str, **kwargs: Any) -> FakeMessage:
        """Append a message (default: one second older than the newest) to a chat."""
        chat_id = getattr(chat, "id", chat)
        msgs = self.messages.setdefault(chat_id, [])
        newest = max((m.id for m in msgs), default=0)
        msg = FakeMessage(
            kwargs.pop("msg_id", newest + 1),
            text,
            date=kwargs.pop("date", datetime.now(timezone.utc) - timedelta(seconds=len(msgs) + 1)),
            sender_id=kwargs.pop("sender_id", self.bot.id),
            **kwargs,
        )
        msgs.append(msg)
        msgs.sort(key=lambda m: m.id)
        return msg

    def join(self, chat: Any) -> Any:
        """Put the account in a chat without going through a request (setup only)."""
        entity = chat if isinstance(chat, (FakeChannel, FakeBot)) else self.entities.get(chat, chat)
        self.joined.add(entity.id)
        return entity