"""Shared types for Live Broadcast."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SessionStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Capabilities:
    """What an integration can REALLY do. The UI only shows controls that are true here."""

    visual: bool = False          # can stream a real picture of the session
    events: bool = True           # emits activity events
    pause_resume: bool = False
    stop: bool = False
    manual_control: bool = False  # operator can click / type into the real session
    keyboard: bool = False        # text + key input in addition to clicks
    chat: bool = False            # message-based session: chat list, history, send, press buttons
    metadata: bool = True

    def as_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass
class IntegrationDescriptor:
    id: str
    label: str
    capabilities: Capabilities
    available: bool = True        # a provider that can list sessions exists
    configured: Optional[bool] = None
    note: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = self.capabilities.as_dict()
        return data


@dataclass
class Frame:
    data: bytes
    mime: str
    width: int           # CSS-pixel viewport the picture represents
    height: int
    frame_id: str
    captured_at: float   # unix seconds


class NotSupported(Exception):
    """The integration does not implement the requested operation."""


class SessionNotFound(Exception):
    pass


class Conflict(Exception):
    """State conflict (control lock held, wrong state, stale input)."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ActionResult:
    ok: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)
