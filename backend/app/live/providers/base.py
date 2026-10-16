"""Provider contract. One provider per integration; the gateway and UI are generic over it."""

from __future__ import annotations

from typing import Any, Dict, List

from backend.app.live.models import (
    Capabilities,
    Frame,
    IntegrationDescriptor,
    NotSupported,
    SessionNotFound,
)


class SessionProvider:
    integration: str = ""
    label: str = ""
    capabilities: Capabilities = Capabilities(events=False, metadata=False)
    note: str | None = None

    def descriptor(self) -> IntegrationDescriptor:
        return IntegrationDescriptor(
            id=self.integration,
            label=self.label,
            capabilities=self.capabilities,
            available=True,
            configured=self.configured(),
            note=self.note,
        )

    def configured(self) -> bool | None:
        return None

    async def list_sessions(self) -> List[Dict[str, Any]]:
        return []

    async def get_session(self, key: str) -> Dict[str, Any]:
        for session in await self.list_sessions():
            if session["key"] == key:
                return session
        raise SessionNotFound(key)

    async def frame(self, key: str, quality: str) -> Frame:
        raise NotSupported(f"{self.label} sessions have no visual screen.")

    async def action(self, key: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotSupported(f"'{action}' is not supported for {self.label}.")

    async def input(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotSupported(f"Manual control is not supported for {self.label}.")

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotSupported(f"Sessions cannot be started from the dashboard for {self.label}.")

    async def chat(self, key: str, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotSupported(f"{self.label} sessions have no chat view.")

    async def media(self, key: str, query: Dict[str, Any]):
        raise NotSupported(f"{self.label} sessions have no attachments.")
