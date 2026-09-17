"""Tests for the bot v1 feature pack: /stats, /addexpense, /history, /find.

Handlers are covered through their pure helpers and formatters; DB-backed
service functions are tested against an in-memory SQLite database.
"""

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot.handlers import ExpenseStates
from app.bot.keyboards import (
    EXPENSE_CATEGORIES,
    expense_category_callback,
    expense_category_keyboard,
    parse_expense_category_callback,
)
from app.models import Base, OneTimeExpense, Payment, Server, Subscription
from app.services.billing import (
    monthly_summary,
    owned_payments,
    search_owned,
)
from app.services.notify import render_history, render_monthly_summary, render_search


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


async def _add_server(
    session: AsyncSession, name: str, amount: int, next_due: date, **kw
) -> Server:
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


async def _add_expense(
    session: AsyncSession, title: str, amount: int, paid_date: date, **kw
) -> OneTimeExpense:
    expense = OneTimeExpense(
        owner_id=kw.pop("owner_id", 1),
        title=title,
        amount_rub=amount,
        paid_date=paid_date,
        category=kw.pop("category", None),
        **kw,
    )
    session.add(expense)
    await session.commit()
    await session.refresh(expense)
    return expense


async def _add_payment(
    session: AsyncSession,
    target_type: str,
    target_id: int,
    amount: int,
    paid_date: date,
    **kw,
) -> Payment:
    payment = Payment(
        target_type=target_type,
        target_id=target_id,
        amount_rub=amount,
        due_date=kw.pop("due_date", paid_date),
        paid_date=paid_date,
        paid_by=kw.pop("paid_by", None),
        notes=kw.pop("notes", None),
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


class TestMonthlySummary:
    async def test_empty_month(self, session):
        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))
        assert summary == {
            "month": date(2026, 9, 1),
            "total_paid": 0,
            "recurring_count": 0,
            "recurring_sum": 0,
            "one_time_count": 0,
            "one_time_sum": 0,
            "top_categories": [],
            "prev_month_total": 0,
            "due_remaining_count": 0,
            "due_remaining_sum": 0,
        }

    async def test_totals_and_breakdown(self, session):
        server = await _add_server(session, "gpu-a", 12000, date(2026, 9, 20))
        await _add_payment(session, "server", server.id, 12000, date(2026, 9, 5))
        await _add_expense(session, "кофе", 300, date(2026, 9, 8), category="Питание")

        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))

        assert summary["total_paid"] == 12300
        assert summary["recurring_count"] == 1
        assert summary["recurring_sum"] == 12000
        assert summary["one_time_count"] == 1
        assert summary["one_time_sum"] == 300

    async def test_prev_month_total(self, session):
        server = await _add_server(session, "gpu-a", 12000, date(2026, 9, 20))
        await _add_payment(session, "server", server.id, 12000, date(2026, 8, 5))
        await _add_expense(session, "старое", 500, date(2026, 8, 2), category="Другое")

        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))

        assert summary["total_paid"] == 0
        assert summary["prev_month_total"] == 12500

    async def test_top_categories_sorted_and_capped(self, session):
        await _add_expense(session, "еда", 100, date(2026, 9, 1), category="Питание")
        await _add_expense(session, "обед", 200, date(2026, 9, 2), category="Питание")
        await _add_expense(session, "такси", 300, date(2026, 9, 3), category="Транспорт")
        await _add_expense(session, "api", 400, date(2026, 9, 4), category="API")
        await _add_expense(session, "прочее", 500, date(2026, 9, 5))

        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))

        assert summary["top_categories"] == [
            {"category": "Без категории", "sum": 500},
            {"category": "API", "sum": 400},
            {"category": "Питание", "sum": 300},
        ]

    async def test_forecast_counts_remaining_dues(self, session):
        await _add_server(session, "soon-srv", 1000, date(2026, 9, 20))
        await _add_server(session, "past-srv", 2000, date(2026, 9, 10))
        await _add_server(session, "inactive-srv", 3000, date(2026, 9, 21), is_active=False)
        await _add_server(session, "next-month-srv", 4000, date(2026, 10, 5))
        await _add_subscription(session, "soon-sub", 500, date(2026, 9, 25))
        await _add_subscription(session, "paused-sub", 600, date(2026, 9, 26), status="paused")

        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))

        assert summary["due_remaining_count"] == 2
        assert summary["due_remaining_sum"] == 1500

    async def test_per_owner_isolation(self, session):
        mine = await _add_server(session, "own", 1000, date(2026, 9, 5), owner_id=1)
        await _add_payment(session, "server", mine.id, 1000, date(2026, 9, 5))
        other = await _add_server(session, "other", 5000, date(2026, 9, 5), owner_id=2)
        await _add_payment(session, "server", other.id, 5000, date(2026, 9, 6))
        await _add_expense(session, "чужой расход", 900, date(2026, 9, 7), owner_id=2)

        summary = await monthly_summary(session, 1, date(2026, 9, 1), today=date(2026, 9, 16))

        assert summary["total_paid"] == 1000
        assert summary["recurring_sum"] == 1000
        assert summary["one_time_sum"] == 0

    async def test_forecast_empty_when_today_after_month_end(self, session):
        await _add_server(session, "late", 1000, date(2026, 9, 20))

        summary = await monthly_summary(session, 1, date(2026, 10, 1), today=date(2026, 11, 1))

        assert summary["due_remaining_count"] == 0
        assert summary["due_remaining_sum"] == 0


class TestOwnedPayments:
    async def test_filters_and_orders(self, session):
        mine = await _add_server(session, "mine", 1000, date(2026, 9, 5), owner_id=1)
        other = await _add_server(session, "other", 5000, date(2026, 9, 5), owner_id=2)
        await _add_payment(session, "server", mine.id, 1000, date(2026, 9, 1))
        await _add_payment(session, "server", mine.id, 1000, date(2026, 8, 1))
        await _add_payment(session, "server", other.id, 5000, date(2026, 9, 2))

        payments = await owned_payments(session, 1)

        assert [p.paid_date for p in payments] == [date(2026, 9, 1), date(2026, 8, 1)]

    async def test_limit(self, session):
        mine = await _add_server(session, "mine", 1000, date(2026, 9, 5), owner_id=1)
        for day in range(1, 6):
            await _add_payment(session, "server", mine.id, 1000, date(2026, 9, day))

        assert len(await owned_payments(session, 1, limit=3)) == 3
        assert len(await owned_payments(session, 1)) == 5


class TestSearchOwned:
    async def test_matches_names_providers_titles_categories(self, session):
        await _add_server(session, "gpu-node-a", 1000, date(2026, 9, 5), provider="VastAI")
        await _add_subscription(session, "netflix", 900, date(2026, 9, 20), provider="OpenAI API")
        await _add_expense(session, "Питание в офисе", 300, date(2026, 9, 8), category="Питание")

        by_name = await search_owned(session, 1, "gpu")
        assert [r["name"] for r in by_name] == ["gpu-node-a"]
        assert by_name[0]["target_type"] == "server"
        assert by_name[0]["due"] == date(2026, 9, 5)

        by_provider = await search_owned(session, 1, "openai")
        assert [r["name"] for r in by_provider] == ["netflix"]
        assert by_provider[0]["target_type"] == "subscription"

        by_title = await search_owned(session, 1, "офис")
        assert [r["name"] for r in by_title] == ["Питание в офисе"]
        assert by_title[0]["target_type"] == "expense"

        by_category = await search_owned(session, 1, "Питание")
        assert [r["name"] for r in by_category] == ["Питание в офисе"]

    async def test_empty_result(self, session):
        await _add_server(session, "gpu-node-a", 1000, date(2026, 9, 5))
        assert await search_owned(session, 1, "zzz-not-found") == []

    async def test_per_owner_isolation(self, session):
        await _add_server(session, "gpu-mine", 1000, date(2026, 9, 5), owner_id=1)
        await _add_server(session, "gpu-other", 2000, date(2026, 9, 5), owner_id=2)

        results = await search_owned(session, 1, "gpu")

        assert [r["name"] for r in results] == ["gpu-mine"]

    async def test_limit(self, session):
        for i in range(4):
            await _add_server(session, f"gpu-{i}", 1000, date(2026, 9, 5))
        results = await search_owned(session, 1, "gpu", limit=2)
        assert len(results) == 2


class TestRender:
    def test_render_monthly_summary_full(self):
        text = render_monthly_summary(
            {
                "month": date(2026, 9, 1),
                "total_paid": 12300,
                "recurring_count": 1,
                "recurring_sum": 12000,
                "one_time_count": 2,
                "one_time_sum": 300,
                "top_categories": [
                    {"category": "Питание", "sum": 300},
                    {"category": "API", "sum": 0},
                ],
                "prev_month_total": 5000,
                "due_remaining_count": 2,
                "due_remaining_sum": 1500,
            }
        )
        assert "📊 Сентябрь 2026" in text
        assert "💸 Оплачено за месяц: 12 300 ₽" in text
        assert "• серверы и подписки: 1 · 12 000 ₽" in text
        assert "• разовые: 2 · 300 ₽" in text
        assert "🏷 Топ категорий:" in text
        assert "• Питание — 300 ₽" in text
        assert "📉 Прошлый месяц: 5 000 ₽" in text
        assert "⏳ До конца месяца предстоит: 2 · 1 500 ₽" in text

    def test_render_monthly_summary_empty(self):
        text = render_monthly_summary(
            {
                "month": date(2026, 1, 1),
                "total_paid": 0,
                "recurring_count": 0,
                "recurring_sum": 0,
                "one_time_count": 0,
                "one_time_sum": 0,
                "top_categories": [],
                "prev_month_total": 0,
                "due_remaining_count": 0,
                "due_remaining_sum": 0,
            }
        )
        assert "📊 Январь 2026" in text
        assert "🏷 Топ категорий:" not in text
        assert "⏳ До конца месяца предстоит:" not in text

    def test_render_history_with_rows(self):
        rows = [
            {
                "paid_date": date(2026, 9, 15),
                "name": "gpu-a",
                "amount_rub": 12000,
                "paid_by": "admin",
                "notes": "оплата",
            },
            {
                "paid_date": date(2026, 9, 14),
                "name": "netflix",
                "amount_rub": 900,
                "paid_by": None,
                "notes": None,
            },
        ]
        text = render_history(rows, 10)
        assert "📜 История (последние 10)" in text
        assert text.splitlines()[1] == "1 · 15.09.2026 · gpu-a · 12 000 ₽ · admin · оплата"
        assert text.splitlines()[2] == "2 · 14.09.2026 · netflix · 900 ₽"

    def test_render_history_empty(self):
        assert render_history([], 10) == "📜 История (последние 10)\n—"

    def test_render_search_with_results(self):
        results = [
            {"target_type": "server", "name": "gpu-node-a", "amount_rub": 12000, "due": date(2026, 9, 20)},
            {"target_type": "expense", "name": "Питание в офисе", "amount_rub": 300, "due": date(2026, 9, 8)},
        ]
        text = render_search("gpu", results)
        assert "🔍 Поиск: gpu" in text
        assert "1. Сервер · gpu-node-a · 12 000 ₽ · до 20.09.2026" in text
        assert "2. Расход · Питание в офисе · 300 ₽ · 08.09.2026" in text

    def test_render_search_empty(self):
        assert render_search("zzz", []) == "🔍 Поиск: zzz\nНичего не найдено"


class TestExpenseCategoriesKeyboard:
    def test_category_callback_roundtrip(self):
        for category in EXPENSE_CATEGORIES:
            assert parse_expense_category_callback(expense_category_callback(category)) == category

    @pytest.mark.parametrize(
        "data",
        [None, "", "exp_cat", "exp_cat:", "exp_cat:Нет", "exp_cat:API:x"],
    )
    def test_parse_category_callback_invalid(self, data):
        assert parse_expense_category_callback(data) is None

    def test_category_callback_invalid_category(self):
        with pytest.raises(ValueError):
            expense_category_callback("Нет")

    def test_category_keyboard_buttons(self):
        buttons = [
            b for row in expense_category_keyboard().inline_keyboard for b in row
        ]
        assert [b.callback_data for b in buttons] == [
            expense_category_callback(c) for c in EXPENSE_CATEGORIES
        ]
        assert [b.text for b in buttons] == list(EXPENSE_CATEGORIES)


def test_expense_states_described():
    assert {state.state for state in ExpenseStates.__states__} == {
        "ExpenseStates:name",
        "ExpenseStates:amount",
        "ExpenseStates:category",
        "ExpenseStates:paid_date",
    }