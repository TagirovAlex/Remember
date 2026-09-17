import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import SCHEMA_VERSION_KEY, migrate_schema
from app.models import Base

OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(260) NOT NULL,
    role VARCHAR(20) NOT NULL,
    theme VARCHAR(10) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE servers (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    kind VARCHAR(20) NOT NULL,
    provider VARCHAR(100) NOT NULL,
    location VARCHAR(100),
    ip VARCHAR(45),
    specs TEXT,
    what_runs TEXT,
    panel_url VARCHAR(300),
    notes TEXT,
    amount_rub INTEGER NOT NULL,
    period VARCHAR(20) NOT NULL,
    next_due DATE NOT NULL,
    billing_anchor DATE,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE subscriptions (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    provider VARCHAR(100) NOT NULL,
    amount_rub INTEGER NOT NULL,
    period VARCHAR(20) NOT NULL,
    next_due DATE NOT NULL,
    billing_anchor DATE,
    auto_renewal BOOLEAN NOT NULL,
    status VARCHAR(20) NOT NULL,
    url VARCHAR(300),
    notes TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE one_time_expenses (
    id INTEGER NOT NULL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    amount_rub INTEGER NOT NULL,
    paid_date DATE NOT NULL,
    category VARCHAR(100),
    notes TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE payments (
    id INTEGER NOT NULL PRIMARY KEY,
    target_type VARCHAR(20) NOT NULL,
    target_id INTEGER NOT NULL,
    amount_rub INTEGER NOT NULL,
    due_date DATE NOT NULL,
    paid_date DATE NOT NULL,
    paid_by INTEGER,
    notes TEXT,
    created_at DATETIME NOT NULL,
    UNIQUE (target_type, target_id, due_date)
);
CREATE TABLE settings (
    key VARCHAR(100) NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    description VARCHAR(300),
    updated_at DATETIME NOT NULL
);
CREATE TABLE bot_subscribers (
    id INTEGER NOT NULL PRIMARY KEY,
    chat_id BIGINT NOT NULL UNIQUE,
    name VARCHAR(200),
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL
);
"""


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        for statement in OLD_SCHEMA.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    yield engine
    await engine.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _indexes(session: AsyncSession, table: str) -> set[str]:
    rows = (await session.execute(text(f"PRAGMA index_list({table})"))).all()
    return {row[1] for row in rows}


async def _columns(session: AsyncSession, table: str) -> set[str]:
    rows = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    return {row[1] for row in rows}


async def test_migrate_schema_adds_columns_and_backfills(engine, factory):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, theme, "
                "is_active, created_at, updated_at) "
                "VALUES (1, 'admin', 'x', 'admin', 'light', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO servers (id, name, kind, provider, amount_rub, period, "
                "next_due, is_active, created_at, updated_at) "
                "VALUES (1, 'gpu-1', 'gpu', 'h', 1000, 'monthly', '2026-09-16', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO subscriptions (id, name, provider, amount_rub, period, "
                "next_due, auto_renewal, status, created_at, updated_at) "
                "VALUES (1, 'netflix', 'n', 900, 'monthly', '2026-09-20', 1, "
                "'active', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO one_time_expenses (id, title, amount_rub, paid_date, "
                "created_at) VALUES (1, 'mouse', 100, '2026-09-01', '2026-09-01')"
            )
        )

    async with factory() as session:
        await migrate_schema(session)

        assert "owner_id" in await _columns(session, "servers")
        assert "owner_id" in await _columns(session, "subscriptions")
        assert "owner_id" in await _columns(session, "one_time_expenses")
        assert "user_id" in await _columns(session, "bot_subscribers")
        users_cols = await _columns(session, "users")
        assert "telegram_id" in users_cols
        assert "telegram_id_confirmed" in users_cols
        assert "session_epoch" in users_cols

        epochs = (await session.execute(text("SELECT session_epoch FROM users"))).scalars().all()
        assert epochs == [0]

        owners = (await session.execute(text("SELECT owner_id FROM servers"))).scalars().all()
        assert owners == [1]
        subs = (await session.execute(text("SELECT owner_id FROM subscriptions"))).scalars().all()
        assert subs == [1]
        expenses = (await session.execute(text("SELECT owner_id FROM one_time_expenses"))).scalars().all()
        assert expenses == [1]

        server_idx = await _indexes(session, "servers")
        assert "uq_servers_owner_name" in server_idx
        assert "sqlite_autoindex_servers_1" not in server_idx
        sub_idx = await _indexes(session, "subscriptions")
        assert "uq_subscriptions_owner_name" in sub_idx
        assert "sqlite_autoindex_subscriptions_1" not in sub_idx
        assert "uq_users_telegram_id" in await _indexes(session, "users")
        assert "ix_bot_subscribers_user_id" in await _indexes(session, "bot_subscribers")
        assert "ix_one_time_expenses_owner_id" in await _indexes(session, "one_time_expenses")

        version = await session.execute(
            text("SELECT value FROM settings WHERE key=:key"),
            {"key": SCHEMA_VERSION_KEY},
        )
        assert version.scalar() == "2"


async def test_migrate_schema_fallback_to_first_user(factory):
    async with factory() as session:
        await migrate_schema(session)
        owners = (await session.execute(text("SELECT owner_id FROM servers"))).scalars().all()
        assert owners == []

    # first (non-admin) user becomes the fallback owner
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, theme, "
                "is_active, created_at, updated_at) "
                "VALUES (1, 'someone', 'x', 'user', 'light', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO servers (id, name, kind, provider, amount_rub, period, "
                "next_due, is_active, created_at, updated_at) "
                "VALUES (1, 'gpu-1', 'gpu', 'h', 1000, 'monthly', '2026-09-16', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await session.commit()
        await migrate_schema(session)
        owners = (await session.execute(text("SELECT owner_id FROM servers"))).scalars().all()
        assert owners == [1]


async def test_migrate_schema_adds_session_epoch(engine, factory):
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, theme, "
                "is_active, created_at, updated_at) "
                "VALUES (1, 'admin', 'x', 'admin', 'light', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await session.commit()
        await migrate_schema(session)
    async with factory() as session:
        cols = await _columns(session, "users")
        assert "session_epoch" in cols
        value = (
            await session.execute(text("SELECT session_epoch FROM users WHERE id=1"))
        ).scalar()
        assert value == 0


async def test_migrate_schema_is_idempotent(engine, factory):
    async with factory() as session:
        await migrate_schema(session)
        server_idx_1 = await _indexes(session, "servers")
        version_1 = (
            await session.execute(
                text("SELECT value FROM settings WHERE key=:key"),
                {"key": SCHEMA_VERSION_KEY},
            )
        ).scalar()
        await migrate_schema(session)
        server_idx_2 = await _indexes(session, "servers")
        version_2 = (
            await session.execute(
                text("SELECT value FROM settings WHERE key=:key"),
                {"key": SCHEMA_VERSION_KEY},
            )
        ).scalar()
        assert server_idx_1 == server_idx_2
        assert version_1 == version_2 == "2"


async def test_migrate_schema_fresh_db_is_noop(factory):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await migrate_schema(session)
        assert "owner_id" in await _columns(session, "servers")
        assert "uq_users_telegram_id" in await _indexes(session, "users")
        covered = [
            name
            for name in await _indexes(session, "servers")
            if [r[2] for r in (await session.execute(text(f"PRAGMA index_xinfo('{name}')"))).all() if r[2] is not None]
            == ["owner_id", "name"]
        ]
        assert covered, "no unique index covers (owner_id, name)"
        version = (
            await session.execute(
                text("SELECT value FROM settings WHERE key=:key"),
                {"key": SCHEMA_VERSION_KEY},
            )
        ).scalar()
        assert version == "2"
        await migrate_schema(session)
    await engine.dispose()


async def test_migrate_schema_preserves_data(engine, factory):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, theme, "
                "is_active, created_at, updated_at) "
                "VALUES (1, 'admin', 'x', 'admin', 'light', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO servers (id, name, kind, provider, amount_rub, period, "
                "next_due, is_active, created_at, updated_at) "
                "VALUES (1, 'gpu-1', 'gpu', 'h', 1000, 'monthly', '2026-09-16', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO servers (id, name, kind, provider, amount_rub, period, "
                "next_due, is_active, created_at, updated_at) "
                "VALUES (2, 'gpu-2', 'vm', 'h', 2000, 'yearly', '2027-01-01', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
    async with factory() as session:
        await migrate_schema(session)
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT name, amount_rub, next_due, owner_id FROM servers ORDER BY id")
            )
        ).all()
        assert [(r[0], r[1], str(r[2]), r[3]) for r in rows] == [
            ("gpu-1", 1000, "2026-09-16", 1),
            ("gpu-2", 2000, "2027-01-01", 1),
        ]