"""BotManager: start/stop the Telegram polling and digest slots from settings.

The manager owns the aiogram Bot/Dispatcher lifecycle. It is a process-wide
singleton configured by ``app.main`` and used by the web layer when the
``bot_enabled`` setting changes, so the bot can be switched on and off from the
UI without restarting the application.
"""

import asyncio
import logging
from contextlib import suppress
from functools import partial

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.bot import build_bot, build_dispatcher
from app.bot.handlers import set_session_factory
from app.bot.keyboards import binding_notice_text, confirm_binding_keyboard
from app.bot.sender import (
    send_digest_to_subscribers,
    send_evening_reminder_to_subscribers,
)
from app.config import settings
from app.scheduler import Sender, drop_slots, plan_jobs
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

_BOT_SLOTS = ("morning", "evening")


class BotManager:
    """Singleton controlling the Telegram bot lifecycle and its digest slots."""

    _GET_ME_TIMEOUT = 15.0

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None
        self._polling_task: asyncio.Task | None = None
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._senders: dict[str, Sender] | None = None
        self._connected: bool = False
        self._error: str | None = None
        self._bot_info: dict[str, str] | None = None

    def configure(
        self,
        factory: async_sessionmaker[AsyncSession],
        scheduler: AsyncIOScheduler,
        senders: dict[str, Sender],
    ) -> None:
        """Store the shared session factory, scheduler and sender registry.

        Must be called once from the entrypoint before start()/stop()/apply().
        Does not start the bot; the caller decides when it should run.
        """
        self._factory = factory
        self._scheduler = scheduler
        self._senders = senders

    @property
    def polling_task(self) -> asyncio.Task | None:
        """Return the current polling task, or None when the bot is not running."""
        return self._polling_task

    @property
    def error(self) -> str | None:
        """Return the last bot error message, or None when there is none."""
        return self._error

    def is_running(self) -> bool:
        """Whether an active polling task exists (not done, bot is set)."""
        if self._polling_task is None or self._bot is None:
            return False
        return not self._polling_task.done()

    def status(self) -> dict[str, bool | str | dict[str, str | None] | None]:
        """Report the bot state: running, connection and error details."""
        return {
            "running": self.is_running(),
            "connected": self._connected,
            "error": self._error,
            "token_configured": bool(settings.bot_token),
            "bot_info": self._bot_info,
        }

    def _note_start_error(self, message: str) -> None:
        """Record a start failure so the settings page can show its reason."""
        self._connected = False
        self._error = message
        self._bot_info = None

    async def _drop_slots(self) -> None:
        """Remove the bot digest slots; a scheduler error must not escape."""
        if self._scheduler is None:
            return
        try:
            await drop_slots(self._scheduler, list(_BOT_SLOTS))
        except Exception:
            logger.exception("failed to drop bot digest slots")

    async def _run_polling(self, bot: Bot, dispatcher: Dispatcher) -> None:
        """Run the polling loop under supervision; failures become status errors.

        The polling task is detached from the application's main gather, so a
        network/API failure here must not take the web UI or scheduler down.
        """
        try:
            await dispatcher.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("bot polling failed")
            self._connected = False
            self._error = str(exc)
            self._bot_info = None
            self._polling_task = None
            self._bot = None
            self._dispatcher = None
            if self._senders is not None:
                for key in _BOT_SLOTS:
                    self._senders.pop(key, None)
            await self._drop_slots()
            with suppress(Exception):
                await bot.session.close()

    async def start(self) -> bool:
        """Verify the API connection and start polling; return True on success.

        No-op (True) when already running. Returns False when BOT_TOKEN is
        missing, the manager is not configured, or the Telegram API is
        unreachable (bad token, network, proxy); the bot then stays off, the
        error is stored in ``self._error`` and the UI shows its state.
        """
        if self.is_running():
            return True
        if not settings.bot_token:
            logger.warning("BOT_TOKEN is empty; Telegram bot will not start")
            return False
        if self._factory is None or self._scheduler is None or self._senders is None:
            logger.error("BotManager is not configured; call configure() first")
            return False
        set_session_factory(self._factory)
        bot: Bot | None = None
        try:
            async with self._factory() as session:
                proxy = await settings_service.proxy_url(session)
            bot = build_bot(settings.bot_token, proxy)
            dispatcher = build_dispatcher()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if bot is not None:
                with suppress(Exception):
                    await bot.session.close()
            self._note_start_error(f"Не удалось создать бота: {exc}")
            logger.exception("failed to build the bot")
            return False
        try:
            self._senders["morning"] = partial(send_digest_to_subscribers, bot)
            self._senders["evening"] = partial(send_evening_reminder_to_subscribers, bot)
            await plan_jobs(self._scheduler, self._senders, self._factory)
        except Exception:
            self._senders.pop("morning", None)
            self._senders.pop("evening", None)
            with suppress(Exception):
                await bot.session.close()
            logger.exception("failed to plan bot digest slots")
            return False
        self._bot = bot
        self._dispatcher = dispatcher
        try:
            bot_me = await asyncio.wait_for(
                bot.get_me(), timeout=self._GET_ME_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            self._error = f"API недоступен: {exc}"
            self._bot_info = None
            self._bot = None
            self._dispatcher = None
            self._senders.pop("morning", None)
            self._senders.pop("evening", None)
            await self._drop_slots()
            with suppress(Exception):
                await bot.session.close()
            logger.exception("bot get_me() failed; the bot will not start")
            return False
        if isinstance(bot_me, dict):
            username = bot_me.get("username")
            first_name = bot_me.get("first_name")
        else:
            username = getattr(bot_me, "username", None)
            first_name = getattr(bot_me, "first_name", None)
        self._bot_info = {"username": username, "first_name": first_name}
        self._error = None
        self._connected = True
        self._polling_task = asyncio.create_task(self._run_polling(bot, dispatcher))
        return True

    async def stop(self) -> bool:
        """Stop polling, drop digest slots and release bot resources.

        Idempotent and safe to call when the bot is already stopped. Polling is
        stopped via ``Dispatcher.stop_polling``: aiogram then cancels its own
        ``_polling`` task. Cancelling the supervisor task directly would skip
        that cleanup and leave the inner task retrying forever on a closed
        session.
        """
        task = self._polling_task
        if task is not None and not task.done():
            if self._dispatcher is not None:
                with suppress(Exception):
                    await self._dispatcher.stop_polling()
            await asyncio.sleep(0)
            if not task.done():
                task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await task
        dispatcher = self._dispatcher
        if dispatcher is not None:
            with suppress(Exception):
                await dispatcher.stop_polling()
        if self._senders is not None:
            for key in _BOT_SLOTS:
                self._senders.pop(key, None)
        await self._drop_slots()
        bot = self._bot
        if bot is not None:
            with suppress(Exception):
                await bot.session.close()
        self._bot = None
        self._dispatcher = None
        self._polling_task = None
        self._connected = False
        self._bot_info = None
        self._error = None
        return True

    async def send_binding_notice(self, user_id: int, telegram_id: int, username: str) -> bool:
        """Send the two-step binding notice to the chat; True when delivered."""
        bot = self._bot
        if bot is None or not self.is_running():
            return False
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=binding_notice_text(username),
                reply_markup=confirm_binding_keyboard(user_id),
            )
            return True
        except TelegramAPIError:
            logger.warning("could not send binding notice to the chat")
            return False

    async def apply(self) -> str:
        """Reconcile the running state with the ``bot_enabled`` setting.

        Returns a short status string for the web layer:
        "started" | "already" | "no-token" | "start-failed" when enabled, and
        "stopped" | "already-stopped" when disabled.
        """
        factory = self._factory
        if factory is None:
            logger.error("BotManager is not configured; cannot apply bot_enabled")
            return "already-stopped"
        async with factory() as session:
            enabled = await settings_service.bot_enabled(session)
        if enabled:
            if self.is_running():
                return "already"
            if not settings.bot_token:
                return "no-token"
            started = await self.start()
            return "started" if started else "start-failed"
        if self.is_running():
            await self.stop()
            return "stopped"
        return "already-stopped"


bot_manager = BotManager()