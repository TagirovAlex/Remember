"""BotManager tests: start/stop/apply without network or a real token."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

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