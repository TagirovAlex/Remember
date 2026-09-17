from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.scheduler import (
    WATCHDOG_JOB_ID,
    build_scheduler,
    compute_schedule,
    next_occurrence,
    plan_jobs,
    shutdown_scheduler,
    start_watchdog,
    watchdog,
)
from app.services.settings import ensure_defaults, set_setting

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await ensure_defaults(session)
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
async def scheduler():
    sched = build_scheduler()
    sched.start()
    yield sched
    await shutdown_scheduler(sched)


async def _set(factory, key: str, value: str) -> None:
    async with factory() as session:
        await set_setting(session, key, value)
        await session.commit()


def _base_settings(**overrides) -> dict[str, object]:
    data: dict[str, object] = {
        "morning_enabled": True,
        "morning_time": "09:00",
        "evening_reminder_enabled": True,
        "evening_reminder_time": "19:00",
        "tz": "Europe/Moscow",
    }
    data.update(overrides)
    return data


class _FakeSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, factory=None) -> int:
        self.calls += 1
        return 1


def test_next_occurrence_same_day():
    now = datetime(2026, 9, 16, 8, 30, tzinfo=MSK)
    assert next_occurrence(now, "09:00", "Europe/Moscow") == datetime(2026, 9, 16, 9, 0, tzinfo=MSK)


def test_next_occurrence_next_day():
    now = datetime(2026, 9, 16, 10, 0, tzinfo=MSK)
    assert next_occurrence(now, "09:00", "Europe/Moscow") == datetime(2026, 9, 17, 9, 0, tzinfo=MSK)


def test_next_occurrence_exact_time_rolls_to_tomorrow():
    now = datetime(2026, 9, 16, 9, 0, tzinfo=MSK)
    assert next_occurrence(now, "09:00", "Europe/Moscow") == datetime(2026, 9, 17, 9, 0, tzinfo=MSK)


def test_next_occurrence_midnight():
    now = datetime(2026, 9, 16, 0, 20, tzinfo=MSK)
    assert next_occurrence(now, "00:30", "Europe/Moscow") == datetime(2026, 9, 16, 0, 30, tzinfo=MSK)
    now = datetime(2026, 9, 16, 0, 45, tzinfo=MSK)
    assert next_occurrence(now, "00:30", "Europe/Moscow") == datetime(2026, 9, 17, 0, 30, tzinfo=MSK)
    now = datetime(2026, 9, 16, 0, 0, tzinfo=MSK)
    assert next_occurrence(now, "00:00", "Europe/Moscow") == datetime(2026, 9, 17, 0, 0, tzinfo=MSK)


@pytest.mark.parametrize("bad", ["9:00", "09:60", "24:00", "00", "abc", "09:0"])
def test_next_occurrence_invalid_hhmm(bad):
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    with pytest.raises(ValueError):
        next_occurrence(now, bad, "Europe/Moscow")


def test_next_occurrence_crosses_timezones():
    now = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)  # 09:00 in Europe/Moscow
    assert next_occurrence(now, "09:00", "Europe/Moscow") == datetime(2026, 9, 17, 9, 0, tzinfo=MSK)


def test_next_occurrence_invalid_tz_falls_back_to_utc():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    assert next_occurrence(now, "09:00", "NoSuch/Zone") == datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def test_compute_schedule_both_slots_sorted():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    plan = compute_schedule(now, _base_settings())
    assert [key for key, _ in plan] == ["morning", "evening"]
    assert plan[0][1] == datetime(2026, 9, 16, 9, 0, tzinfo=MSK)
    assert plan[1][1] == datetime(2026, 9, 16, 19, 0, tzinfo=MSK)


def test_compute_schedule_order_when_times_passed():
    now = datetime(2026, 9, 16, 20, 0, tzinfo=MSK)
    plan = compute_schedule(now, _base_settings())
    assert [key for key, _ in plan] == ["morning", "evening"]
    assert plan[0][1] == datetime(2026, 9, 17, 9, 0, tzinfo=MSK)
    assert plan[1][1] == datetime(2026, 9, 17, 19, 0, tzinfo=MSK)


def test_compute_schedule_morning_disabled():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    plan = compute_schedule(now, _base_settings(morning_enabled=False))
    assert [key for key, _ in plan] == ["evening"]


def test_compute_schedule_evening_disabled():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    plan = compute_schedule(now, _base_settings(evening_reminder_enabled=False))
    assert [key for key, _ in plan] == ["morning"]


def test_compute_schedule_all_disabled():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    plan = compute_schedule(
        now, _base_settings(morning_enabled=False, evening_reminder_enabled=False)
    )
    assert plan == []


def test_compute_schedule_invalid_time_falls_back_to_default():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=MSK)
    plan = compute_schedule(now, _base_settings(morning_time="oops"))
    assert plan[0][1] == datetime(2026, 9, 16, 9, 0, tzinfo=MSK)


async def test_plan_jobs_arms_every_enabled_slot(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    planned = await plan_jobs(
        scheduler, senders, factory, now=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )
    assert planned == ["morning", "evening"]
    assert scheduler.get_job("remember-morning").trigger.run_date == datetime(
        2026, 9, 16, 9, 0, tzinfo=MSK
    )
    assert scheduler.get_job("remember-evening").trigger.run_date == datetime(
        2026, 9, 16, 19, 0, tzinfo=MSK
    )
    assert fake.calls == 0


async def test_plan_jobs_is_idempotent(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    await plan_jobs(scheduler, senders, factory)
    await plan_jobs(scheduler, senders, factory)
    ids = [job.id for job in scheduler.get_jobs()]
    assert ids.count("remember-morning") == 1
    assert ids.count("remember-evening") == 1
    assert fake.calls == 0


async def test_plan_jobs_skips_disabled_morning(factory, scheduler):
    await _set(factory, "morning_enabled", "false")
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    planned = await plan_jobs(scheduler, senders, factory)
    assert planned == ["evening"]
    assert scheduler.get_job("remember-morning") is None
    assert scheduler.get_job("remember-evening") is not None


async def test_plan_jobs_drops_job_when_slot_disabled(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    await plan_jobs(scheduler, senders, factory)
    await _set(factory, "evening_reminder_enabled", "false")
    planned = await plan_jobs(scheduler, senders, factory)
    assert planned == ["morning"]
    assert scheduler.get_job("remember-evening") is None
    assert scheduler.get_job("remember-morning") is not None


async def test_plan_jobs_skips_slots_without_sender(factory, scheduler):
    fake = _FakeSender()
    senders = {"backup": fake.send}
    planned = await plan_jobs(scheduler, senders, factory)
    assert planned == []
    assert scheduler.get_job("remember-morning") is None
    assert scheduler.get_job("remember-evening") is None


async def test_plan_jobs_all_disabled_drops_everything(factory, scheduler):
    await _set(factory, "morning_enabled", "false")
    await _set(factory, "evening_reminder_enabled", "false")
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    planned = await plan_jobs(scheduler, senders, factory)
    assert planned == []
    assert [job for job in scheduler.get_jobs()] == []


async def test_fire_slot_sends_once_and_rearms(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send}
    await plan_jobs(
        scheduler, senders, factory, now=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )
    job = scheduler.get_job("remember-morning")
    await job.func(**job.kwargs)
    assert fake.calls == 1
    rearmed = scheduler.get_job("remember-morning")
    assert rearmed is not None
    assert rearmed.trigger.run_date == datetime(2026, 9, 17, 9, 0, tzinfo=MSK)


async def test_fire_slot_does_not_rearm_when_sender_removed(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send}
    await plan_jobs(
        scheduler, senders, factory, now=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )
    job = scheduler.get_job("remember-morning")
    senders.pop("morning", None)
    await job.func(**job.kwargs)
    assert fake.calls == 0
    assert scheduler.get_job("remember-morning") is None


async def test_start_watchdog_registers_single_recurring_job(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    start_watchdog(scheduler, senders, factory)
    start_watchdog(scheduler, senders, factory)
    watchdog_jobs = [job for job in scheduler.get_jobs() if job.id == WATCHDOG_JOB_ID]
    assert len(watchdog_jobs) == 1
    assert watchdog_jobs[0].trigger.__class__.__name__ == "CronTrigger"


async def test_watchdog_reapplies_settings(factory, scheduler):
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send}
    await plan_jobs(
        scheduler, senders, factory, now=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )
    await _set(factory, "evening_reminder_enabled", "false")
    await watchdog(scheduler, senders, factory)
    assert scheduler.get_job("remember-evening") is None
    assert scheduler.get_job("remember-morning") is not None