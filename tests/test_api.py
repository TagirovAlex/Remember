from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, Server, User
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
        admin = await session.scalar(select(User).where(User.username == "admin"))
        session.add(
            User(
                username="alice",
                password_hash=hash_password("alice-pass"),
                role="user",
                is_active=True,
                telegram_id=111,
                telegram_id_confirmed=True,
            )
        )
        await session.commit()
        admin_id = admin.id
        session.add(
            Server(
                name="admin-server",
                kind="gpu",
                provider="Hetzner",
                amount_rub=1000,
                period="monthly",
                next_due=date(2026, 9, 16),
                is_active=True,
                owner_id=admin_id,
            )
        )
        alice = await session.scalar(select(User).where(User.username == "alice"))
        session.add(
            Server(
                name="alice-server",
                kind="vm",
                provider="Hetzner",
                amount_rub=500,
                period="monthly",
                next_due=date(2026, 9, 16),
                is_active=True,
                owner_id=alice.id,
            )
        )
        await session.commit()
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: httpx.AsyncClient, username: str, password: str):
    return await client.post(
        "/login", data={"username": username, "password": password}
    )


async def _server_id(session_factory, owner_name: str) -> int:
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.username == owner_name))
        server = await session.scalar(select(Server).where(Server.owner_id == owner.id))
        return server.id


async def test_api_mark_foreign_target_returns_404(client, session_factory):
    await _login(client, "alice", "alice-pass")
    admin_server_id = await _server_id(session_factory, "admin")
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "server",
            "target_id": admin_server_id,
            "amount_rub": 1000,
            "paid_date": "2026-09-16",
        },
    )
    assert response.status_code == 404


async def test_api_mark_own_target_ok_with_paid_by(client, session_factory):
    await _login(client, "alice", "alice-pass")
    alice_server_id = await _server_id(session_factory, "alice")
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "server",
            "target_id": alice_server_id,
            "amount_rub": 500,
            "paid_date": "2026-09-16",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["already"] is False
    async with session_factory() as session:
        alice = await session.scalar(select(User).where(User.username == "alice"))
        assert data["payment"]["paid_by"] == alice.id


async def test_api_mark_missing_target_returns_404(client):
    await _login(client, "alice", "alice-pass")
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "server",
            "target_id": 9999,
            "amount_rub": 1000,
            "paid_date": "2026-09-16",
        },
    )
    assert response.status_code == 404


async def test_api_requires_auth(client):
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "server",
            "target_id": 1,
            "amount_rub": 1000,
            "paid_date": "2026-09-16",
        },
    )
    assert response.status_code == 401