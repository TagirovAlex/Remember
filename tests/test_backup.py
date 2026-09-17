import sqlite3

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, User
from app.security import hash_password
from app.services.backup import (
    create_backup,
    list_backups,
    prune_backups,
    resolve_backup,
)

PASSWORD = "secret-pass"


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
def db_path(tmp_path, monkeypatch):
    db_file = tmp_path / "remember.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO servers VALUES (1, 'GPU-1')")
    conn.execute("INSERT INTO servers VALUES (2, 'GPU-2')")
    conn.execute("INSERT INTO servers VALUES (3, 'GPU-3')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "database_path", str(db_file))
    return db_file


@pytest.fixture
async def app(engine, session_factory, monkeypatch, db_path):
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


async def _add_user(session_factory, username, password, role="user"):
    async with session_factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
            )
        )
        await session.commit()


async def test_create_backup(db_path):
    path = await create_backup()
    assert path.exists()
    assert path.name.startswith("remember-")
    assert path.name.endswith(".db")
    conn = sqlite3.connect(path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='servers'"
    ).fetchone()
    assert tables is not None
    rows = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
    assert rows == 3
    conn.close()


async def test_list_backups(db_path):
    await create_backup()
    await create_backup()
    backups = list_backups()
    assert len(backups) == 2
    assert all("name" in b and "size" in b for b in backups)


async def test_prune_backups(db_path):
    await create_backup()
    await create_backup()
    await create_backup()
    removed = await prune_backups(2)
    assert removed == 1
    assert len(list_backups()) == 2


async def test_resolve_backup_traversal(db_path):
    assert resolve_backup("../../secret") is None


async def test_resolve_backup_valid(db_path):
    path = await create_backup()
    result = resolve_backup(path.name)
    assert result is not None
    assert result == path


async def test_resolve_backup_bad_name():
    assert resolve_backup("not-a-backup.db") is None


async def test_backups_page_admin(client, db_path):
    await _login(client)
    response = await client.get("/backups")
    assert response.status_code == 200


async def test_backups_page_non_admin(client, session_factory, db_path):
    await _add_user(session_factory, "viewer", "pass1234", role="user")
    await _login(client, "viewer", "pass1234")
    response = await client.get("/backups")
    assert response.status_code == 403


async def test_backups_create_via_web(client, db_path):
    await _login(client)
    response = await client.post("/backups/create")
    assert response.status_code == 303
    assert len(list_backups()) == 1


async def test_backups_download(client, db_path):
    path = await create_backup()
    await _login(client)
    response = await client.get(f"/backups/download/{path.name}")
    assert response.status_code == 200


async def test_backups_download_404(client, db_path):
    await create_backup()
    await _login(client)
    response = await client.get("/backups/download/../../secret")
    assert response.status_code == 404


async def test_backups_delete(client, db_path):
    path = await create_backup()
    await _login(client)
    response = await client.post(f"/backups/{path.name}/delete")
    assert response.status_code == 303
    assert not path.exists()


async def test_backups_non_admin_create_403(client, session_factory, db_path):
    await _add_user(session_factory, "viewer2", "pass1234", role="user")
    await _login(client, "viewer2", "pass1234")
    response = await client.post("/backups/create")
    assert response.status_code == 403


async def test_backup_setting_validation_backup_keep_too_large(client):
    await _login(client)
    response = await client.post(
        "/settings",
        data={
            "morning_enabled": "on",
            "morning_time": "09:00",
            "evening_reminder_enabled": "on",
            "evening_reminder_time": "19:00",
            "remind_soon_days": "7",
            "tz": "Europe/Moscow",
            "backup_keep": "200",
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


async def test_backup_setting_validation_time_invalid(client):
    await _login(client)
    response = await client.post(
        "/settings",
        data={
            "morning_enabled": "on",
            "morning_time": "09:00",
            "evening_reminder_enabled": "on",
            "evening_reminder_time": "19:00",
            "remind_soon_days": "7",
            "tz": "Europe/Moscow",
            "backup_time": "9:00",
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


async def test_backup_setting_validation_time_valid(client):
    await _login(client)
    response = await client.post(
        "/settings",
        data={
            "morning_enabled": "on",
            "morning_time": "09:00",
            "evening_reminder_enabled": "on",
            "evening_reminder_time": "19:00",
            "remind_soon_days": "7",
            "tz": "Europe/Moscow",
            "backup_time": "09:00",
        },
    )
    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
