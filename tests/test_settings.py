"""Settings service tests: proxy getters and SOCKS5 URL building."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.services.settings import ensure_defaults, proxy_port, proxy_url, set_setting


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


async def _set(factory, **values: str) -> None:
    async with factory() as session:
        for key, value in values.items():
            await set_setting(session, key, value)
        await session.commit()


async def test_proxy_url_encodes_user_and_password(factory):
    await _set(
        factory,
        proxy_enabled="true",
        proxy_host="127.0.0.1",
        proxy_port="1080",
        proxy_user="user",
        proxy_password="p@ss:wo/rd",
    )
    async with factory() as session:
        assert await proxy_url(session) == "socks5://user:p%40ss%3Awo%2Frd@127.0.0.1:1080"


async def test_proxy_url_empty_when_disabled(factory):
    await _set(factory, proxy_enabled="false")
    async with factory() as session:
        assert await proxy_url(session) == ""


async def test_proxy_url_empty_when_host_missing(factory):
    await _set(factory, proxy_enabled="true", proxy_host="")
    async with factory() as session:
        assert await proxy_url(session) == ""


async def test_proxy_url_default_port_1080(factory):
    await _set(factory, proxy_enabled="true", proxy_host="proxy.example.com")
    async with factory() as session:
        assert await proxy_url(session) == "socks5://proxy.example.com:1080"


async def test_proxy_port_none_when_empty(factory):
    async with factory() as session:
        assert await proxy_port(session) is None


async def test_set_proxy_port_empty_is_allowed(factory):
    await _set(factory, proxy_port="")
    async with factory() as session:
        assert await proxy_port(session) is None