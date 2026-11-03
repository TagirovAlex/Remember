"""BotManager tests: start/stop/apply without network or a real token."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot.bot import build_dispatcher as real_build_dispatcher
from app.bot.manager import BotManager, bot_manager
from app.config import settings
from app.models import Base
from app.scheduler import Sender, drop_slots, plan_jobs
from app.services.settings import ensure_defaults, set_setting


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


class _FakeScheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def add_job(self, func, **kwargs: Any) -> None:
        self._jobs[kwargs["id"]] = {"func": func, **kwargs}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)

    def remove_job(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.timeout = 0.0

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    def __init__(self, get_me_error: Exception | None = None) -> None:
        self.session = _FakeSession()
        self._get_me_error = get_me_error
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        if self._get_me_error is not None:
            raise self._get_me_error
        return {"id": 1, "is_bot": True, "first_name": "test", "username": "test_bot"}


class _DispatcherBot:
    """Bot stand-in good enough for a real aiogram Dispatcher."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.id = 1

    async def me(self):
        return SimpleNamespace(username="test_bot", id=1, full_name="Test Bot")

    async def get_me(self):
        return await self.me()

    async def __call__(self, method, **kwargs):
        await asyncio.sleep(0.05)
        return []


class _FakeDispatcher:
    def __init__(self, polling_error: Exception | None = None) -> None:
        self.started = False
        self.stopped = False
        self.handle_signals: bool | None = None
        self._polling_error = polling_error

    async def start_polling(self, bot, handle_signals: bool = False) -> None:
        self.started = True
        self.handle_signals = handle_signals
        if self._polling_error is not None:
            raise self._polling_error
        while True:
            await asyncio.sleep(3600)

    async def stop_polling(self) -> None:
        self.stopped = True


class _BuildRecorder:
    def __init__(
        self,
        get_me_error: Exception | None = None,
        polling_error: Exception | None = None,
        build_error: Exception | None = None,
    ) -> None:
        self.bot = _FakeBot(get_me_error=get_me_error)
        self.dispatcher = _FakeDispatcher(polling_error=polling_error)
        self.build_error = build_error
        self.calls: list[str] = []
        self.proxy_calls: list[tuple[str, str]] = []

    def build_bot(self, token: str, proxy_url: str = "") -> _FakeBot:
        if self.build_error is not None:
            raise self.build_error
        self.calls.append("bot")
        self.proxy_calls.append((token, proxy_url))
        return self.bot

    def build_dispatcher(self) -> _FakeDispatcher:
        self.calls.append("dispatcher")
        return self.dispatcher


class _Harness:
    def __init__(
        self,
        manager: BotManager,
        factory,
        scheduler: _FakeScheduler,
        senders: dict[str, Sender],
    ) -> None:
        self.manager = manager
        self.factory = factory
        self.scheduler = scheduler
        self.senders = senders


@pytest.fixture
async def harness(factory):
    scheduler = _FakeScheduler()
    senders: dict[str, Sender] = {}
    manager = BotManager()
    manager.configure(factory, scheduler, senders)
    yield _Harness(manager, factory, scheduler, senders)
    await manager.stop()


async def _set_setting(factory, key: str, value: str) -> None:
    async with factory() as session:
        await set_setting(session, key, value)
        await session.commit()


class _FakeSender:
    async def send(self, factory=None) -> int:
        return 0


def test_singleton_initial_state(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    assert bot_manager.polling_task is None
    assert bot_manager.is_running() is False
    assert bot_manager.status() == {
        "running": False,
        "token_configured": False,
        "connected": False,
        "error": None,
        "bot_info": None,
    }


async def test_start_without_token_returns_false(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is False
    assert harness.manager.is_running() is False
    assert recorder.calls == []
    assert harness.senders == {}


async def test_start_with_token_starts_polling_and_arms_slots(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    assert recorder.bot.get_me_calls == 1
    await asyncio.sleep(0)
    assert harness.manager.is_running() is True
    assert harness.manager.status()["connected"] is True
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["bot_info"] == {
        "username": "test_bot",
        "first_name": "test",
    }
    assert harness.manager.polling_task is not None
    assert recorder.dispatcher.started is True
    assert recorder.dispatcher.handle_signals is False
    assert set(harness.senders) == {"morning", "evening"}
    assert harness.scheduler.get_job("remember-morning") is not None
    assert harness.scheduler.get_job("remember-evening") is not None

    assert await harness.manager.stop() is True
    assert harness.manager.is_running() is False
    assert harness.manager.status()["connected"] is False
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["bot_info"] is None
    assert recorder.dispatcher.stopped is True
    assert recorder.bot.session.closed is True
    assert harness.senders == {}
    assert harness.scheduler.get_job("remember-morning") is None
    assert harness.scheduler.get_job("remember-evening") is None
    assert harness.manager.polling_task is None


async def test_start_reads_proxy_from_db(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    await _set_setting(harness.factory, "proxy_enabled", "true")
    await _set_setting(harness.factory, "proxy_host", "10.0.0.1")
    await _set_setting(harness.factory, "proxy_port", "3128")
    await _set_setting(harness.factory, "proxy_user", "u")
    await _set_setting(harness.factory, "proxy_password", "p")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    await asyncio.sleep(0)
    assert harness.manager.is_running() is True
    assert recorder.proxy_calls == [("123:test", "socks5://u:p@10.0.0.1:3128")]

    assert await harness.manager.stop() is True


async def test_stop_when_not_running_is_noop(harness):
    assert await harness.manager.stop() is True
    assert harness.manager.is_running() is False


async def test_start_stop_idempotent(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    assert await harness.manager.start() is True
    assert recorder.calls == ["bot", "dispatcher"]
    assert harness.manager.is_running() is True

    assert await harness.manager.stop() is True
    assert await harness.manager.stop() is True
    assert harness.manager.is_running() is False
    assert harness.senders == {}


async def test_apply_disabled_does_not_start(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "false")
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    result = await harness.manager.apply()
    assert result == "already-stopped"
    assert harness.manager.is_running() is False
    assert recorder.calls == []
    assert harness.senders == {}


async def test_apply_enabled_starts(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "true")
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    result = await harness.manager.apply()
    assert result == "started"
    await asyncio.sleep(0)
    assert harness.manager.is_running() is True
    assert recorder.dispatcher.started is True
    assert set(harness.senders) == {"morning", "evening"}


async def test_apply_enabled_without_token(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "true")
    monkeypatch.setattr(settings, "bot_token", "")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    result = await harness.manager.apply()
    assert result == "no-token"
    assert harness.manager.is_running() is False
    assert recorder.calls == []


async def test_start_after_stop_rebuilds_dispatcher(harness, monkeypatch):
    """Manual stop/start must not hit aiogram's single-attach router rule."""
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)

    def build_dispatcher_with_real_router():
        real_build_dispatcher()
        return recorder.dispatcher

    monkeypatch.setattr(
        "app.bot.manager.build_dispatcher", build_dispatcher_with_real_router
    )

    assert await harness.manager.start() is True
    assert await harness.manager.stop() is True
    assert await harness.manager.start() is True
    await asyncio.sleep(0)
    assert harness.manager.is_running() is True
    assert await harness.manager.stop() is True


async def test_start_dispatcher_failure_sets_error(harness, monkeypatch):
    """A dispatcher build failure is reported in the status, never raised as a 500."""
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)

    def broken_build() -> None:
        raise RuntimeError("Router is already attached")

    monkeypatch.setattr("app.bot.manager.build_dispatcher", broken_build)

    assert await harness.manager.start() is False
    assert harness.manager.is_running() is False
    assert harness.manager.status()["error"] is not None
    assert "Router is already attached" in harness.manager.status()["error"]
    assert recorder.bot.session.closed is True


def _aiogram_polling_tasks() -> list[asyncio.Task]:
    """Return pending aiogram polling tasks (the inner ones start_polling owns)."""
    current = asyncio.current_task()
    found: list[asyncio.Task] = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        name = getattr(task.get_coro(), "__qualname__", "")
        if name.startswith("Dispatcher._polling") or "_listen_updates" in name:
            found.append(task)
    return found


async def test_stop_cancels_aiogram_inner_polling_task(harness, monkeypatch):
    """stop() must not leave aiogram's polling loop retrying on a closed client."""
    monkeypatch.setattr(settings, "bot_token", "123:test")
    bot = _DispatcherBot()
    dispatcher = Dispatcher()
    monkeypatch.setattr("app.bot.manager.build_bot", lambda token, proxy="": bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", lambda: dispatcher)

    assert await harness.manager.start() is True
    await asyncio.sleep(0.1)
    assert len(_aiogram_polling_tasks()) == 1

    assert await harness.manager.stop() is True
    await asyncio.sleep(0.1)
    assert _aiogram_polling_tasks() == []


async def test_apply_enabled_when_already_running(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "true")
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)
    await harness.manager.start()

    result = await harness.manager.apply()
    assert result == "already"
    assert harness.manager.is_running() is True
    assert recorder.calls == ["bot", "dispatcher"]


async def test_apply_disabled_stops_running(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "true")
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)
    await harness.manager.start()

    await _set_setting(harness.factory, "bot_enabled", "false")
    result = await harness.manager.apply()
    assert result == "stopped"
    assert harness.manager.is_running() is False
    assert recorder.bot.session.closed is True
    assert harness.senders == {}


async def test_drop_slots_removes_only_requested_keys(harness):
    await _set_setting(harness.factory, "backup_enabled", "true")
    fake = _FakeSender()
    senders = {"morning": fake.send, "evening": fake.send, "backup": fake.send}
    await plan_jobs(harness.scheduler, senders, harness.factory)
    assert harness.scheduler.get_job("remember-morning") is not None
    assert harness.scheduler.get_job("remember-evening") is not None
    assert harness.scheduler.get_job("remember-backup") is not None

    await drop_slots(harness.scheduler, ["morning", "evening"])
    assert harness.scheduler.get_job("remember-morning") is None
    assert harness.scheduler.get_job("remember-evening") is None
    assert harness.scheduler.get_job("remember-backup") is not None


async def test_drop_slots_missing_keys_is_noop(harness):
    await drop_slots(harness.scheduler, ["morning", "evening"])
    assert await drop_slots(harness.scheduler, ["morning", "evening"]) is None


async def test_polling_failure_sets_error_without_crashing(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder(polling_error=RuntimeError("proxy unreachable"))
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    assert recorder.bot.get_me_calls == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert harness.manager.is_running() is False
    assert harness.manager.status()["connected"] is False
    assert harness.manager.status()["error"] is not None
    assert "proxy unreachable" in harness.manager.status()["error"]
    assert harness.manager.status()["bot_info"] is None
    assert recorder.dispatcher.started is True
    assert recorder.bot.session.closed is True
    assert harness.senders == {}
    assert harness.scheduler.get_job("remember-morning") is None
    assert harness.scheduler.get_job("remember-evening") is None


async def test_start_get_me_failure_sets_error(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder(get_me_error=RuntimeError("invalid token"))
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is False
    assert harness.manager.is_running() is False
    assert harness.manager.status()["connected"] is False
    assert harness.manager.status()["error"] is not None
    assert "invalid token" in harness.manager.status()["error"]
    assert harness.manager.status()["bot_info"] is None
    assert recorder.bot.get_me_calls == 1
    assert recorder.dispatcher.started is False
    assert recorder.bot.session.closed is True
    assert harness.senders == {}
    assert harness.scheduler.get_job("remember-morning") is None
    assert harness.scheduler.get_job("remember-evening") is None


async def test_stop_clears_error_and_connected(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder(polling_error=RuntimeError("boom"))
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert harness.manager.status()["error"] is not None

    assert await harness.manager.stop() is True
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["connected"] is False
    assert harness.manager.is_running() is False


async def test_apply_enabled_when_start_fails(harness, monkeypatch):
    await _set_setting(harness.factory, "bot_enabled", "true")
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder(get_me_error=RuntimeError("proxy unreachable"))
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    result = await harness.manager.apply()
    assert result == "start-failed"
    assert harness.manager.is_running() is False
    assert harness.manager.status()["error"] is not None
    assert "proxy unreachable" in harness.manager.status()["error"]


async def test_start_build_failure_sets_error(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    recorder = _BuildRecorder(build_error=ValueError("Unknown scheme for proxy URL"))
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is False
    assert harness.manager.is_running() is False
    assert harness.manager.status()["connected"] is False
    assert harness.manager.status()["error"] is not None
    assert "Unknown scheme" in harness.manager.status()["error"]
    assert harness.manager.polling_task is None


async def test_successful_restart_clears_error(harness, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:test")
    failing = _BuildRecorder(polling_error=RuntimeError("boom"))
    monkeypatch.setattr("app.bot.manager.build_bot", failing.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", failing.build_dispatcher)

    assert await harness.manager.start() is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert harness.manager.status()["error"] is not None

    recorder = _BuildRecorder()
    monkeypatch.setattr("app.bot.manager.build_bot", recorder.build_bot)
    monkeypatch.setattr("app.bot.manager.build_dispatcher", recorder.build_dispatcher)

    assert await harness.manager.start() is True
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["connected"] is True
    assert harness.manager.is_running() is True
    assert set(harness.senders) == {"morning", "evening"}

    assert await harness.manager.stop() is True
    assert harness.manager.status()["error"] is None