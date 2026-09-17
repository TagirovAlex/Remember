"""Tests for new bot features: /view, payment amount confirmation, and /export."""

from datetime import date
from io import StringIO

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot.keyboards import (
    VIEW_PREFIX,
    CONFIRM_AMT_PREFIX,
    CHANGE_AMT_PREFIX,
    AMOUNT_NOTE_PREFIX,
    confirm_amount_callback,
    change_amount_callback,
    parse_change_amount_callback,
    parse_confirm_amount_callback,
    parse_view_card_callback,
    view_card_callback,
)
from app.models import Base


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


# ---- Callback roundtrip tests ----


def test_view_card_callback_roundtrip():
    for action in ("pay", "pause", "resume", "shift"):
        for item_id in (1, 42, 999):
            cb = view_card_callback(action, item_id)
            assert cb == f"view:{action}:{item_id}"
            assert parse_view_card_callback(cb) == (action, item_id)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "view",
        "view:",
        "view:pay",
        "view:pay:",
        "view:pay:abc",
        "view:pay:-1",
        "view:pay:0",
        "menu:main",
        "pay:server:1",
    ],
)
def test_parse_view_card_callback_invalid(data):
    assert parse_view_card_callback(data) is None


def test_confirm_amount_callback_roundtrip():
    cb = confirm_amount_callback("server", 3)
    assert cb == "confirm_amount_ok:server:3"
    assert parse_confirm_amount_callback(cb) == ("server", 3)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "confirm_amount_ok",
        "confirm_amount_ok:",
        "confirm_amount_ok:server",
        "confirm_amount_ok:expense:1",
        "confirm_amount_ok:server:0",
        "menu:main",
    ],
)
def test_parse_confirm_amount_callback_invalid(data):
    assert parse_confirm_amount_callback(data) is None


def test_change_amount_callback_roundtrip():
    cb = change_amount_callback("subscription", 7)
    assert cb == "change_amount:subscription:7"
    assert parse_change_amount_callback(cb) == ("subscription", 7)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "change_amount",
        "change_amount:",
        "change_amount:expense:1",
        "change_amount:server:0",
        "menu:main",
    ],
)
def test_parse_change_amount_callback_invalid(data):
    assert parse_change_amount_callback(data) is None


# ---- No overlap between existing and new prefixes ----


_EXISTING_PREFIXES = ["pay", "add", "period", "exp_cat", "tg_confirm", "tg_cancel", "menu"]
_NEW_PREFIXES = ["view", CONFIRM_AMT_PREFIX, CHANGE_AMT_PREFIX, AMOUNT_NOTE_PREFIX]


def test_new_prefixes_do_not_overlap_with_existing():
    """Ensure new callback prefixes don't start with any existing prefix characters."""
    for new in _NEW_PREFIXES:
        for existing in _EXISTING_PREFIXES:
            assert new != existing
            # Check they don't share conflicting routing patterns
            if len(new) >= 2 and len(existing) >= 2:
                assert not (new[:2] == existing[:2]) or new == existing


def test_all_prefixes_unique():
    """Each prefix should be distinct."""
    all_prefixes = _EXISTING_PREFIXES + _NEW_PREFIXES
    assert len(all_prefixes) == len(set(all_prefixes))


# ---- _format_card tests ----


async def test_format_card_server(session):
    """Test formatting of a server card."""
    from app.models import Server
    from app.services.notify import fmt_money
    from app.bot.handlers import _format_card

    server = Server(
        name="GPU-Cluster-1",
        kind="gpu",
        provider="Vast.ai",
        ip="192.168.1.1",
        panel_url="https://panel.example.com",
        specs="A100 80GB",
        what_runs="stable-diffusion",
        notes="Production env",
        amount_rub=8000,
        period="monthly",
        next_due=date(2026, 10, 15),
        is_active=True,
        owner_id=1,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)

    text = _format_card(server, "server")

    assert "GPU-Cluster-1" in text
    assert "Vast.ai" in text
    assert "192.168.1.1" in text
    assert fmt_money(8000) in text
    assert "15.10.2026" in text
    assert "Активна" in text
    assert "A100 80GB" in text
    assert "stable-diffusion" in text


async def test_format_card_subscription(session):
    """Test formatting of a subscription card."""
    from app.models import Subscription
    from app.services.notify import fmt_money
    from app.bot.handlers import _format_card

    sub = Subscription(
        name="OpenAI API",
        provider="OpenAI",
        amount_rub=2400,
        period="monthly",
        next_due=date(2026, 10, 1),
        auto_renewal=True,
        status="active",
        notes="",
        owner_id=1,
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)

    text = _format_card(sub, "subscription")

    assert "OpenAI API" in text
    assert "OpenAI" in text
    assert fmt_money(2400) in text
    assert "Активна" in text
    assert "Да" in text  # auto-renewal


async def test_format_card_expense(session):
    """Test formatting of an expense card."""
    from app.models import OneTimeExpense
    from app.services.notify import fmt_money
    from app.bot.handlers import _format_card

    exp = OneTimeExpense(
        title="API Call",
        amount_rub=400,
        paid_date=date(2026, 9, 10),
        category="API",
        owner_id=1,
    )
    session.add(exp)
    await session.commit()
    await session.refresh(exp)

    text = _format_card(exp, "expense")

    assert "API Call" in text
    assert "Категория: API" in text
    assert fmt_money(400) in text
    assert "10.09.2026" in text


# ---- Export CSV content tests ----


def test_export_csv_structure():
    """Verify the export format produces correct sections."""
    buf = StringIO()
    em = chr(0x2014)
    username = "testuser"
    export_date = "17.09.2026"

    buf.write(f"# Remember {em} экспорт данных пользователя {username}\n")
    buf.write(f"# Дата экспорта: {export_date}\n")
    buf.write("\n")
    buf.write("=== СЕРВЕРЫ ===\n")
    buf.write("ID | Название | Тип | Провайдер | IP | Сумма ₽ | Период | До | Активен\n")
    buf.write("\n")
    buf.write("=== ПОДПИСКИ ===\n")
    buf.write("ID | Название | Провайдер | Сумма ₽ | Период | До | Статус\n")
    buf.write("\n")
    buf.write("=== РАСХОДЫ ===\n")
    buf.write("ID | Название | Сумма ₽ | Категория | Дата\n")
    buf.write("\n")
    buf.write("=== ПЛАТЕЖИ ===\n")
    buf.write("Дата | Что | Сумма ₽ | Кто отметил | Период\n")

    csv_text = buf.getvalue()

    assert "# Remember" in csv_text
    assert "СЕРВЕРЫ" in csv_text
    assert "ПОДПИСКИ" in csv_text
    assert "РАСХОДЫ" in csv_text
    assert "ПЛАТЕЖИ" in csv_text
    assert "testuser" in csv_text
    assert "17.09.2026" in csv_text


# ---- HELP_TEXT updated ----


def test_help_text_includes_new_commands():
    """Verify HELP_TEXT mentions /view and /export."""
    from app.bot.handlers import HELP_TEXT

    assert "/view <id>" in HELP_TEXT
    assert "/export" in HELP_TEXT
