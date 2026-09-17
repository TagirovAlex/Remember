"""Telegram binding tests: web setting, confirm/cancel buttons, bot commands."""

from datetime import date
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import SessionLocal, get_session
from app.factory import create_app
from app.models import Base, BotSubscriber, Server, User
from app.security import hash_password

PASSWORD = "secret-pass"
SID = "remember_sid"


@pytest.fixture
async def engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def app(engine, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", PASSWORD)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_session] = override_get_session
    async with session_factory() as session:
        await ensure_admin_user(session)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _handlers_factory(session_factory):
    from app.bot import handlers

    handlers.set_session_factory(session_factory)
    yield
    handlers.set_session_factory(SessionLocal)


async def _add_user(session_factory, username, password, role="user", **kw) -> User:
    async with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
            **kw,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _login(client: httpx.AsyncClient, username: str, password: str):
    return await client.post(
        "/login", data={"username": username, "password": password}
    )


async def _add_server(session_factory, name: str, owner_id: int, next_due: date) -> int:
    async with session_factory() as session:
        server = Server(
            name=name,
            kind="gpu",
            provider="Hetzner",
            amount_rub=1000,
            period="monthly",
            next_due=next_due,
            is_active=True,
            owner_id=owner_id,
        )
        session.add(server)
        await session.commit()
        await session.refresh(server)
        return server.id


class _FakeChat:
    def __init__(self, chat_id: int, title: str | None = None):
        self.id = chat_id
        self.title = title


class _FakeFromUser:
    def __init__(self, user_id: int, full_name: str = "Test User"):
        self.id = user_id
        self.full_name = full_name


class _FakeMessage:
    def __init__(self, chat_id: int, text: str = "", from_user=None, title=None):
        self.chat = _FakeChat(chat_id, title)
        self.text = text
        self.from_user = from_user
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class _FakeCallbackMessage:
    def __init__(self, chat_id: int):
        self.chat = _FakeChat(chat_id)
        self.edits: list[str] = []
        self.answers: list[str] = []

    async def edit_text(self, text):
        self.edits.append(text)

    async def answer(self, text, *args, **kwargs):
        self.answers.append(text)


class _FakeCallback:
    def __init__(self, data: str, chat_id: int):
        self.data = data
        self.message = _FakeCallbackMessage(chat_id)
        self.answers: list[str] = []

    async def answer(self, text, *args, **kwargs):
        self.answers.append(text)


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


async def test_setting_telegram_id_saves_and_notifies(client, session_factory, monkeypatch):
    from app.bot.manager import bot_manager

    await _add_user(session_factory, "alice", "alice-pass")
    async with session_factory() as session:
        session.add(BotSubscriber(chat_id=555, is_active=True))
        await session.commit()
    await _login(client, "alice", "alice-pass")

    sent: list[tuple[int, int, str]] = []
    monkeypatch.setattr(bot_manager, "is_running", lambda: True)

    async def fake_notice(user_id, telegram_id, username):
        sent.append((user_id, telegram_id, username))
        return True

    monkeypatch.setattr(bot_manager, "send_binding_notice", fake_notice)
    response = await client.post("/settings/telegram", data={"telegram_id": "555"})
    assert response.status_code == 303
    assert "подтвердите привязку" in unquote(response.headers["location"])

    async with session_factory() as session:
        alice = await session.scalar(select(User).where(User.username == "alice"))
        assert alice.telegram_id == 555
        assert alice.telegram_id_confirmed is False
        assert sent == [(alice.id, 555, "alice")]


async def test_setting_telegram_id_occupied(client, session_factory):
    await _add_user(session_factory, "first", "first-pass", telegram_id=555, telegram_id_confirmed=True)
    await _add_user(session_factory, "alice", "alice-pass")
    await _login(client, "alice", "alice-pass")
    response = await client.post("/settings/telegram", data={"telegram_id": "555"})
    assert response.status_code == 303
    assert "уже привязан" in unquote(response.headers["location"])
    async with session_factory() as session:
        alice = await session.scalar(select(User).where(User.username == "alice"))
        assert alice.telegram_id is None


async def test_setting_telegram_id_invalid(client, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    await _login(client, "alice", "alice-pass")
    response = await client.post("/settings/telegram", data={"telegram_id": "-5"})
    assert response.status_code == 303
    assert "диапазон" in unquote(response.headers["location"])
    response = await client.post("/settings/telegram", data={"telegram_id": "abc"})
    assert response.status_code == 303
    assert "целым числом" in unquote(response.headers["location"])


async def test_confirm_button_activates_binding(session_factory):
    from app.bot.handlers import on_tg_confirm

    user = await _add_user(session_factory, "alice", "alice-pass", telegram_id=555)
    async with session_factory() as session:
        session.add(BotSubscriber(chat_id=555, is_active=True))
        await session.commit()

    callback = _FakeCallback(f"tg_confirm:{user.id}", chat_id=555)
    await on_tg_confirm(callback)

    assert callback.answers == ["Привязка подтверждена ✅"]
    async with session_factory() as session:
        alice = await session.get(User, user.id)
        assert alice.telegram_id_confirmed is True
        sub = await session.scalar(
            select(BotSubscriber).where(BotSubscriber.chat_id == 555)
        )
        assert sub.user_id == user.id
        assert sub.is_active is True


async def test_confirm_button_wrong_chat_rejected(session_factory):
    from app.bot.handlers import on_tg_confirm

    user = await _add_user(session_factory, "alice", "alice-pass", telegram_id=555)
    async with session_factory() as session:
        session.add(BotSubscriber(chat_id=555, is_active=True))
        await session.commit()

    callback = _FakeCallback(f"tg_confirm:{user.id}", chat_id=999)
    await on_tg_confirm(callback)

    assert "несовпадение" in callback.answers[0]
    async with session_factory() as session:
        alice = await session.get(User, user.id)
        assert alice.telegram_id_confirmed is False


async def test_cancel_button_clears_telegram_id(session_factory):
    from app.bot.handlers import on_tg_cancel

    user = await _add_user(session_factory, "alice", "alice-pass", telegram_id=555)
    callback = _FakeCallback(f"tg_cancel:{user.id}", chat_id=555)
    await on_tg_cancel(callback)

    async with session_factory() as session:
        alice = await session.get(User, user.id)
        assert alice.telegram_id is None
        assert alice.telegram_id_confirmed is False


async def test_unbound_chat_gets_hint(session_factory):
    from app.bot.handlers import cmd_today

    message = _FakeMessage(chat_id=777, from_user=_FakeFromUser(777))
    await cmd_today(message)
    assert message.answers
    text, _ = message.answers[0]
    assert "привяжите аккаунт" in text


async def test_bound_chat_gets_own_data(session_factory):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.bot.handlers import cmd_today

    user = await _add_user(
        session_factory, "alice", "alice-pass", telegram_id=777,
        telegram_id_confirmed=True,
    )
    today_value = datetime.now(ZoneInfo("Europe/Moscow")).date()
    await _add_server(session_factory, "gpu-alice", user.id, today_value)
    message = _FakeMessage(chat_id=777, from_user=_FakeFromUser(777))
    await cmd_today(message)
    assert message.answers
    text, _ = message.answers[0]
    assert "gpu-alice" in text


async def test_unconfirmed_user_gets_hint_not_data(session_factory):
    from app.bot.handlers import cmd_today

    user = await _add_user(
        session_factory, "alice", "alice-pass", telegram_id=777,
        telegram_id_confirmed=False,
    )
    await _add_server(session_factory, "gpu-alice", user.id, date(2026, 9, 16))
    message = _FakeMessage(chat_id=777, from_user=_FakeFromUser(777))
    await cmd_today(message)
    assert message.answers
    text, _ = message.answers[0]
    assert "привяжите аккаунт" in text
    assert "gpu-alice" not in text


async def test_digest_sent_per_user_only(session_factory):
    from app.bot.sender import send_digest_to_subscribers

    user1 = await _add_user(
        session_factory, "user1", "pass1234", telegram_id=111,
        telegram_id_confirmed=True,
    )
    user2 = await _add_user(
        session_factory, "user2", "pass1234", telegram_id=222,
        telegram_id_confirmed=True,
    )
    await _add_user(
        session_factory, "user3", "pass1234", telegram_id=333,
        telegram_id_confirmed=False,
    )
    await _add_server(session_factory, "gpu-111", user1.id, date(2026, 9, 16))
    await _add_server(session_factory, "gpu-222", user2.id, date(2026, 9, 16))
    async with session_factory() as session:
        session.add_all(
            [
                BotSubscriber(chat_id=111, user_id=user1.id, is_active=True),
                BotSubscriber(chat_id=222, user_id=user2.id, is_active=True),
                BotSubscriber(chat_id=333, is_active=True),
                BotSubscriber(chat_id=444, user_id=user1.id, is_active=False),
            ]
        )
        await session.commit()

    bot = _FakeBot()
    sent = await send_digest_to_subscribers(bot, session_factory)

    assert sent == 2
    messages = {chat_id: text for chat_id, text in bot.sent}
    assert 111 in messages and "gpu-111" in messages[111] and "gpu-222" not in messages[111]
    assert 222 in messages and "gpu-222" in messages[222] and "gpu-111" not in messages[222]
    assert 333 not in messages
    assert 444 not in messages


async def test_evening_reminder_only_for_users_with_overdue(session_factory):
    from app.bot.sender import send_evening_reminder_to_subscribers

    user1 = await _add_user(
        session_factory, "user1", "pass1234", telegram_id=111,
        telegram_id_confirmed=True,
    )
    user2 = await _add_user(
        session_factory, "user2", "pass1234", telegram_id=222,
        telegram_id_confirmed=True,
    )
    far_future = date(2999, 1, 1)
    await _add_server(session_factory, "gpu-overdue", user1.id, date(2020, 1, 1))
    await _add_server(session_factory, "gpu-far", user2.id, far_future)
    async with session_factory() as session:
        session.add_all(
            [
                BotSubscriber(chat_id=111, user_id=user1.id, is_active=True),
                BotSubscriber(chat_id=222, user_id=user2.id, is_active=True),
            ]
        )
        await session.commit()

    bot = _FakeBot()
    sent = await send_evening_reminder_to_subscribers(bot, session_factory)

    assert sent == 1
    messages = {chat_id: text for chat_id, text in bot.sent}
    assert 111 in messages and "gpu-overdue" in messages[111]
    assert 222 not in messages


async def test_cmd_start_restores_subscriber_cache(session_factory):
    from app.bot.handlers import cmd_start

    user = await _add_user(
        session_factory, "alice", "alice-pass", telegram_id=555,
        telegram_id_confirmed=True,
    )
    async with session_factory() as session:
        session.add(BotSubscriber(chat_id=555, is_active=True))
        await session.commit()

    message = _FakeMessage(chat_id=555, from_user=_FakeFromUser(555))
    await cmd_start(message)
    assert message.answers
    assert "Привет, alice!" in message.answers[0][0]
    async with session_factory() as session:
        sub = await session.scalar(
            select(BotSubscriber).where(BotSubscriber.chat_id == 555)
        )
        assert sub.user_id == user.id


async def test_unsubscribe_requires_binding(session_factory):
    from app.bot.handlers import cmd_unsubscribe

    message = _FakeMessage(chat_id=888, from_user=_FakeFromUser(888))
    await cmd_unsubscribe(message)
    assert message.answers
    assert "привяжите аккаунт" in message.answers[0][0]


async def test_add_requires_binding(session_factory):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers import cmd_add

    storage = MemoryStorage()
    ctx = FSMContext(storage, StorageKey(bot_id=1, chat_id=888, user_id=888))
    message = _FakeMessage(chat_id=888, from_user=_FakeFromUser(888))
    await cmd_add(message, ctx)
    assert message.answers
    assert "привяжите аккаунт" in message.answers[0][0]
    assert await ctx.get_state() is None


async def test_add_creates_owned_server(session_factory):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers import AddStates, add_enter_next_due

    user = await _add_user(
        session_factory, "alice", "alice-pass", telegram_id=777,
        telegram_id_confirmed=True,
    )
    storage = MemoryStorage()
    ctx = FSMContext(storage, StorageKey(bot_id=1, chat_id=777, user_id=777))
    await ctx.set_state(AddStates.next_due)
    await ctx.update_data(
        target_type="server",
        name="gpu-new",
        amount_rub=1000,
        period="monthly",
        owner_id=user.id,
    )
    message = _FakeMessage(chat_id=777, text="2026-10-01")
    await add_enter_next_due(message, ctx)
    assert message.answers
    assert "gpu-new" in message.answers[0][0]
    async with session_factory() as session:
        server = await session.scalar(select(Server).where(Server.name == "gpu-new"))
        assert server is not None
        assert server.owner_id == user.id


async def test_pay_button_on_foreign_target_rejected(session_factory):
    from app.bot.handlers import on_pay

    owner = await _add_user(
        session_factory, "owner", "pass1234", telegram_id=111,
        telegram_id_confirmed=True,
    )
    await _add_user(
        session_factory, "other", "pass1234", telegram_id=222,
        telegram_id_confirmed=True,
    )
    server_id = await _add_server(session_factory, "owner-gpu", owner.id, date(2026, 9, 16))

    callback = _FakeCallback(f"pay:server:{server_id}", chat_id=222)
    await on_pay(callback)
    assert callback.answers
    assert "не найдена" in callback.answers[0]

    async with session_factory() as session:
        server = await session.get(Server, server_id)
        assert server.next_due == date(2026, 9, 16)


async def test_pay_button_on_unbound_chat_sends_hint(session_factory):
    from app.bot.handlers import on_pay

    callback = _FakeCallback("pay:server:1", chat_id=999)
    await on_pay(callback)
    assert callback.answers
    assert callback.message.answers
    assert "привяжите аккаунт" in callback.message.answers[0]