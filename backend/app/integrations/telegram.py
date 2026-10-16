"""Telegram Bot Integration.

Shares the same database, task engine, and agent controller as the web dashboard.
A task created here is immediately visible there and vice versa (Part 17).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class TelegramAuthError(Exception):
    """Raised when an unauthorized user attempts a command."""


class TelegramBot:
    """Command router for the Telegram interface."""

    def __init__(self) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.authorized_users = set(settings.TELEGRAM_AUTHORIZED_USER_IDS or [])
        self._command_handlers: Dict[str, Callable] = {}
        self._running = False
        self._app = None

    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.token) and bool(self.authorized_users)

    def authorize(self, telegram_user_id: int) -> bool:
        """Only explicitly authorized users may drive the agent."""
        if telegram_user_id in self.authorized_users:
            return True
        logger.warning("Rejected unauthorized Telegram user %s", telegram_user_id)
        return False

    def register_command(self, command: str, handler: Callable) -> None:
        self._command_handlers[command] = handler

    # ------------------------------------------------------------------
    async def handle_update(self, update: Dict[str, Any]) -> None:
        """Route an incoming update. Shared handler for polling and webhooks."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat = message.get("chat") or {}
        user = message.get("from") or {}
        user_id = user.get("id")
        text = (message.get("text") or "").strip()

        if not user_id or not text:
            return

        if not self.authorize(user_id):
            await self._reply(
                chat.get("id"),
                "Access denied. This account is not authorized to drive the agent.",
            )
            return

        # Concurrency guard: identical commands from one user in quick succession
        # are deduplicated so an irreversible operation cannot fire twice.
        command_key = (user_id, text[:64])
        if command_key in getattr(self, "_recent_commands", set()):
            await self._reply(chat.get("id"), "That command is already being processed.")
            return
        if not hasattr(self, "_recent_commands"):
            self._recent_commands: set = set()
        self._recent_commands.add(command_key)

        try:
            await self._dispatch(chat.get("id"), text)
        except Exception as exc:
            logger.exception("Telegram command failed")
            await self._reply(chat.get("id"), f"Command failed: {type(exc).__name__}: {exc}")
        finally:
            self._recent_commands.discard(command_key)

    async def _dispatch(self, chat_id: int, text: str) -> None:
        parts = text.split()
        command = parts[0].lower().lstrip("/")
        args = parts[1:]

        handler = self._command_handlers.get(command)
        if handler is not None:
            result = await handler(args)
            await self._reply(chat_id, result)
            return

        built_in = {
            "start": "AirdropOS is online. Commands: /status, /tasks, /task <ID>, /goals, "
                     "/wallet, /discover, /help, /stop, /resume",
            "help": "Available commands:\n"
                    "/status - platform and agent status\n"
                    "/tasks - list active tasks\n"
                    "/task <AIR-YYYY-MM-DD-NNNNNN> - full task status report\n"
                    "/create <title> - create a task\n"
                    "/goals - list long-term goals\n"
                    "/wallet - wallet summary\n"
                    "/discover - run airdrop discovery\n"
                    "/stop - engage emergency stop\n"
                    "/resume - release emergency stop",
        }

        if command in built_in:
            await self._reply(chat_id, built_in[command])
            return

        # Anything else is routed to the central agent as natural language.
        agent_handler = self._command_handlers.get("agent")
        if agent_handler is not None:
            await self._reply(chat_id, await agent_handler([text]))
            return

        await self._reply(chat_id, "Unrecognized command. Send /help.")

    async def _reply(self, chat_id: Optional[int], text: str) -> None:
        if not chat_id or not self.token:
            return
        import httpx

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Telegram caps messages at 4096 chars; split defensively.
        for chunk in [text[i : i + 4000] for i in range(0, len(text), 4000)]:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                        },
                    )
            except Exception as exc:
                logger.error("Failed to reply to Telegram chat %s: %s", chat_id, exc)

    # ------------------------------------------------------------------
    async def start_polling(self) -> bool:
        """Begin long polling. Returns False if unconfigured or unavailable."""
        if not self.is_configured:
            logger.info("Telegram bot not started: token or authorized users not configured.")
            return False
        try:
            from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

            application = (
                ApplicationBuilder().token(self.token).build()
            )
            application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
            )
            for command in ("start", "help", "status", "tasks", "discover", "stop", "resume"):
                application.add_handler(CommandHandler(command, self._on_command))
            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)
            self._app = application
            self._running = True
            logger.info("Telegram bot polling started")
            return True
        except Exception as exc:
            logger.error("Telegram polling failed to start: %s", exc)
            return False

    async def _on_message(self, update, context) -> None:
        await self.handle_update(update.to_dict())

    async def _on_command(self, update, context) -> None:
        await self.handle_update(update.to_dict())

    async def stop(self) -> None:
        if self._app is not None:
            try:
                await self._app.updater.stop()
                await self._app.stop()
            except Exception:
                pass
            self._app = None
        self._running = False

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._running else ("NOT_CONFIGURED" if not self.is_configured else "STOPPED"),
            "polling": self._running,
            "authorized_users": len(self.authorized_users),
            "token_configured": bool(self.token),
        }


telegram_bot = TelegramBot()
