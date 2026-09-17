import logging
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import event, text
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from app.config import settings
from app.models import Base, Server, Setting, Subscription

logger = logging.getLogger(__name__)

SCHEMA_VERSION_KEY = "schema_version"
SCHEMA_VERSION = "2"

_COLUMN_DEFS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("telegram_id", "BIGINT"),
        ("telegram_id_confirmed", "BOOLEAN NOT NULL DEFAULT 0"),
        ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "servers": [
        ("owner_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
        ("image", "VARCHAR(255)"),
    ],
    "subscriptions": [
        ("owner_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
        ("image", "VARCHAR(255)"),
    ],
    "one_time_expenses": [
        ("owner_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
        ("image", "VARCHAR(255)"),
    ],
    "bot_subscribers": [("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
}

_OWNER_TABLES = ("servers", "subscriptions", "one_time_expenses")

# Old global unique indexes on the name column; SQLAlchemy rendered the
# UNIQUE (name) constraint as an auto-index, so both names may be present.
_DROP_INDEX_NAMES: dict[str, set[str]] = {
    "servers": {"sqlite_autoindex_servers_1", "ix_servers_name"},
    "subscriptions": {"sqlite_autoindex_subscriptions_1", "ix_subscriptions_name"},
}

_REBUILD_MODELS = {"servers": Server, "subscriptions": Subscription}

_CREATE_INDEXES: list[str] = [
    "CREATE UNIQUE INDEX uq_users_telegram_id ON users(telegram_id)",
    "CREATE UNIQUE INDEX uq_servers_owner_name ON servers(owner_id, name)",
    "CREATE UNIQUE INDEX uq_subscriptions_owner_name ON subscriptions(owner_id, name)",
    "CREATE INDEX ix_bot_subscribers_user_id ON bot_subscribers(user_id)",
    "CREATE INDEX ix_one_time_expenses_owner_id ON one_time_expenses(owner_id)",
]


def _make_engine() -> AsyncEngine:
    path = settings.database_path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{path}")


engine = _make_engine()


@event.listens_for(engine.sync_engine, "connect")
def _enable_foreign_keys(dbapi_connection, _) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a database session."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables that do not exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _table_columns(session: AsyncSession, table: str) -> set[str]:
    rows = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    return {row[1] for row in rows}


async def _table_indexes(session: AsyncSession, table: str) -> set[str]:
    rows = (await session.execute(text(f"PRAGMA index_list({table})"))).all()
    return {row[1] for row in rows}


async def _backfill_owner(session: AsyncSession) -> int | None:
    """Assign every ownerless row to the admin (fallback: the first user)."""
    admin_id = await session.scalar(
        text("SELECT id FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1")
    )
    if admin_id is None:
        admin_id = await session.scalar(
            text("SELECT id FROM users ORDER BY id ASC LIMIT 1")
        )
    if admin_id is None:
        return None
    for table in _OWNER_TABLES:
        await session.execute(
            text(f"UPDATE {table} SET owner_id = :admin_id WHERE owner_id IS NULL"),
            {"admin_id": admin_id},
        )
    return admin_id


async def _rebuild_table(session: AsyncSession, table: str) -> None:
    """Recreate ``table`` without the global unique name constraint.

    SQLite refuses to DROP INDEX an auto-index that backs a UNIQUE constraint
    (``index associated with UNIQUE or PRIMARY KEY constraint cannot be
    dropped``); rebuilding the table is the only in-place way to remove it.
    All columns are copied, so no data is lost. The per-owner composite unique
    index is created later by the caller so it exists exactly once.
    """
    temp = f"{table}__migrate"
    await session.execute(text(f"DROP TABLE IF EXISTS {temp}"))
    ddl = str(
        CreateTable(_REBUILD_MODELS[table].__table__).compile(
            dialect=sqlite_dialect.dialect()
        )
    )
    ddl = re.sub(
        r"\n\tCONSTRAINT uq_(?:servers|subscriptions)_owner_name "
        r"UNIQUE \(owner_id, name\),",
        "",
        ddl,
    )
    await session.execute(
        text(ddl.replace(f"CREATE TABLE {table} ", f"CREATE TABLE {temp} ", 1))
    )
    columns = ", ".join(
        row[1]
        for row in (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    )
    await session.execute(
        text(f"INSERT INTO {temp} ({columns}) SELECT {columns} FROM {table}")
    )
    await session.execute(text(f"DROP TABLE {table}"))
    await session.execute(text(f"ALTER TABLE {temp} RENAME TO {table}"))


async def _index_columns(session: AsyncSession, table: str, index: str) -> list[str]:
    rows = (
        await session.execute(text(f"PRAGMA index_xinfo('{index}')"))
    ).all()
    return [row[2] for row in rows if row[2] is not None]


async def _drop_name_constraints(session: AsyncSession) -> bool:
    """Remove the global unique constraint on the name column if present.

    Returns True when anything changed. A plain DROP INDEX works on SQLite
    builds that allow it; otherwise the table is rebuilt without the
    constraint (the old constraint always surfaces as an auto-index).
    """
    changed = False
    for table in _DROP_INDEX_NAMES:
        for index_name in await _table_indexes(session, table):
            if await _index_columns(session, table, index_name) != ["name"]:
                continue
            try:
                await session.execute(text(f"DROP INDEX {index_name}"))
            except OperationalError:
                await _rebuild_table(session, table)
            changed = True
            break
    return changed


async def migrate_schema(session: AsyncSession) -> None:
    """Bring an existing database up to the multiuser schema; idempotent.

    Adds the new columns via ALTER (SQLite 3.37+ supports REFERENCES in
    ADD COLUMN), backfills owners to the admin user, replaces the global
    unique name indexes with per-owner composite ones and records the schema
    version in the settings table. Every step checks the physical schema first,
    so re-running is a no-op and a partially applied migration finishes on the
    next start.
    """
    changed = False

    for table, columns in _COLUMN_DEFS.items():
        existing = await _table_columns(session, table)
        for name, definition in columns:
            if name in existing:
                continue
            await session.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            )
            changed = True

    owner_id = await _backfill_owner(session)
    if owner_id is None:
        logger.warning("migrate_schema: no users exist, owner backfill skipped")
    elif await _drop_name_constraints(session):
        changed = True

    for statement in _CREATE_INDEXES:
        parts = statement.split()
        index_name = parts[3] if parts[1] == "UNIQUE" else parts[2]
        table = statement.split("ON ")[1].split("(")[0]
        columns = statement.split("(", 1)[1].rsplit(")", 1)[0]
        column_list = [c.strip() for c in columns.split(",")]
        existing = await _table_indexes(session, table)
        if index_name in existing:
            continue
        covered = False
        for name in existing:
            if await _index_columns(session, table, name) == column_list:
                covered = True
                break
        if covered:
            continue
        try:
            await session.execute(text(statement))
        except IntegrityError:
            # Anomalous DB with duplicate (owner, name); fall back to a plain
            # index so the app stays usable. Fix the duplicates manually.
            await session.rollback()
            logger.warning(
                "unique index %s could not be created (duplicates); "
                "falling back to a plain index",
                index_name,
            )
            await session.execute(text(f"CREATE INDEX {index_name} ON {table}({columns})"))
        changed = True

    if await session.get(Setting, SCHEMA_VERSION_KEY) is None:
        session.add(
            Setting(
                key=SCHEMA_VERSION_KEY,
                value=SCHEMA_VERSION,
                updated_at=datetime.now(UTC),
            )
        )
        changed = True

    if changed:
        await session.commit()