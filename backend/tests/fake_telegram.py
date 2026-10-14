"""A minimal, deterministic fake of the Telethon surface the AirdropOS tools use.

Every Telegram test drives this instead of a real account. It implements ONLY the
methods the tools actually call, so a tool that reaches for something un-mocked raises
`AssertionError` rather than silently appearing to succeed.

Deliberately simple: no network, no timing, no real crypto. `GetParticipantRequest`
answers from the set of chats the fake has actually joined, which is what makes the
membership assertions meaningful -- a join that never happened cannot fake a
participant record.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.app.tools import telegram_user_tools as tg


from backend.app.tools import telegram_user_tools as tg  # noqa: F401  (documents the shape we mirror)

# The client most recently installed by the `client` fixture. Buttons record presses
# here so a test can assert "nothing was clicked" without threading the client through
# every message and button object.
ACTIVE_CLIENT: Optional["FakeClient"] = None


class _ButtonVariant:
    """A Telethon button variant (`type` discriminator), identified by its TL name."""

    def __init__(self, tl_name: str, **attrs: Any) -> None:
        self._ = tl_name
        for key, value in attrs.items():
            setattr(self, key, value)


class _UrlType(_ButtonVariant):
    def __init__(self, url: str) -> None:
        super().__init__("KeyboardButtonUrl", url=url)


class _CallbackType(_ButtonVariant):
    def __init__(self, data: bytes = b"cb-data") -> None:
        super().__init__("KeyboardButtonCallback", data=data)


class _WebViewType(_ButtonVariant):
    def __init__(self, url: str) -> None:
        super().__init__("KeyboardButtonSimpleWebView", url=url)


class _PlainType(_ButtonVariant):
    def __init__(self) -> None:
        super().__init__("KeyboardButton")


class _RawButton:
    """A stand-in for Telethon's `KeyboardButton(text, type)`.

    Uses plain attributes rather than the real TLObject so the fake cannot break on a
    Telethon refactor; `_button_kind` reads exactly these attributes.
    """

    def __init__(self, text: str, variant: _ButtonVariant) -> None:
        self.text = text
        self.type = variant


class FakeButton:
    """An inline keyboard button shaped like current Telethon: `KeyboardButton(text, type)`."""

    def __init__(self, text: str, kind: str, url: Optional[str] = None) -> None:
        self.text = text
        self.owner: Optional["FakeClient"] = None
        if kind == "callback":
            self.url = None
            self.button = _RawButton(text, _CallbackType())
        elif kind == "url":
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
        FakeClient.last_clicked = text
        if ACTIVE_CLIENT is not None:
            ACTIVE_CLIENT.last_clicked = text
        return type("Answer", (), {"message": "Accepted"})()


class FakeMessage:
    def __init__(
        self,
        msg_id: int,
        text: str,
        buttons: Optional[List[List[FakeButton]]] = None,
        out: bool = False,
    ) -> None:
        self.id = msg_id
        self.message = text
        self.buttons = buttons or []
        self.out = out
        self.date = None

    async def click(self, text: Optional[str] = None) -> Any:
        """Mirror Telethon's `Message.click(text=...)`, which presses the matching button."""
        for row in self.buttons:
            for button in row:
                if str(getattr(button, "text", "")) == str(text):
                    return await button.click(text=text)
        return type("Answer", (), {"message": None})()

class FakeChannel:
    def __init__(self, chat_id: int, title: str, username: Optional[str] = None) -> None:
        self.id = chat_id
        self.title = title
        self.username = username
        self.bot = False
        self.broadcast = False
        self.megagroup = True
        self.gigagroup = False


class FakeBot:
    def __init__(self, chat_id: int, username: str) -> None:
        self.id = chat_id
        self.title = username
        self.username = username
        self.bot = True
        self.broadcast = False
        self.megagroup = False
        self.gigagroup = False


class FakeDialog:
    def __init__(self, entity: Any, unread: int = 0) -> None:
        self.entity = entity
        self.id = entity.id
        self.unread_count = unread


class FakeClient:
    """The subset of Telethon's API that the AirdropOS Telegram tools exercise."""

    def __init__(self) -> None:
        # Instance-level, not class-level: a stale class attribute made "was anything
        # clicked?" assertions pass or fail based on a previous test's clicks.
        self.last_clicked: Optional[str] = None
        self.group = FakeChannel(-1001234567890, "Airdrop Announcements", "airdropchat")
        # A private group has NO public username: only its id can address it.
        self.private_group = FakeChannel(-1009999999999, "Private Airdrop VIP")
        self.bot = FakeBot(555000111, "AirdropOfficialBot")

        self.entities: Dict[Any, Any] = {
            "airdropchat": self.group,
            "@airdropchat": self.group,
            "AirdropOfficialBot": self.bot,
            "@AirdropOfficialBot": self.bot,
            "AbCdEf123456": self.private_group,
            self.group.id: self.group,
            self.private_group.id: self.private_group,
            self.bot.id: self.bot,
        }

        # The account starts OUTSIDE the public group so a real join is observable.
        self.joined: set = set()

        self.messages: Dict[int, List[FakeMessage]] = {
            self.bot.id: [
                FakeMessage(
                    1,
                    "Welcome! Join our channel, then press Check.",
                    buttons=[
                        [FakeButton("Join Channel", "url", "https://t.me/airdropchat")],
                        [FakeButton("Open Mini App", "webapp", "https://app.example.com/quest")],
                        [FakeButton("Check Eligibility", "callback")],
                    ],
                ),
            ],
            self.group.id: [FakeMessage(10, "Welcome to the announcements channel.")],
            self.private_group.id: [FakeMessage(20, "Welcome to the private VIP group.")],
        }
        self.next_id = 100

    # -- identity -------------------------------------------------------
    async def get_me(self) -> Any:
        return type("Me", (), {"id": 4242})()

    # -- resolution -----------------------------------------------------
    async def get_entity(self, ref: Any) -> Any:
        # Mirror Telethon: an already-resolved TL object is accepted as-is; only
        # usernames, t.me links and bare ids need a lookup.
        if hasattr(ref, "id") and hasattr(ref, "title"):
            return ref
        if ref in self.entities:
            return self.entities[ref]
        raise ValueError(f"Cannot find any entity corresponding to {ref!r}")

    # -- reading --------------------------------------------------------
    async def get_messages(self, entity: Any, ids: Any = None, limit: int = 10) -> Any:
        chat_id = getattr(entity, "id", entity)
        msgs = self.messages.get(chat_id, [])
        if ids is not None:
            return next((m for m in msgs if m.id == ids), None)
        return msgs[-limit:]

    async def iter_dialogs(self, limit: Optional[int] = None):
        for entity in (self.bot, self.group, self.private_group):
            yield FakeDialog(entity)

    # -- writing --------------------------------------------------------
    async def send_message(self, entity: Any, text: str) -> FakeMessage:
        chat_id = getattr(entity, "id", entity)
        self.next_id += 1
        sent = FakeMessage(self.next_id, text, out=True)
        self.messages.setdefault(chat_id, []).append(sent)
        if text.startswith("/start"):
            # The bot answers /start with its instructions and buttons.
            self.next_id += 1
            self.messages[chat_id].append(
                FakeMessage(
                    self.next_id,
                    "Welcome! Complete the tasks below.",
                    buttons=[
                        [FakeButton("Join Channel", "url", "https://t.me/airdropchat")],
                        [FakeButton("Open Mini App", "webapp", "https://app.example.com/quest")],
                        [FakeButton("Check Eligibility", "callback")],
                    ],
                )
            )
        return sent

    def is_connected(self) -> bool:
        return True

    # -- raw requests: JoinChannel / ImportChatInvite / GetParticipant ----
    async def __call__(self, request: Any) -> Any:
        name = type(request).__name__

        if name == "JoinChannelRequest":
            channel = getattr(request, "channel", None)
            self.joined.add(getattr(channel, "id", None))
            return type("Updates", (), {"chats": [channel], "messages": []})()

        if name == "ImportChatInviteRequest":
            self.joined.add(self.private_group.id)
            return type(
                "Updates",
                (),
                {
                    "chats": [self.private_group],
                    "messages": [
                        type(
                            "M",
                            (),
                            {
                                "peer_id": type(
                                    "P",
                                    (),
                                    {"channel_id": self.private_group.id, "chat_id": None},
                                )()
                            },
                        )()
                    ],
                },
            )()

        if name == "GetParticipantRequest":
            channel = getattr(request, "channel", None)
            if getattr(channel, "id", None) in self.joined:
                return object()  # A participant record exists.
            raise type("UserNotParticipantError", (Exception,), {})(
                "The user is not a participant of this channel"
            )

        raise AssertionError(f"Un-mocked Telegram request reached the fake client: {name}")

