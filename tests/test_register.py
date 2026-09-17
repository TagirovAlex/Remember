from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, User
from app.ratelimit import rate_limiter

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
def _reset_rate_limit():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


async def test_register_page_open_for_anonymous(client):
    response = await client.get("/register")
    assert response.status_code == 200
    assert "Регистрация" in response.text


async def test_register_creates_user_and_logs_in(client, session_factory):
    response = await client.post(
        "/register",
        data={
            "username": "newbie",
            "password": "pass1234",
            "confirm": "pass1234",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(SID)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "newbie"))
        assert user is not None
        assert user.role == "user"
        assert user.is_active is True


async def test_register_duplicate_username(client, session_factory):
    await client.post(
        "/register",
        data={"username": "first", "password": "pass1234", "confirm": "pass1234"},
    )
    response = await client.post(
        "/register",
        data={"username": "first", "password": "other123", "confirm": "other123"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "уже существует" in unquote(response.headers["location"])
    async with session_factory() as session:
        users = (await session.scalars(select(User).where(User.username == "first"))).all()
        assert len(users) == 1


async def test_register_short_password_rejected(client, session_factory):
    response = await client.post(
        "/register",
        data={"username": "shorty", "password": "abc", "confirm": "abc"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    async with session_factory() as session:
        assert (
            await session.scalar(select(User).where(User.username == "shorty"))
        ) is None


async def test_register_password_mismatch(client, session_factory):
    response = await client.post(
        "/register",
        data={
            "username": "mismatch",
            "password": "pass1234",
            "confirm": "pass9999",
        },
    )
    assert response.status_code == 303
    assert "Пароли не совпадают" in unquote(response.headers["location"])
    async with session_factory() as session:
        assert (
            await session.scalar(select(User).where(User.username == "mismatch"))
        ) is None


async def test_register_rate_limit_429(client, session_factory):
    for i in range(5):
        response = await client.post(
            "/register",
            data={
                "username": f"user{i}",
                "password": "pass1234",
                "confirm": "pass1234",
            },
        )
        assert response.status_code == 303
    response = await client.post(
        "/register",
        data={"username": "sixth", "password": "pass1234", "confirm": "pass1234"},
    )
    assert response.status_code == 429
    async with session_factory() as session:
        assert (
            await session.scalar(select(User).where(User.username == "sixth"))
        ) is None


async def test_register_does_not_create_admin(client, session_factory):
    await client.post(
        "/register",
        data={"username": "first-user", "password": "pass1234", "confirm": "pass1234"},
    )
    async with session_factory() as session:
        admins = (await session.scalars(select(User).where(User.role == "admin"))).all()
        assert [user.username for user in admins] == ["admin"]