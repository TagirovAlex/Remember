import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, User

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


async def _login(client: httpx.AsyncClient):
    return await client.post(
        "/login", data={"username": "admin", "password": PASSWORD}
    )


async def test_set_theme_dark(client, session_factory):
    await _login(client)
    response = await client.post("/theme", data={"theme": "dark"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    async with session_factory() as session:
        from sqlalchemy import select
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user.theme == "dark"


async def test_set_theme_light(client, session_factory):
    await _login(client)
    response = await client.post("/theme", data={"theme": "light"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    async with session_factory() as session:
        from sqlalchemy import select
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user.theme == "light"


async def test_set_theme_invalid(client):
    await _login(client)
    response = await client.post("/theme", data={"theme": "blue"})
    assert response.status_code == 400
    assert response.json()["ok"] is False


async def test_default_theme_is_light(client, session_factory):
    async with session_factory() as session:
        from sqlalchemy import select
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user.theme == "light"


async def test_page_renders_data_theme(client):
    await _login(client)
    await client.post("/theme", data={"theme": "dark"})
    response = await client.get("/")
    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text
