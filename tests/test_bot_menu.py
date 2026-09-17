"""Tests for the main inline bot menu: callbacks, keyboards and list rendering.

Handlers are covered through their pure helpers (parsers, keyboards,
formatters); DB-backed flows reuse the services tested elsewhere.
"""

from datetime import date

import pytest

from app.bot.keyboards import (
    MENU_ACTIONS,
    back_to_menu_keyboard,
    main_menu_keyboard,
    menu_add_kind_callback,
    menu_callback,
    menu_digest_keyboard,
    menu_entries_keyboard,
    menu_kind_keyboard,
    menu_pay_kind_callback,
    parse_menu_add_kind_callback,
    parse_menu_callback,
    parse_menu_pay_kind_callback,
)
from app.services.notify import render_entries


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def test_menu_callback_roundtrip():
    for action in MENU_ACTIONS:
        assert parse_menu_callback(menu_callback(action)) == action


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "menu",
        "menu:",
        "menu:foo",
        "menu:main:x",
        "menu:subs:extra",
        "menu:pay_kind:server",
        "unknown:main",
    ],
)
def test_parse_menu_callback_invalid(data):
    assert parse_menu_callback(data) is None


def test_menu_callback_invalid_action():
    with pytest.raises(ValueError):
        menu_callback("foo")


def test_menu_pay_kind_callback_roundtrip():
    for target_type in ("server", "subscription"):
        assert (
            parse_menu_pay_kind_callback(menu_pay_kind_callback(target_type))
            == target_type
        )


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "menu",
        "menu:",
        "menu:pay_kind",
        "menu:pay_kind:",
        "menu:pay_kind:expense",
        "menu:pay_kind:server:x",
        "menu:pay_kind:server:5",
        "menu:main",
    ],
)
def test_parse_menu_pay_kind_callback_invalid(data):
    assert parse_menu_pay_kind_callback(data) is None


def test_menu_pay_kind_callback_invalid_target_type():
    with pytest.raises(ValueError):
        menu_pay_kind_callback("expense")


def test_menu_add_kind_callback_roundtrip():
    for target_type in ("server", "subscription"):
        assert (
            parse_menu_add_kind_callback(menu_add_kind_callback(target_type))
            == target_type
        )


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "menu:add_kind",
        "menu:add_kind:",
        "menu:add_kind:expense",
        "menu:add_kind:server:x",
        "menu:add_kind:server:5",
        "menu:main",
    ],
)
def test_parse_menu_add_kind_callback_invalid(data):
    assert parse_menu_add_kind_callback(data) is None


def test_menu_add_kind_callback_invalid_target_type():
    with pytest.raises(ValueError):
        menu_add_kind_callback("expense")


def test_menu_callback_data_is_compact():
    for data in (
        menu_callback("dash"),
        menu_pay_kind_callback("subscription"),
        menu_add_kind_callback("subscription"),
    ):
        assert len(data.encode()) <= 64


def test_main_menu_keyboard_buttons():
    buttons = _buttons(main_menu_keyboard())
    assert [b.callback_data for b in buttons] == [
        "menu:subs",
        "menu:servers",
        "menu:pay",
        "menu:add",
        "menu:dash",
    ]
    assert [b.text for b in buttons] == [
        "📋 Подписки",
        "🖥 Серверы",
        "💳 Внести оплату",
        "➕ Добавить",
        "📊 Дашборд",
    ]


def test_back_to_menu_keyboard_buttons():
    buttons = _buttons(back_to_menu_keyboard())
    assert [b.callback_data for b in buttons] == ["menu:main"]
    assert buttons[0].text == "🏠 Меню"


def test_menu_kind_keyboard_pay():
    keyboard = menu_kind_keyboard("pay")
    assert [len(row) for row in keyboard.inline_keyboard] == [2, 1]
    buttons = _buttons(keyboard)
    assert [b.callback_data for b in buttons] == [
        "menu:pay_kind:subscription",
        "menu:pay_kind:server",
        "menu:main",
    ]
    assert [b.text for b in buttons] == ["Подписка", "Сервер", "🏠 Меню"]


def test_menu_kind_keyboard_add():
    buttons = _buttons(menu_kind_keyboard("add"))
    assert [b.callback_data for b in buttons] == [
        "menu:add_kind:subscription",
        "menu:add_kind:server",
        "menu:main",
    ]


def test_menu_kind_keyboard_invalid_action():
    with pytest.raises(ValueError):
        menu_kind_keyboard("foo")


def test_menu_entries_keyboard_pay_and_back():
    entries = [
        {"target_type": "server", "target_id": 1},
        {"target_type": "subscription", "target_id": 2},
    ]
    buttons = _buttons(menu_entries_keyboard(entries))
    assert [b.callback_data for b in buttons] == [
        "pay:server:1",
        "pay:subscription:2",
        "menu:main",
    ]
    assert [b.text for b in buttons] == ["Оплачено ✓", "Оплачено ✓", "🏠 Меню"]


def test_menu_entries_keyboard_empty_only_back():
    buttons = _buttons(menu_entries_keyboard([]))
    assert [b.callback_data for b in buttons] == ["menu:main"]


def test_menu_digest_keyboard_all_sections_plus_back():
    digest = {
        "due_today": [{"target_type": "server", "target_id": 1}],
        "due_soon": [{"target_type": "subscription", "target_id": 2}],
        "overdue": [{"target_type": "server", "target_id": 3}],
    }
    buttons = _buttons(menu_digest_keyboard(digest))
    assert [b.callback_data for b in buttons] == [
        "pay:server:1",
        "pay:subscription:2",
        "pay:server:3",
        "menu:main",
    ]


def test_render_entries_with_items():
    entries = [
        {"name": "Netflix", "amount_rub": 1500, "next_due": date(2026, 10, 1)},
        {"name": "GPU node", "amount_rub": 12000, "next_due": date(2026, 9, 20)},
    ]
    text = render_entries("📡 Подписки", entries, "Подписок нет")
    assert text.startswith("📡 Подписки")
    assert "• Netflix — 1 500 ₽ — до 01.10.2026" in text
    assert "• GPU node — 12 000 ₽ — до 20.09.2026" in text


def test_render_entries_empty():
    text = render_entries("🖥 Серверы", [], "Серверов нет")
    assert "🖥 Серверы" in text
    assert "Серверов нет" in text


def test_help_text_mentions_menu():
    from app.bot.handlers import HELP_TEXT

    assert "/menu" in HELP_TEXT