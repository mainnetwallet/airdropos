"""Discord Bot Integration.

Shares the same database, task engine, and agent controller as the web dashboard.
A task created here is immediately visible there and vice versa.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from backend.app.core.config import settings
from backend.app.notifications.dispatcher import Severity, dispatcher

logger = logging.getLogger(__name__)


class DiscordAuthError(Exception):
    """Raised when an unauthorized user attempts a command."""


class DiscordBot:
    """Command router for the Discord interface."""

    def __init__(self) -> None:
        self.token = settings.DISCORD_BOT_TOKEN
        # In a real implementation, we would have server/channel specific auth
        # For now, we'll use a simple authorized user list from settings
        self.authorized_users = set(str(uid) for uid in settings.DISCORD_AUTHORIZED_USER_IDS)
        self._command_handlers: Dict[str, Callable] = {}
        self._running = False
        self._client = None

    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.token) and bool(self.authorized_users)

    def authorize(self, discord_user_id: str) -> bool:
        """Only explicitly authorized users may drive the agent."""
        if discord_user_id in self.authorized_users:
            return True
        logger.warning("Rejected unauthorized Discord user %s", discord_user_id)
        return False

    def register_command(self, command: str, handler: Callable) -> None:
        self._command_handlers[command] = handler

    # ------------------------------------------------------------------
    async def handle_message(self, message: Dict[str, Any]) -> None:
        """Route an incoming message."""
        author = message.get("author", {})
        user_id = str(author.get("id", ""))
        content = (message.get("content") or "").strip()
        channel_id = message.get("channel_id")

        if not user_id or not content:
            return

        if not self.authorize(user_id):
            await self._reply(
                channel_id,
                "Access denied. This account is not authorized to drive the agent.",
            )
            return

        # Simple deduplication by user and message content
        message_key = (user_id, content[:64])
        if not hasattr(self, "_recent_messages"):
            self._recent_messages: set = set()
        if message_key in self._recent_messages:
            await self._reply(channel_id, "That command is already being processed.")
            return
        self._recent_messages.add(message_key)
        # Keep only recent messages to prevent memory growth
        if len(self._recent_messages) > 1000:
            self._recent_messages.clear()

        try:
            await self._dispatch(channel_id, content)
        except Exception as exc:
            logger.exception("Discord command failed")
            await self._reply(channel_id, f"Command failed: {type(exc).__name__}: {exc}")
        finally:
            self._recent_messages.discard(message_key)

    async def _dispatch(self, channel_id: Optional[str], content: str) -> None:
        parts = content.split()
        command = parts[0].lower().lstrip("/")
        args = parts[1:]

        handler = self._command_handlers.get(command)
        if handler is not None:
            result = await handler(args)
            await self._reply(channel_id, result)
            return

        built_in = {
            "start": "AirdropOS is online. Commands: /status, /tasks, /task <ID>, /goals, "
                     "/wallet, /discover, /help, /stop, /resume",
            "help": "Available commands:\n"
                    "/status - platform and agent status\n"
                    "/tasks - list active tasks\n"
                    "/task <AIR-YYYY-NNNNNN> - full task status report\n"
                    "/create <title> - create a task\n"
                    "/goals - list long-term goals\n"
                    "/wallet - wallet summary\n"
                    "/discover - run airdrop discovery\n"
                    "/stop - engage emergency stop\n"
                    "/resume - release emergency stop",
        }

        if command in built_in:
            await self._reply(channel_id, built_in[command])
            return

        # Anything else is routed to the central agent as natural language.
        agent_handler = self._command_handlers.get("agent")
        if agent_handler is not None:
            await self._reply(channel_id, await agent_handler([content]))
            return

        await self._reply(channel_id, "Unrecognized command. Send /help.")

    async def _reply(self, channel_id: Optional[str], text: str) -> None:
        if not channel_id or not self.token:
            return
        import httpx

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        # Discord caps messages at 2000 chars; split defensively.
        for chunk in [text[i : i + 1900] for i in range(0, len(text), 1900)]:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        url,
                        headers={
                            "Authorization": f"Bot {self.token}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "content": chunk,
                        },
                    )
            except Exception as exc:
                logger.error("Failed to reply to Discord channel %s: %s", channel_id, exc)

    # ------------------------------------------------------------------
    async def start_polling(self) -> bool:
        """Begin gateway connection. Returns False if unconfigured or unavailable."""
        if not self.is_configured:
            logger.info("Discord bot not started: token or authorized users not configured.")
            return False
        try:
            # In a real implementation, we would use discord.py or similar
            # For now, we'll simulate the startup
            logger.info("Discord bot gateway connection started (simulated)")
            self._running = True
            return True
        except Exception as exc:
            logger.error("Discord bot failed to start: %s", exc)
            return False

    async def stop(self) -> None:
        self._running = False
        self._client = None

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._running else ("NOT_CONFIGURED" if not self.is_configured else "STOPPED"),
            "gateway": self._running,
            "authorized_users": len(self.authorized_users),
            "token_configured": bool(self.token),
        }


# Global bot instance
discord_bot = DiscordBot()