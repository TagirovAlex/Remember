"""Inline keyboards and callback_data builders/parsers for the Telegram bot.

Callback formats (compact, <=64 bytes):
    pay:{target_type}:{target_id}   — mark an entry as paid
    add:{target_type}               — choose what to add in /add FSM
    period:{period}                 — choose billing period in /add FSM
    exp_cat:{category}              — choose an expense category in /addexpense FSM
    tg_confirm:{user_id}            — confirm a Telegram binding
    tg_cancel:{user_id}             — reject a Telegram binding
    menu:{action}                   — main menu navigation (main/subs/servers/pay/add/dash)
    menu:pay_kind:{target_type}     — choose what to pay in the menu
    menu:add_kind:{target_type}     -- choose what to add in /add FSM
    view:{action}:{id}                 -- view card actions
    confirm_amount_ok:{type}:{id}      -- confirm payment original amount
    change_amount:{type}:{id}          -- change payment amount FSM trigger
    amount_note:{type}:{id}            -- payment note FSM step
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAY_PREFIX = "pay"
ADD_PREFIX = "add"
PERIOD_PREFIX = "period"
EXP_CAT_PREFIX = "exp_cat"
TG_CONFIRM_PREFIX = "tg_confirm"
TG_CANCEL_PREFIX = "tg_cancel"
MENU_PREFIX = "menu"

MENU_ACTIONS = ("main", "subs", "servers", "pay", "add", "dash")

MENU_BACK_TEXT = "🏠 Меню"

VALID_TARGET_TYPES = ("server", "subscription")
VALID_PERIODS = ("monthly", "quarterly", "yearly")
EXPENSE_CATEGORIES = ("API", "Питание", "Транспорт", "Инфраструктура", "Другое")

PERIOD_LABELS: dict[str, str] = {
    "monthly": "Ежемесячно",
    "quarterly": "Раз в квартал",
    "yearly": "Раз в год",
}

PAY_BUTTON_TEXT = "Оплачено ✓"


def confirm_binding_callback(user_id: int) -> str:
    """Build callback_data for the confirm-binding button."""
    return f"{TG_CONFIRM_PREFIX}:{user_id}"


def cancel_binding_callback(user_id: int) -> str:
    """Build callback_data for the cancel-binding button."""
    return f"{TG_CANCEL_PREFIX}:{user_id}"


def _parse_binding_callback(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(f"{prefix}:"):
        return None
    raw = data.split(":", 1)[1]
    try:
        user_id = int(raw)
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def parse_confirm_binding_callback(data: str | None) -> int | None:
    """Parse tg_confirm:{user_id}; return None for invalid payloads."""
    return _parse_binding_callback(data, TG_CONFIRM_PREFIX)


def parse_cancel_binding_callback(data: str | None) -> int | None:
    """Parse tg_cancel:{user_id}; return None for invalid payloads."""
    return _parse_binding_callback(data, TG_CANCEL_PREFIX)


def binding_notice_text(username: str) -> str:
    """Text of the two-step binding confirmation notice."""
    return (
        f"К вам привязан аккаунт {username} в Remember. Если это вы — нажмите "
        "«Подтвердить». Если нет — нажмите «Отменить»."
    )


def pay_callback(target_type: str, target_id: int) -> str:
    """Build callback_data for the pay button, e.g. pay:server:5."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"unknown target_type: {target_type}")
    return f"{PAY_PREFIX}:{target_type}:{target_id}"


def parse_pay_callback(data: str | None) -> tuple[str, int] | None:
    """Parse pay:{target_type}:{target_id}; return None for invalid payloads."""
    if not data or not data.startswith(f"{PAY_PREFIX}:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    target_type, raw_id = parts[1], parts[2]
    if target_type not in VALID_TARGET_TYPES:
        return None
    try:
        target_id = int(raw_id)
    except ValueError:
        return None
    if target_id <= 0:
        return None
    return target_type, target_id


def add_kind_callback(target_type: str) -> str:
    """Build callback_data for choosing an item kind in /add, e.g. add:server."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"unknown target_type: {target_type}")
    return f"{ADD_PREFIX}:{target_type}"


def parse_add_kind_callback(data: str | None) -> str | None:
    """Parse add:{target_type}; return None for invalid payloads."""
    if not data or not data.startswith(f"{ADD_PREFIX}:"):
        return None
    target_type = data.split(":", 1)[1]
    return target_type if target_type in VALID_TARGET_TYPES else None


def period_callback(period: str) -> str:
    """Build callback_data for choosing a period, e.g. period:monthly."""
    if period not in VALID_PERIODS:
        raise ValueError(f"unknown period: {period}")
    return f"{PERIOD_PREFIX}:{period}"


def parse_period_callback(data: str | None) -> str | None:
    """Parse period:{period}; return None for invalid payloads."""
    if not data or not data.startswith(f"{PERIOD_PREFIX}:"):
        return None
    period = data.split(":", 1)[1]
    return period if period in VALID_PERIODS else None


def add_kind_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for the first /add step: server or subscription."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Сервер", callback_data=add_kind_callback("server"))
    builder.button(text="Подписка", callback_data=add_kind_callback("subscription"))
    builder.adjust(1)
    return builder.as_markup()


def period_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for choosing a billing period in /add."""
    builder = InlineKeyboardBuilder()
    for period in VALID_PERIODS:
        builder.button(text=PERIOD_LABELS[period], callback_data=period_callback(period))
    builder.adjust(1)
    return builder.as_markup()


def expense_category_callback(category: str) -> str:
    """Build callback_data for choosing an expense category, e.g. exp_cat:API."""
    if category not in EXPENSE_CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    return f"{EXP_CAT_PREFIX}:{category}"


def parse_expense_category_callback(data: str | None) -> str | None:
    """Parse exp_cat:{category}; return None for invalid payloads."""
    if not data or not data.startswith(f"{EXP_CAT_PREFIX}:"):
        return None
    category = data.split(":", 1)[1]
    return category if category in EXPENSE_CATEGORIES else None


def expense_category_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for choosing an expense category in /addexpense."""
    builder = InlineKeyboardBuilder()
    for category in EXPENSE_CATEGORIES:
        builder.button(
            text=category, callback_data=expense_category_callback(category)
        )
    builder.adjust(1)
    return builder.as_markup()


def pay_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    """One «Оплачено ✓» button per entry (digest/list item)."""
    builder = InlineKeyboardBuilder()
    for i, entry in enumerate(entries, 1):
        builder.button(
            text=f"📌 {i}. {PAY_BUTTON_TEXT}",
            callback_data=pay_callback(entry["target_type"], entry["target_id"]),
        )
    builder.adjust(1)
    return builder.as_markup()


def digest_pay_keyboard(digest: dict) -> InlineKeyboardMarkup:
    """Collect all entries from a collect_digest result into a pay keyboard."""
    entries = [*digest["due_today"], *digest["due_soon"], *digest["overdue"]]
    return pay_keyboard(entries)


def confirm_binding_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Keyboard for the binding notice: confirm or cancel."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Подтвердить", callback_data=confirm_binding_callback(user_id)
    )
    builder.button(text="Отменить", callback_data=cancel_binding_callback(user_id))
    builder.adjust(2)
    return builder.as_markup()


def menu_callback(action: str) -> str:
    """Build callback_data for a main-menu action, e.g. menu:subs."""
    if action not in MENU_ACTIONS:
        raise ValueError(f"unknown menu action: {action}")
    return f"{MENU_PREFIX}:{action}"


def parse_menu_callback(data: str | None) -> str | None:
    """Parse menu:{action}; return None for invalid payloads."""
    if not data or not data.startswith(f"{MENU_PREFIX}:"):
        return None
    action = data.split(":", 1)[1]
    return action if action in MENU_ACTIONS else None


def menu_pay_kind_callback(target_type: str) -> str:
    """Build callback_data for choosing a pay kind, e.g. menu:pay_kind:server."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"unknown target_type: {target_type}")
    return f"{MENU_PREFIX}:pay_kind:{target_type}"


def parse_menu_pay_kind_callback(data: str | None) -> str | None:
    """Parse menu:pay_kind:{target_type}; return None for invalid payloads."""
    if not data or not data.startswith(f"{MENU_PREFIX}:pay_kind:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    target_type = parts[2]
    return target_type if target_type in VALID_TARGET_TYPES else None


def menu_add_kind_callback(target_type: str) -> str:
    """Build callback_data for choosing an add kind, e.g. menu:add_kind:server."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"unknown target_type: {target_type}")
    return f"{MENU_PREFIX}:add_kind:{target_type}"


def parse_menu_add_kind_callback(data: str | None) -> str | None:
    """Parse menu:add_kind:{target_type}; return None for invalid payloads."""
    if not data or not data.startswith(f"{MENU_PREFIX}:add_kind:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    target_type = parts[2]
    return target_type if target_type in VALID_TARGET_TYPES else None


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu: lists, payments, add and the dashboard."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Подписки", callback_data=menu_callback("subs"))
    builder.button(text="🖥 Серверы", callback_data=menu_callback("servers"))
    builder.button(text="💳 Внести оплату", callback_data=menu_callback("pay"))
    builder.button(text="➕ Добавить", callback_data=menu_callback("add"))
    builder.button(text="📊 Дашборд", callback_data=menu_callback("dash"))
    builder.adjust(1)
    return builder.as_markup()


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """A single «🏠 Меню» button returning to the main menu."""
    builder = InlineKeyboardBuilder()
    builder.button(text=MENU_BACK_TEXT, callback_data=menu_callback("main"))
    builder.adjust(1)
    return builder.as_markup()


def menu_kind_keyboard(action: str) -> InlineKeyboardMarkup:
    """Choose server/subscription for the pay or add flow from the menu."""
    if action == "pay":
        kind = menu_pay_kind_callback
    elif action == "add":
        kind = menu_add_kind_callback
    else:
        raise ValueError(f"unknown menu kind action: {action}")
    builder = InlineKeyboardBuilder()
    builder.button(text="Подписка", callback_data=kind("subscription"))
    builder.button(text="Сервер", callback_data=kind("server"))
    builder.button(text=MENU_BACK_TEXT, callback_data=menu_callback("main"))
    builder.adjust(2, 1)
    return builder.as_markup()


def menu_entries_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    """«Оплачено ✓» per entry plus a back-to-menu button."""
    builder = InlineKeyboardBuilder()
    for i, entry in enumerate(entries, 1):
        builder.button(
            text=f"📌 {i}. {PAY_BUTTON_TEXT}",
            callback_data=pay_callback(entry["target_type"], entry["target_id"]),
        )
    builder.button(text=MENU_BACK_TEXT, callback_data=menu_callback("main"))
    builder.adjust(1)
    return builder.as_markup()


def menu_digest_keyboard(digest: dict) -> InlineKeyboardMarkup:
    """Digest «Оплачено ✓» buttons plus a back-to-menu button."""
    entries = [*digest["due_today"], *digest["due_soon"], *digest["overdue"]]
    return menu_entries_keyboard(entries)

VIEW_PREFIX = "view"
CONFIRM_AMT_PREFIX = "confirm_amount_ok"
CHANGE_AMT_PREFIX = "change_amount"
AMOUNT_NOTE_PREFIX = "amount_note"

VIEW_ACTIONS_PAY = "pay"
VIEW_ACTIONS_PAUSE = "pause"
VIEW_ACTIONS_RESUME = "resume"
VIEW_ACTIONS_SHIFT = "shift"
VIEW_ACTIONS_DELETE_CONFIRM = "delete:confirm"


def view_card_callback(action: str, item_id: int) -> str:
    """Build callback_data for a view card action, e.g. view:pay:5."""
    return f"{VIEW_PREFIX}:{action}:{item_id}"


def parse_view_card_callback(data: str | None) -> tuple[str, int] | None:
    """Parse view:{action}:{id}; return None for invalid payloads."""
    if not data or not data.startswith(f"{VIEW_PREFIX}:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    action, raw_id = parts[1], parts[2]
    try:
        item_id = int(raw_id)
    except ValueError:
        return None
    if item_id <= 0:
        return None
    return action, item_id


def confirm_amount_callback(target_type: str, target_id: int) -> str:
    """Build callback_data for confirming payment with original amount."""
    return f"{CONFIRM_AMT_PREFIX}:{target_type}:{target_id}"


def parse_confirm_amount_callback(data: str | None) -> tuple[str, int] | None:
    """Parse confirm_amount_ok:{type}:{id}; return None for invalid payloads."""
    if not data or not data.startswith(f"{CONFIRM_AMT_PREFIX}:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    target_type, raw_id = parts[1], parts[2]
    if target_type not in VALID_TARGET_TYPES:
        return None
    try:
        target_id = int(raw_id)
    except ValueError:
        return None
    if target_id <= 0:
        return None
    return target_type, target_id


def change_amount_callback(target_type: str, target_id: int) -> str:
    """Build callback_data for changing payment amount."""
    return f"{CHANGE_AMT_PREFIX}:{target_type}:{target_id}"


def parse_change_amount_callback(data: str | None) -> tuple[str, int] | None:
    """Parse change_amount:{type}:{id}; return None for invalid payloads."""
    if not data or not data.startswith(f"{CHANGE_AMT_PREFIX}:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    target_type, raw_id = parts[1], parts[2]
    if target_type not in VALID_TARGET_TYPES:
        return None
    try:
        target_id = int(raw_id)
    except ValueError:
        return None
    if target_id <= 0:
        return None
    return target_type, target_id


def view_card_keyboard(
    entity_type: str,
    item_id: int,
    has_status: bool = False,
    status_value: str | None = None,
) -> InlineKeyboardMarkup:
    """Action buttons for a view card message.

    entity_type: 'server', 'subscription', or 'expense'
    has_status: whether pause/resume buttons should be shown
    status_value: current status ('active', 'paused', etc.)
    """
    builder = InlineKeyboardBuilder()

    # Row 1: Pay + Pause/Resume
    builder.button(
        text=PAY_BUTTON_TEXT,
        callback_data=view_card_callback(VIEW_ACTIONS_PAY, item_id),
    )
    if has_status:
        if status_value == "paused":
            resume_text = "▶ Включить"
            builder.button(
                text=resume_text,
                callback_data=view_card_callback(VIEW_ACTIONS_RESUME, item_id),
            )
        else:
            pause_text = "⏸ Пауза"
            builder.button(
                text=pause_text,
                callback_data=view_card_callback(VIEW_ACTIONS_PAUSE, item_id),
            )
    builder.adjust(3)

    # Row 2: Shift date + Delete
    shift_text = "⏱ Сдвинуть дату"
    delete_text = "🗑️ Удалить"
    builder.button(
        text=shift_text,
        callback_data=view_card_callback(VIEW_ACTIONS_SHIFT, item_id),
    )
    builder.button(
        text=delete_text,
        callback_data=view_card_callback(VIEW_ACTIONS_DELETE_CONFIRM, item_id),
    )
    builder.adjust(2)

    # Row 3: Back to menu
    menu_text = "🏠 Меню"
    builder.button(text=menu_text, callback_data=menu_callback("main"))
    builder.adjust(1)

    return builder.as_markup()
