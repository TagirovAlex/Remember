from datetime import date
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import (
    Base,
    BotSubscriber,
    OneTimeExpense,
    Payment,
    Server,
    Setting,
    Subscription,
    User,
)
from app.security import hash_password, verify_password

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
    return await client.post(
        "/login", data={"username": username, "password": password}
    )


async def _add_user(session_factory, username, password, role="user", is_active=True):
    async with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def test_login_rejects_wrong_password(client):
    response = await _login(client, password="wrong")
    assert response.status_code == 303
    assert "/login?error=" in response.headers["location"]


async def test_inactive_user_cannot_login(client, session_factory):
    await _add_user(session_factory, "inactive", "pass1234", is_active=False)
    response = await _login(client, "inactive", "pass1234")
    assert response.status_code == 303
    assert "/login?error=" in response.headers["location"]


async def test_session_works_after_login(client):
    response = await _login(client)
    assert response.status_code == 303
    response = await client.get("/")
    assert response.status_code == 200


async def test_admin_creates_user(client, session_factory):
    await _login(client)
    response = await client.post(
        "/users",
        data={"username": "newuser", "password": "pass1234", "role": "user", "is_active": "on"},
    )
    assert response.status_code == 303
    assert "/users?msg=" in response.headers["location"]
    async with session_factory() as session:
        from sqlalchemy import select
        user = await session.scalar(select(User).where(User.username == "newuser"))
        assert user is not None
        assert user.role == "user"


async def test_admin_edits_user(client, session_factory):
    target = await _add_user(session_factory, "editor", "pass1234")
    await _login(client)
    response = await client.post(
        f"/users/{target.id}",
        data={"role": "admin", "is_active": "on"},
    )
    assert response.status_code == 303
    assert "/users?msg=" in response.headers["location"]
    async with session_factory() as session:
        user = await session.get(User, target.id)
        assert user.role == "admin"


async def test_admin_deletes_user(client, session_factory):
    target = await _add_user(session_factory, "deleteme", "pass1234")
    await _login(client)
    response = await client.post(f"/users/{target.id}/delete")
    assert response.status_code == 303
    assert "/users?msg=" in response.headers["location"]
    async with session_factory() as session:
        assert await session.get(User, target.id) is None


async def test_non_admin_gets_403(client, session_factory):
    await _add_user(session_factory, "viewer", "pass1234", role="user")
    await _login(client, "viewer", "pass1234")
    response = await client.get("/users")
    assert response.status_code == 403


async def test_cannot_delete_self(client, session_factory):
    await _login(client)
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
    response = await client.post(f"/users/{admin_id}/delete")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "себя" in unquote(response.headers["location"])
    async with session_factory() as session:
        assert await session.get(User, admin_id) is not None


async def test_cannot_deactivate_last_admin(client, session_factory):
    await _login(client)
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
    response = await client.post(
        f"/users/{admin_id}",
        data={"role": "user", "is_active": "on"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "деактивировать" in unquote(response.headers["location"])
    async with session_factory() as session:
        user = await session.get(User, admin_id)
        assert user.role == "admin"
        assert user.is_active is True


async def test_mark_paid_via_web_saves_paid_by(client, session_factory):
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="Pay-Test",
            kind="gpu",
            provider="Test",
            amount_rub=1000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
        server_id = server.id
    await _login(client)
    response = await client.post(f"/servers/{server_id}/pay", data={"next": "/servers"})
    assert response.status_code == 303
    async with session_factory() as session:
        from sqlalchemy import select
        payment = await session.scalar(
            select(Payment).where(Payment.target_type == "server", Payment.target_id == server_id)
        )
        assert payment is not None
        admin = await session.scalar(select(User).where(User.username == "admin"))
        assert payment.paid_by == admin.id


async def test_mark_paid_via_api_saves_paid_by(client, session_factory):
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="API-Pay-Test",
            kind="vm",
            provider="Test",
            amount_rub=500,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
        server_id = server.id
    await _login(client)
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "server",
            "target_id": server_id,
            "amount_rub": 500,
            "paid_date": "2026-09-16",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["payment"]["paid_by"] is not None
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        assert data["payment"]["paid_by"] == admin.id


async def test_bot_payment_no_paid_by(session_factory):
    from app.services.billing import mark_paid_once
    async with session_factory() as session:
        admin = User(
            username="admin",
            password_hash=hash_password("x"),
            role="admin",
        )
        session.add(admin)
        await session.commit()
        server = Server(
            name="Bot-Pay-Test",
            kind="vm",
            provider="Test",
            amount_rub=200,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
        server_id = server.id
    async with session_factory() as session:
        payment, created = await mark_paid_once(
            session, "server", server_id, 200, date(2026, 9, 16), owner_id=admin.id
        )
        await session.commit()
        assert created is True
        assert payment.paid_by is None


async def test_migration_creates_admin(engine):
    from argon2 import PasswordHasher
    hasher = PasswordHasher()
    test_hash = hasher.hash("legacy-pass")
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(
            Setting(key="auth_password_hash", value=test_hash)
        )
        await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await ensure_admin_user(session)
    async with factory() as session:
        from sqlalchemy import select
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        assert user.role == "admin"
        assert verify_password("legacy-pass", user.password_hash)
        setting = await session.get(Setting, "auth_password_hash")
        assert setting is None


async def test_change_password(client, session_factory):
    await _login(client)
    response = await client.post(
        "/settings/password",
        data={"old": PASSWORD, "new": "new-pass-1234", "confirm": "new-pass-1234"},
    )
    assert response.status_code == 303
    assert "msg" in response.headers["location"]
    response = await _login(client, "admin", "new-pass-1234")
    assert response.status_code == 303
    assert client.cookies.get(SID) is not None


async def test_change_password_wrong_old(client):
    await _login(client)
    response = await client.post(
        "/settings/password",
        data={"old": "wrong-old", "new": "new-pass-1234", "confirm": "new-pass-1234"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


async def test_admin_sees_only_own_data(client, session_factory):
    await _add_user(session_factory, "other", "other-pass")
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        other = await session.scalar(select(User).where(User.username == "other"))
        session.add(
            Server(
                name="admin-server",
                kind="gpu",
                provider="Hetzner",
                amount_rub=1000,
                period="monthly",
                next_due=date(2026, 9, 16),
                is_active=True,
                owner_id=admin.id,
            )
        )
        session.add(
            Server(
                name="other-server",
                kind="vm",
                provider="Hetzner",
                amount_rub=500,
                period="monthly",
                next_due=date(2026, 9, 16),
                is_active=True,
                owner_id=other.id,
            )
        )
        await session.commit()
    await _login(client)
    response = await client.get("/servers")
    assert response.status_code == 200
    assert "admin-server" in response.text
    assert "other-server" not in response.text


async def test_delete_user_cascades(client, session_factory):
    await _add_user(session_factory, "victim", "victim-pass")
    async with session_factory() as session:
        from sqlalchemy import select
        admin = await session.scalar(select(User).where(User.username == "admin"))
        victim = await session.scalar(select(User).where(User.username == "victim"))
        server = Server(
            name="victim-server",
            kind="gpu",
            provider="Hetzner",
            amount_rub=1000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=victim.id,
        )
        sub = Subscription(
            name="victim-sub",
            provider="Netflix",
            amount_rub=900,
            period="monthly",
            next_due=date(2026, 9, 20),
            status="active",
            owner_id=victim.id,
        )
        session.add_all([server, sub])
        await session.flush()
        session.add(
            Payment(
                target_type="server",
                target_id=server.id,
                amount_rub=1000,
                due_date=date(2026, 9, 16),
                paid_date=date(2026, 9, 16),
                paid_by=victim.id,
            )
        )
        admin_server = Server(
            name="admin-server",
            kind="vm",
            provider="Hetzner",
            amount_rub=500,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(admin_server)
        await session.flush()
        session.add(
            Payment(
                target_type="server",
                target_id=admin_server.id,
                amount_rub=500,
                due_date=date(2026, 9, 16),
                paid_date=date(2026, 9, 16),
                paid_by=victim.id,
            )
        )
        session.add(
            OneTimeExpense(
                title="victim-expense",
                amount_rub=100,
                paid_date=date(2026, 9, 1),
                owner_id=victim.id,
            )
        )
        session.add(
            BotSubscriber(chat_id=999, user_id=victim.id, is_active=True)
        )
        await session.commit()
        victim_id = victim.id
        admin_server_id = admin_server.id

    await _login(client)
    response = await client.post(f"/users/{victim_id}/delete")
    assert response.status_code == 303

    async with session_factory() as session:
        from sqlalchemy import select
        assert await session.get(User, victim_id) is None
        assert (
            await session.scalar(select(Server).where(Server.name == "victim-server"))
        ) is None
        assert (
            await session.scalar(
                select(Subscription).where(Subscription.name == "victim-sub")
            )
        ) is None
        assert (
            await session.scalar(
                select(OneTimeExpense).where(OneTimeExpense.title == "victim-expense")
            )
        ) is None
        payments = (await session.scalars(select(Payment))).all()
        assert len(payments) == 1
        assert payments[0].target_id == admin_server_id
        assert payments[0].paid_by is None
        sub = await session.scalar(
            select(BotSubscriber).where(BotSubscriber.chat_id == 999)
        )
        assert sub is not None
        assert sub.user_id is None
        assert await session.get(Server, admin_server_id) is not None
