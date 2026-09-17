"""APScheduler wiring: DB-driven one-shot jobs plus an hourly watchdog.

The scheduler lives in the same asyncio process as uvicorn and aiogram polling.
Every enabled slot ("morning" digest, "evening" reminder, "backup") is armed
as a single-fire DateTrigger job named ``remember-<key>``. When a job fires it
invokes its sender and re-arms the slot for the next occurrence, so time and
on/off changes made in the web UI are picked up without a restart. Settings are
read from the database on every (re)arm.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "remember-"
WATCHDOG_JOB_ID = "remember-watchdog"
_WATCHDOG_TRIGGER = CronTrigger(minute=0)
_MISFIRE_GRACE_SECONDS = 300
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_SLOT_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "morning": ("morning_enabled", "morning_time", "09:00"),
    "evening": ("evening_reminder_enabled", "evening_reminder_time", "19:00"),
    "backup": ("backup_enabled", "backup_time", "03:00"),
}

Sender = Callable[..., Awaitable[int]]
Senders = Mapping[str, Sender]


def job_id(key: str) -> str:
    """Return the scheduler job id for a slot key."""
    return f"{JOB_ID_PREFIX}{key}"


def next_occurrence(now: datetime, hhmm: str, tz: str) -> datetime:
    """Return the next moment at HH:MM in ``tz`` that is strictly after ``now``.

    The result keeps the timezone of ``tz``; an unknown zone name falls back to
    UTC. ``now`` may be naive (treated as UTC) or timezone-aware.
    """
    if not _TIME_RE.fullmatch(hhmm):
        raise ValueError(f"time must be in HH:MM 24h format, got {hhmm!r}")
    hour, minute = (int(part) for part in hhmm.split(":"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def compute_schedule(
    now: datetime, schedule_settings: Mapping[str, object]
) -> list[tuple[str, datetime]]:
    """Return ``(key, next_run)`` pairs for every enabled slot.

    Times and the timezone come from ``schedule_settings`` so the function stays
    pure and testable without a database. Pairs are sorted by run time.
    """
    tz = schedule_settings.get("tz") or settings.tz
    plan: list[tuple[str, datetime]] = []
    for key, (enabled_key, time_key, default_time) in _SLOT_DEFAULTS.items():
        if not schedule_settings.get(enabled_key, key != "backup"):
            continue
        hhmm = schedule_settings.get(time_key) or default_time
        if not isinstance(hhmm, str) or not _TIME_RE.fullmatch(hhmm):
            logger.warning("invalid time for slot %s: %r, using default %s", key, hhmm, default_time)
            hhmm = default_time
        plan.append((key, next_occurrence(now, hhmm, str(tz))))
    plan.sort(key=lambda item: item[1])
    return plan


async def _load_settings(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    """Read scheduling settings from the database into a plain dict."""
    async with factory() as session:
        return {
            "morning_enabled": await settings_service.morning_enabled(session),
            "morning_time": await settings_service.morning_time(session),
            "evening_reminder_enabled": await settings_service.evening_reminder_enabled(session),
            "evening_reminder_time": await settings_service.evening_reminder_time(session),
            "backup_enabled": await settings_service.backup_enabled(session),
            "backup_time": await settings_service.backup_time(session),
            "tz": await settings_service.tz(session),
        }


async def _plan_slot(
    scheduler: AsyncIOScheduler,
    key: str,
    run_at: datetime,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Arm one one-shot job for one slot, replacing any previous occurrence."""
    scheduler.add_job(
        _fire_slot,
        trigger=DateTrigger(run_date=run_at),
        id=job_id(key),
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        kwargs={
            "scheduler": scheduler,
            "key": key,
            "senders": senders,
            "factory": factory,
            "run_at": run_at,
        },
    )


async def _reschedule_slot(
    scheduler: AsyncIOScheduler,
    key: str,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
    after: datetime,
) -> None:
    """Re-arm ``key`` for the next occurrence after ``after`` if it stays enabled.

The sender check happens after the only suspension point (the database read)
and right before planning, so a concurrent bot stop cannot leave an orphan job.
"""
    store = await _load_settings(factory)
    if key not in senders:
        return
    run_at = next(
        (time for slot, time in compute_schedule(after, store) if slot == key),
        None,
    )
    if run_at is not None:
        await _plan_slot(scheduler, key, run_at, senders, factory)


async def _fire_slot(
    scheduler: AsyncIOScheduler,
    key: str,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
    run_at: datetime,
) -> None:
    """Run the slot sender and re-arm the slot for its next occurrence.

    When the sender for ``key`` has been removed (e.g. the bot was stopped
    while this job was pending), the slot is dropped without firing or rearming.
    """
    if key not in senders:
        if scheduler.get_job(job_id(key)) is not None:
            scheduler.remove_job(job_id(key))
        return
    try:
        await senders[key](factory=factory)
    finally:
        await _reschedule_slot(scheduler, key, senders, factory, after=run_at)


async def plan_jobs(
    scheduler: AsyncIOScheduler,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> list[str]:
    """Arm one-shot jobs for every enabled slot and drop jobs for disabled slots.

    Senders map slot keys to async callables invoked as ``await sender(factory=...)``.
    Re-running is idempotent: jobs are replaced in place, never duplicated.
    """
    if now is None:
        now = datetime.now(UTC)
    store = await _load_settings(factory)
    scheduled: list[str] = []
    for key, run_at in compute_schedule(now, store):
        if key not in senders:
            continue
        await _plan_slot(scheduler, key, run_at, senders, factory)
        scheduled.append(key)
    for key in senders:
        if key not in scheduled:
            job_id_value = job_id(key)
            if scheduler.get_job(job_id_value) is not None:
                scheduler.remove_job(job_id_value)
    return scheduled


async def drop_slots(
    scheduler: AsyncIOScheduler,
    keys: list[str],
) -> None:
    """Remove scheduled jobs for the given slot keys if they exist.

    Jobs of other slots are left untouched. Used when the bot is stopped so
    morning/evening digests no longer fire.
    """
    for key in keys:
        job_id_value = job_id(key)
        if scheduler.get_job(job_id_value) is not None:
            scheduler.remove_job(job_id_value)


async def watchdog(
    scheduler: AsyncIOScheduler,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-apply the database schedule so e.g. a web UI change takes effect on its own."""
    await plan_jobs(scheduler, senders, factory)


def start_watchdog(
    scheduler: AsyncIOScheduler,
    senders: Senders,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register the recurring hourly watchdog job (idempotent, replace_existing)."""
    scheduler.add_job(
        watchdog,
        trigger=_WATCHDOG_TRIGGER,
        id=WATCHDOG_JOB_ID,
        replace_existing=True,
        kwargs={
            "scheduler": scheduler,
            "senders": senders,
            "factory": factory,
        },
    )


def build_scheduler() -> AsyncIOScheduler:
    """Create an AsyncIOScheduler bound to the current asyncio event loop."""
    return AsyncIOScheduler(timezone=UTC)


async def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Shut the scheduler down gracefully when it is still running."""
    if scheduler.running:
        scheduler.shutdown(wait=False)