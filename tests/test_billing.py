from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, Payment, Server, Subscription
from app.services.billing import (
    add_period,
    advance_next_due,
    collect_digest,
    mark_paid,
    mark_paid_once,
)


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _add_server(session: AsyncSession, name: str, amount: int, next_due: date, **kw) -> Server:
    server = Server(
        name=name,
        kind=kw.pop("kind", "gpu"),
        provider=kw.pop("provider", "TestProvider"),
        amount_rub=amount,
        period=kw.pop("period", "monthly"),
        next_due=next_due,
        owner_id=kw.pop("owner_id", 1),
        **kw,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return server


async def _add_subscription(
    session: AsyncSession, name: str, amount: int, next_due: date, **kw
) -> Subscription:
    subscription = Subscription(
        name=name,
        provider=kw.pop("provider", "TestProvider"),
        amount_rub=amount,
        period=kw.pop("period", "monthly"),
        next_due=next_due,
        owner_id=kw.pop("owner_id", 1),
        **kw,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription


@pytest.mark.parametrize(
    "start,period,expected",
    [
        (date(2026, 1, 31), "monthly", date(2026, 2, 28)),
        (date(2024, 1, 31), "monthly", date(2024, 2, 29)),
        (date(2026, 3, 31), "monthly", date(2026, 4, 30)),
        (date(2026, 4, 30), "monthly", date(2026, 5, 30)),
        (date(2026, 1, 31), "quarterly", date(2026, 4, 30)),
        (date(2026, 11, 30), "quarterly", date(2027, 2, 28)),
        (date(2026, 1, 15), "yearly", date(2027, 1, 15)),
        (date(2024, 2, 29), "yearly", date(2025, 2, 28)),
        (date(2016, 12, 31), "monthly", date(2017, 1, 31)),
    ],
)
def test_add_period(start, period, expected):
    assert add_period(start, period) == expected


def test_add_period_invalid_period():
    with pytest.raises(ValueError):
        add_period(date(2026, 1, 1), "weekly")


def test_advance_next_due_from_original_date():
    assert advance_next_due(date(2026, 1, 31), "monthly", date(2026, 3, 5)) == date(2026, 2, 28)
    assert advance_next_due(date(2024, 1, 31), "monthly", date(2024, 2, 10)) == date(2024, 2, 29)
    assert advance_next_due(date(2026, 6, 10), "quarterly") == date(2026, 9, 10)


async def test_mark_paid_creates_payment_and_rolls_over(session):
    server = await _add_server(session, "gpu-a", 12000, date(2026, 9, 16))
    payment = await mark_paid(
        session, "server", server.id, 12000, date(2026, 9, 16), notes="оплата",
        owner_id=1,
    )
    await session.commit()
    await session.refresh(server)
    assert payment.target_type == "server"
    assert payment.target_id == server.id
    assert payment.due_date == date(2026, 9, 16)
    assert payment.paid_date == date(2026, 9, 16)
    assert server.next_due == date(2026, 10, 16)


async def test_mark_paid_idempotent_same_period(session):
    server = await _add_server(session, "gpu-b", 1000, date(2026, 9, 16))
    session.add(
        Payment(
            target_type="server",
            target_id=server.id,
            amount_rub=1000,
            due_date=date(2026, 9, 16),
            paid_date=date(2026, 9, 16),
        )
    )
    await session.commit()
    payment = await mark_paid(session, "server", server.id, 1000, date(2026, 9, 16), owner_id=1)
    await session.commit()
    await session.refresh(server)
    assert payment.due_date == date(2026, 9, 16)
    assert server.next_due == date(2026, 9, 16)
    payments = (await session.scalars(select(Payment))).all()
    assert len(payments) == 1


async def test_mark_paid_rolls_subscription(session):
    sub = await _add_subscription(session, "netflix", 900, date(2026, 9, 20))
    await mark_paid(session, "subscription", sub.id, 900, date(2026, 9, 20), owner_id=1)
    await session.commit()
    await session.refresh(sub)
    assert sub.next_due == date(2026, 10, 20)


async def test_mark_paid_missing_target(session):
    with pytest.raises(ValueError):
        await mark_paid(session, "server", 999, 1000, date(2026, 9, 16), owner_id=1)


async def test_mark_paid_bad_target_type(session):
    with pytest.raises(ValueError):
        await mark_paid(session, "expense", 1, 1000, date(2026, 9, 16), owner_id=1)


async def test_mark_paid_once_creates_on_first_call(session):
    server = await _add_server(session, "gpu-once", 12000, date(2026, 9, 16))
    payment, created = await mark_paid_once(
        session, "server", server.id, 12000, date(2026, 9, 16), owner_id=1
    )
    await session.commit()
    await session.refresh(server)
    assert created is True
    assert payment.due_date == date(2026, 9, 16)
    assert server.next_due == date(2026, 10, 16)


async def test_mark_paid_once_same_day_does_not_roll_twice(session):
    server = await _add_server(session, "gpu-twice", 12000, date(2026, 9, 16))
    first, first_created = await mark_paid_once(
        session, "server", server.id, 12000, date(2026, 9, 16), owner_id=1
    )
    await session.commit()
    await session.refresh(server)
    assert first_created is True
    assert server.next_due == date(2026, 10, 16)

    second, second_created = await mark_paid_once(
        session, "server", server.id, 12000, date(2026, 9, 16), owner_id=1
    )
    await session.commit()
    await session.refresh(server)
    assert second_created is False
    assert second.id == first.id
    assert server.next_due == date(2026, 10, 16)
    payments = (await session.scalars(select(Payment))).all()
    assert len(payments) == 1


async def test_mark_paid_once_next_day_rolls_over(session):
    server = await _add_server(session, "gpu-next-day", 12000, date(2026, 9, 16))
    await mark_paid_once(session, "server", server.id, 12000, date(2026, 9, 16), owner_id=1)
    await session.commit()

    payment, created = await mark_paid_once(
        session, "server", server.id, 12000, date(2026, 9, 17), owner_id=1
    )
    await session.commit()
    await session.refresh(server)
    assert created is True
    assert payment.paid_date == date(2026, 9, 17)
    assert server.next_due == date(2026, 11, 16)


async def test_mark_paid_once_missing_target(session):
    with pytest.raises(ValueError):
        await mark_paid_once(session, "server", 999, 1000, date(2026, 9, 16), owner_id=1)


async def test_mark_paid_once_bad_target_type(session):
    with pytest.raises(ValueError):
        await mark_paid_once(session, "expense", 1, 1000, date(2026, 9, 16), owner_id=1)


async def test_collect_digest_buckets_and_totals(session):
    today = date(2026, 9, 16)
    await _add_server(session, "gpu-due-today", 1000, today)
    await _add_subscription(session, "sub-due-today", 500, today)
    await _add_server(session, "vm-soon", 200, today + timedelta(days=2))
    await _add_server(session, "gpu-overdue", 3000, today - timedelta(days=1))
    await _add_server(session, "gpu-inactive", 4000, today, is_active=False)
    await _add_subscription(session, "sub-paused", 600, today, status="paused")
    await _add_subscription(session, "sub-archived", 700, today, status="archived")
    await _add_server(session, "gpu-far", 800, today + timedelta(days=10))

    digest = await collect_digest(session, today, soon_days=7, owner_id=1)

    assert [e["name"] for e in digest["due_today"]] == ["gpu-due-today", "sub-due-today"]
    assert digest["total_today"] == 1500
    assert [e["name"] for e in digest["due_soon"]] == ["vm-soon"]
    assert digest["total_soon"] == 200
    assert [e["name"] for e in digest["overdue"]] == ["gpu-overdue"]
    assert digest["total_overdue"] == 3000
    entry = digest["due_today"][0]
    assert entry["target_type"] == "server"
    assert isinstance(entry["target_id"], int)
    assert entry["next_due"] == today


async def test_collect_digest_empty(session):
    digest = await collect_digest(session, date(2026, 9, 16), soon_days=7, owner_id=1)
    assert digest == {
        "due_today": [],
        "due_soon": [],
        "overdue": [],
        "total_today": 0,
        "total_soon": 0,
        "total_overdue": 0,
    }


async def test_collect_digest_is_per_owner(session):
    today = date(2026, 9, 16)
    await _add_server(session, "own-a", 1000, today, owner_id=1)
    await _add_server(session, "other-b", 5000, today, owner_id=2)
    await _add_subscription(session, "own-sub", 300, today, owner_id=1)

    digest = await collect_digest(session, today, soon_days=7, owner_id=1)
    assert [e["name"] for e in digest["due_today"]] == ["own-a", "own-sub"]
    assert digest["total_today"] == 1300

    other = await collect_digest(session, today, soon_days=7, owner_id=2)
    assert [e["name"] for e in other["due_today"]] == ["other-b"]
    assert other["total_today"] == 5000


async def test_mark_paid_rejects_foreign_owner(session):
    from app.services.billing import NotOwnerError

    server = await _add_server(session, "gpu-x", 1000, date(2026, 9, 16), owner_id=1)
    with pytest.raises(NotOwnerError):
        await mark_paid(
            session, "server", server.id, 1000, date(2026, 9, 16), owner_id=2
        )


async def test_mark_paid_once_rejects_foreign_owner(session):
    from app.services.billing import NotOwnerError

    server = await _add_server(session, "gpu-y", 1000, date(2026, 9, 16), owner_id=1)
    with pytest.raises(NotOwnerError):
        await mark_paid_once(
            session, "server", server.id, 1000, date(2026, 9, 16), owner_id=2
        )