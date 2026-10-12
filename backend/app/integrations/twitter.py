"""Twitter / X Integration.

Shares the same database, task engine, and agent controller as the web dashboard.
A task created here is immediately visible there and vice versa.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from backend.app.core.config import settings
from backend.app.notifications.dispatcher import Severity, dispatcher

logger = logging.getLogger(__name__)


class TwitterAuthError(Exception):
    """Raised when an unauthorized user attempts a command."""


class TwitterBot:
    """Command router for the Twitter/X interface via DMs and mentions."""

    def __init__(self) -> None:
        self.bearer_token = settings.TWITTER_BEARER_TOKEN
        self.api_key = settings.TWITTER_API_KEY
        self.api_secret = settings.TWITTER_API_SECRET
        self.access_token = settings.TWITTER_ACCESS_TOKEN
        self.access_secret = settings.TWITTER_ACCESS_SECRET
        # Authorized user IDs (Twitter numeric IDs)
        self.authorized_users = set(str(uid) for uid in settings.TWITTER_AUTHORIZED_USER_IDS)
        self._command_handlers: Dict[str, Callable] = {}
        self._running = False
        self._client = None

    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        # For now, we consider it configured if we have bearer token (for API v2)
        # In a full implementation, we'd check all required credentials
        return bool(self.bearer_token) and bool(self.authorized_users)

    def authorize(self, twitter_user_id: str) -> bool:
        """Only explicitly authorized users may drive the agent."""
        if twitter_user_id in self.authorized_users:
            return True
        logger.warning("Rejected unauthorized Twitter user %s", twitter_user_id)
        return False

    def register_command(self, command: str, handler: Callable) -> None:
        self._command_handlers[command] = handler

    # ------------------------------------------------------------------
    async def handle_message(self, message: Dict[str, Any]) -> None:
        """Route an incoming DM or mention."""
        # Extract sender ID and message content
        # This structure assumes we're processing webhook data from Twitter
        sender_id = str(message.get("sender_id", ""))  # For DMs
        if not sender_id:
            # For mentions/tweets, we might get user_id from different field
            sender_id = str(message.get("user_id", ""))

        content = (message.get("text", "") or "").strip()
        # In a real implementation, we'd also get conversation_id for DMs

        if not sender_id or not content:
            return

        if not self.authorize(sender_id):
            await self._reply(
                message.get("dm_conversation_id"),  # For DMs
                "Access denied. This account is not authorized to drive the agent.",
                is_dm=True
            )
            return

        # Simple deduplication by user and message content
        message_key = (sender_id, content[:64])
        if not hasattr(self, "_recent_messages"):
            self._recent_messages: set = set()
        if message_key in self._recent_messages:
            await self._reply(
                message.get("dm_conversation_id"),
                "That command is already being processed.",
                is_dm=True
            )
            return
        self._recent_messages.add(message_key)
        # Keep only recent messages to prevent memory growth
        if len(self._recent_messages) > 1000:
            self._recent_messages.clear()

        try:
            await self._dispatch(
                message.get("dm_conversation_id"),
                content,
                is_dm=bool(message.get("dm_conversation_id"))
            )
        except Exception as exc:
            logger.exception("Twitter command failed")
            await self._reply(
                message.get("dm_conversation_id"),
                f"Command failed: {type(exc).__name__}: {exc}",
                is_dm=True
            )
        finally:
            self._recent_messages.discard(message_key)

    async def _dispatch(
        self,
        conversation_id: Optional[str],
        content: str,
        is_dm: bool = False
    ) -> None:
        """Process a command from a DM or mention."""
        parts = content.split()
        command = parts[0].lower().lstrip("/")
        args = parts[1:]

        handler = self._command_handlers.get(command)
        if handler is not None:
            result = await handler(args)
            await self._reply(conversation_id, result, is_dm=is_dm)
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
            await self._reply(conversation_id, built_in[command], is_dm=is_dm)
            return

        # Anything else is routed to the central agent as natural language.
        agent_handler = self._command_handlers.get("agent")
        if agent_handler is not None:
            await self._reply(
                conversation_id,
                await agent_handler([content]),
                is_dm=is_dm
            )
            return

        await self._reply(
            conversation_id,
            "Unrecognized command. Send /help.",
            is_dm=is_dm
        )

    async def _reply(
        self,
        conversation_id: Optional[str],
        text: str,
        is_dm: bool = False
    ) -> None:
        """Send a reply via Twitter API (DM or tweet)."""
        if not conversation_id or not self.bearer_token:
            return

        # In a real implementation, we would use the Twitter API v2
        # For now, we'll simulate the sending
        logger.info("Would send Twitter %s: %s", "DM" if is_dm else "mention", text[:100])

    # ------------------------------------------------------------------
    async def start_polling(self) -> bool:
        """Begin listening for DMs and mentions. Returns False if unconfigured."""
        if not self.is_configured:
            logger.info("Twitter bot not started: token or authorized users not configured.")
            return False
        # No X API listener exists yet (no polling/webhook/DM client), so nothing can receive
        # messages. Report that honestly rather than pretending to be listening.
        logger.warning("X (Twitter) listener is not implemented; the bot is NOT listening.")
        self._running = False
        return False

    async def stop(self) -> None:
        self._running = False
        self._client = None

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._running else "NOT_IMPLEMENTED",
            "implemented": False,
            "listener": self._running,
            "authorized_users": len(self.authorized_users),
            "bearer_token_configured": bool(self.bearer_token),
        }


# Global bot instance
twitter_bot = TwitterBot()