import time as _time_module
from http.cookies import SimpleCookie

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import (
    create_session_cookie,
    ensure_admin_user,
    get_session_secret,
)
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, User
from app.security import hash_password, verify_session
from app.services import settings as settings_svc

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


async def _login(client: httpx.AsyncClient, username: str = "admin", password: str = PASSWORD):
    response = await client.post(
        "/login", data={"username": username, "password": password}
    )
    assert response.status_code == 303
    return response


async def _add_user(session_factory, username, password, role="user"):
    async with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _raw_token(response: httpx.Response) -> str:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[SID].value


def _set_cookie(client: httpx.AsyncClient, token: str) -> None:
    client.cookies.set(SID, token, domain="test.local")


async def test_login_sets_cookie_with_session_epoch(client, session_factory):
    response = await _login(client)
    token = _raw_token(response)
    async with session_factory() as session:
        secret = await get_session_secret(session)
        admin = await session.scalar(select(User).where(User.username == "admin"))
    payload = verify_session(token, secret, 24 * 3600)
    assert payload == {"sub": str(admin.id), "epoch": 0}


async def test_session_survives_configured_ttl(client, session_factory):
    async with session_factory() as session:
        await settings_svc.set_setting(session, "session_ttl_days", "1")
        await session.commit()
    await _login(client)
    response = await client.get("/")
    assert response.status_code == 200


async def test_session_ttl_days_default_is_one(session_factory):
    async with session_factory() as session:
        assert await settings_svc.session_ttl_days(session) == 1


async def test_settings_page_shows_session_ttl(client):
    await _login(client)
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "session_ttl_days" in response.text
    assert "Срок жизни веб-сессии" in response.text


async def test_session_ttl_days_validation(client, session_factory):
    await _login(client)
    response = await client.post("/settings", data={"session_ttl_days": "0"})
    assert "error=" in response.headers["location"]
    response = await client.post("/settings", data={"session_ttl_days": "31"})
    assert "error=" in response.headers["location"]
    response = await client.post("/settings", data={"session_ttl_days": "3"})
    assert response.status_code == 303
    async with session_factory() as session:
        assert await settings_svc.session_ttl_days(session) == 3


async def test_expired_token_rejected_by_verify_session(app, session_factory):
    async with session_factory() as session:
        secret = await get_session_secret(session)
        admin = await session.scalar(select(User).where(User.username == "admin"))
    token = create_session_cookie(secret, admin.id, admin.session_epoch, 30 * 24 * 3600)
    assert verify_session(token, secret, 30 * 24 * 3600) is not None
    assert verify_session(token, secret, -1) is None


async def test_expired_cookie_logs_user_out(client, session_factory, monkeypatch):
    async with session_factory() as session:
        secret = await get_session_secret(session)
        admin = await session.scalar(select(User).where(User.username == "admin"))
    token = create_session_cookie(secret, admin.id, admin.session_epoch, 24 * 3600)
    _set_cookie(client, token)
    assert (await client.get("/")).status_code == 200

    import itsdangerous.timed as timed_mod

    real_time = _time_module.time
    monkeypatch.setattr(timed_mod.time, "time", lambda: real_time() + 2 * 24 * 3600)
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_tampered_cookie_rejected(client):
    response = await _login(client)
    token = _raw_token(response)
    _set_cookie(client, token[:-3] + "xxx")
    response = await client.get("/")
    assert response.status_code == 303


async def test_change_password_keeps_current_session_and_kills_old(
    client, session_factory
):
    old_token = _raw_token(await _login(client))
    response = await client.post(
        "/settings/password",
        data={"old": PASSWORD, "new": "new-pass-1234", "confirm": "new-pass-1234"},
    )
    assert response.status_code == 303
    new_token = _raw_token(response)
    assert new_token and new_token != old_token
    assert (await client.get("/")).status_code == 200

    _set_cookie(client, old_token)
    assert (await client.get("/")).status_code == 303

    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        assert admin.session_epoch == 1


async def test_admin_password_change_kills_user_sessions(app, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as alice:
        alice_token = _raw_token(
            await alice.post("/login", data={"username": "alice", "password": "alice-pass"})
        )
        assert (await alice.get("/")).status_code == 200

        async with session_factory() as session:
            alice_id = (
                await session.scalar(select(User).where(User.username == "alice"))
            ).id

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin:
            await admin.post("/login", data={"username": "admin", "password": PASSWORD})
            response = await admin.post(
                f"/users/{alice_id}",
                data={
                    "role": "user",
                    "is_active": "on",
                    "password": "new-pass-42",
                },
            )
            assert response.status_code == 303

        _set_cookie(alice, alice_token)
        assert (await alice.get("/")).status_code == 303

        async with session_factory() as session:
            alice_user = await session.scalar(
                select(User).where(User.username == "alice")
            )
            assert alice_user.session_epoch == 1

        response = await alice.post(
            "/login", data={"username": "alice", "password": "new-pass-42"}
        )
        assert response.status_code == 303
        assert (await alice.get("/")).status_code == 200