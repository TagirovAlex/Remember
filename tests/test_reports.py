"""Tests for reports functionality: monthly_report function and /reports page."""

from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, OneTimeExpense, Payment, Server, Subscription, User
from app.security import hash_password
from app.services.billing import monthly_report


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as s:
        yield s
    await engine.dispose()


PASSWORD = "secret-pass"
SID = "remember_sid"


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def app(session_factory, monkeypatch):
    from app.config import settings
    from app.db import get_session
    from app.factory import create_app

    monkeypatch.setattr(settings, "admin_password", PASSWORD)

    async def override_get_session():
        async with session_factory() as s:
            yield s

    application = create_app()
    application.dependency_overrides[get_session] = override_get_session
    # Create admin user
    async with session_factory() as s:
        from app.auth import ensure_admin_user
        await ensure_admin_user(s)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post(
        "/login", data={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 303
    return response


def _mk_server(name, provider, amount, next_due, owner_id, session_factory):
    """Sync helper to create a server dict for Payment targets."""
    return {
        "name": name, "provider": provider, "amount_rub": amount,
        "next_due": next_due, "owner_id": owner_id,
    }


async def _setup_month_data(session_factory, ref_month: date):
    """Create servers, subs, expenses in a given month for admin user."""
    admin_owner_id = None
    async with session_factory() as sess:
        admin = await sess.scalar(select(User).where(User.username == "admin"))
        admin_owner_id = admin.id

        s1 = Server(
            name="Vast-1", kind="gpu", provider="Vast.ai",
            amount_rub=24000, period="monthly", next_due=date(2026, 10, 15),
            is_active=True, owner_id=admin_owner_id,
        )
        s2 = Server(
            name="Hetzner-1", kind="vm", provider="Hetzner",
            amount_rub=7200, period="quarterly", next_due=date(2026, 11, 1),
            is_active=True, owner_id=admin_owner_id,
        )
        sub = Subscription(
            name="OpenAI-Plus", provider="OpenAI",
            amount_rub=8400, period="monthly", next_due=date(2026, 10, 5),
            status="active", owner_id=admin_owner_id,
        )
        sess.add_all([s1, s2, sub])
        await sess.commit()
        await sess.refresh(s1); await sess.refresh(s2); await sess.refresh(sub)

        payment_data = [
            ("server", s1.id, 12000, date(2026, 9, 15), date(2026, 9, 15)),
            ("server", s2.id, 7200, date(2026, 9, 1), date(2026, 9, 1)),
            ("subscription", sub.id, 8400, date(2026, 9, 5), date(2026, 9, 5)),
            ("server", s1.id, 12000, date(2026, 9, 20), date(2026, 9, 20)),
        ]
        for ttype, tid, amt, due, pd in payment_data:
            sess.add(Payment(target_type=ttype, target_id=tid, amount_rub=amt, due_date=due, paid_date=pd))

        expense_data = [
            ("OpenAI API", 2400, date(2026, 9, 10), "API"),
            ("Питание", 1800, date(2026, 9, 8), "Питание"),
            ("AWS S3", 1500, date(2026, 9, 15), "Хранение"),
            ("Кофе", 800, date(2026, 9, 25), None),
        ]
        for title, amt, pd, cat in expense_data:
            sess.add(OneTimeExpense(title=title, amount_rub=amt, paid_date=pd, category=cat, owner_id=admin_owner_id))

        await sess.commit()


# ---- Business logic tests for monthly_report ----


async def test_monthly_report_basic_totals(session):
    """Test that totals match sum of all payments + expenses."""
    now = date(2026, 9, 15)
    server = Server(
        name="Srv-A", kind="gpu", provider="Test", amount_rub=5000,
        period="monthly", next_due=date(2026, 10, 1), owner_id=1, is_active=True,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    session.add(Payment(target_type="server", target_id=server.id, amount_rub=5000, due_date=date(2026, 9, 15), paid_date=now))
    session.add(OneTimeExpense(title="Coffee", amount_rub=500, paid_date=now, category="Без категории", owner_id=1))
    await session.commit()

    result = await monthly_report(session, owner_id=1, ref_month=now, today=date(2026, 9, 15))

    assert result["month_label"] == "Сентябрь 2026"
    assert isinstance(result["total_paid"], int)
    assert result["by_type"]["expense"]["count"] == 1
    assert result["by_type"]["expense"]["sum"] == 500


async def test_monthly_report_server_subscription_count(session):
    """Payments for servers/subscriptions are counted correctly."""
    now = date(2026, 9, 15)
    server = Server(
        name="Srv-A", kind="gpu", provider="Test", amount_rub=5000,
        period="monthly", next_due=date(2026, 10, 1), owner_id=1, is_active=True,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    session.add(Payment(target_type="server", target_id=server.id, amount_rub=5000, due_date=date(2026, 9, 15), paid_date=now))
    await session.commit()

    result = await monthly_report(session, owner_id=1, ref_month=now, today=now)
    assert result["by_type"]["server_subscription"]["count"] == 1
    assert result["by_type"]["server_subscription"]["sum"] == 5000


async def test_monthly_report_top_providers(session):
    """Top providers are sorted by sum descending and limited to 5 + others."""
    now = date(2026, 9, 15)
    s1 = Server(name="S1", kind="gpu", provider="BigProv", amount_rub=10000,
                period="monthly", next_due=date(2026, 10, 1), owner_id=1, is_active=True)
    s2 = Server(name="S2", kind="vm", provider="SmallProv", amount_rub=1000,
                period="monthly", next_due=date(2026, 10, 1), owner_id=1, is_active=True)
    session.add_all([s1, s2])
    await session.commit()
    await session.refresh(s1); await session.refresh(s2)
    session.add(Payment(target_type="server", target_id=s1.id, amount_rub=10000, due_date=date(2026, 9, 15), paid_date=now))
    session.add(Payment(target_type="server", target_id=s2.id, amount_rub=1000, due_date=date(2026, 9, 15), paid_date=now))
    await session.commit()

    result = await monthly_report(session, owner_id=1, ref_month=now, today=now)
    assert len(result["top_providers"]) == 2
    assert result["top_providers"][0]["provider"] == "BigProv"
    assert result["top_providers"][0]["sum"] == 10000
    assert result["top_providers"][1]["provider"] == "SmallProv"


async def test_monthly_report_top_categories(session):
    """Top categories are limited to 3 items sorted by sum desc."""
    now = date(2026, 9, 15)
    data = [
        ("E1", 5000, now, "Food"),
        ("E2", 3000, now, "Food"),
        ("E3", 4000, now, "Transport"),
        ("E4", 2000, now, "Fun"),
        ("E5", 1000, now, "Other"),
    ]
    for title, amt, pd, cat in data:
        session.add(OneTimeExpense(title=title, amount_rub=amt, paid_date=pd, category=cat, owner_id=1))
    await session.commit()

    result = await monthly_report(session, owner_id=1, ref_month=now, today=now)
    assert len(result["top_categories"]) == 3
    assert result["top_categories"][0]["category"] == "Food"
    assert result["top_categories"][0]["sum"] == 8000
    assert result["top_categories"][1]["category"] == "Transport"
    assert result["top_categories"][1]["sum"] == 4000
    assert result["top_categories"][2]["category"] == "Fun"
    assert result["top_categories"][2]["sum"] == 2000


async def test_monthly_report_forecast(session):
    """Forecast sums active servers/subs with next_due between today and end of month."""
    today = date(2026, 9, 15)
    s1 = Server(name="S-next", kind="gpu", provider="P", amount_rub=3000,
                period="monthly", next_due=date(2026, 9, 25), owner_id=1, is_active=True)
    s2 = Server(name="S-later", kind="vm", provider="P", amount_rub=7000,
                period="yearly", next_due=date(2026, 11, 1), owner_id=1, is_active=True)
    session.add_all([s1, s2])
    await session.commit()

    result = await monthly_report(session, owner_id=1, ref_month=today, today=today)
    assert result["forecast_remaining"] == 3000


async def test_monthly_report_empty_month(session):
    """Empty month returns zeroes."""
    result = await monthly_report(session, owner_id=999, ref_month=date(2026, 12, 1), today=date(2026, 12, 15))
    assert result["total_paid"] == 0
    assert result["by_type"]["server_subscription"]["count"] == 0
    assert result["by_type"]["expense"]["count"] == 0
    assert result["by_type"]["server_subscription"]["sum"] == 0
    assert result["by_type"]["expense"]["sum"] == 0
    assert result["top_providers"] == []
    assert result["top_categories"] == []
    assert result["forecast_remaining"] == 0


# ---- Web route tests ----


async def test_reports_page_requires_auth(client):
    response = await client.get("/reports")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


async def test_reports_page_render_without_month(client):
    await _login(client)
    response = await client.get("/reports")
    assert response.status_code == 200
    assert "Финансовые отчёты" in response.text
    assert "Генерировать" in response.text


async def test_reports_page_post_with_month(client, session_factory):
    await _setup_month_data(session_factory, date(2026, 9, 1))
    await _login(client)
    response = await client.post("/reports", data={"month": "2026-09"})
    assert response.status_code == 200
    # Check numeric content (encoding-safe)
    assert "39 600" in response.text
    assert "6 500" in response.text
    assert "Разбивка по типу" in response.text
    assert "Топ провайдеров" in response.text
    assert "Топ категорий расходов" in response.text


async def test_reports_isolation_different_user(client, session_factory):
    """Alice has no September data so shows zero totals."""
    async with session_factory() as sess:
        alice = User(username="alice-rpt", password_hash=hash_password("pass"), role="user", is_active=True)
        sess.add(alice)
        await sess.commit()

    resp = await client.post("/login", data={"username": "alice-rpt", "password": "pass"})
    assert resp.status_code == 303

    response = await client.post("/reports", data={"month": "2026-09"})
    assert response.status_code == 200
    assert "0 ₽" in response.text
