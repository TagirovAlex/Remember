"""Application entry point: FastAPI (uvicorn) + Telegram polling + scheduler.

Run with ``python -m app``; importing this module starts nothing.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uvicorn import Config, Server

from app.auth import ensure_admin_user
from app.bot.bot import create_session_factory
from app.bot.manager import bot_manager
from app.config import settings
from app.db import SessionLocal, init_db, migrate_schema
from app.factory import create_app
from app.scheduler import (
    Sender,
    build_scheduler,
    plan_jobs,
    shutdown_scheduler,
    start_watchdog,
)
from app.services import settings as settings_svc
from app.services.backup import create_backup, prune_backups

logger = logging.getLogger(__name__)


async def run_backup(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Create a database backup and prune old ones; return 1 on success."""
    factory = factory or SessionLocal
    async with factory() as session:
        keep = await settings_svc.backup_keep(session)
    await create_backup()
    await prune_backups(keep)
    return 1


async def _cleanup(
    scheduler: AsyncIOScheduler | None,
    tasks: list[asyncio.Task],
) -> None:
    """Stop the bot, cancel background tasks and shut the scheduler down."""
    await bot_manager.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if scheduler is not None:
        await shutdown_scheduler(scheduler)


async def main() -> None:
    """Run the web UI, the Telegram bot polling and the scheduler."""
    await init_db()
    async with SessionLocal() as session:
        await migrate_schema(session)
        await ensure_admin_user(session)

    app = create_app()

    factory = create_session_factory()
    senders: dict[str, Sender] = {"backup": run_backup}

    scheduler = build_scheduler()
    scheduler.start()
    bot_manager.configure(factory, scheduler, senders)

    if settings.bot_token:
        async with factory() as session:
            bot_enabled = await settings_svc.bot_enabled(session)
        if bot_enabled:
            await bot_manager.start()

    await plan_jobs(scheduler, senders, factory)
    start_watchdog(scheduler, senders, factory)

    host = settings.host
    port = settings.port
    server = Server(Config(app, host=host, port=port, log_level="info"))
    server_task = asyncio.create_task(server.serve())

    # The bot polling task is owned by bot_manager: its failures are logged and
    # turned into an "error" status instead of killing the whole process.
    tasks = [server_task]
    try:
        await asyncio.gather(*tasks)
    finally:
        await _cleanup(scheduler, tasks)


if __name__ == "__main__":
    asyncio.run(main())