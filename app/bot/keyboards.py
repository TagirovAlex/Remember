"""Inline keyboards and callback_data builders/parsers for the Telegram bot.

Callback formats (compact, <=64 bytes):
    pay:{target_type}:{target_id}   — mark an entry as paid
    add:{target_type}               — choose what to add in /add FSM
    period:{period}                 — choose billing period in /add FSM
    tg_confirm:{user_id}            — confirm a Telegram binding
    tg_cancel:{user_id}             — reject a Telegram binding
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAY_PREFIX = "pay"
ADD_PREFIX = "add"
PERIOD_PREFIX = "period"
TG_CONFIRM_PREFIX = "tg_confirm"
TG_CANCEL_PREFIX = "tg_cancel"

VALID_TARGET_TYPES = ("server", "subscription")
VALID_PERIODS = ("monthly", "quarterly", "yearly")

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


def pay_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    """One «Оплачено ✓» button per entry (digest/list item)."""
    builder = InlineKeyboardBuilder()
    for entry in entries:
        builder.button(
            text=PAY_BUTTON_TEXT,
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