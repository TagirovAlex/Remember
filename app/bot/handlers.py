"""Telegram bot command handlers and callbacks (all user messages are in Russian)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import (
    PERIOD_LABELS,
    add_kind_keyboard,
    back_to_menu_keyboard,
    binding_notice_text,
    confirm_binding_keyboard,
    digest_pay_keyboard,
    expense_category_keyboard,
    main_menu_keyboard,
    menu_digest_keyboard,
    menu_entries_keyboard,
    menu_kind_keyboard,
    parse_add_kind_callback,
    parse_cancel_binding_callback,
    parse_confirm_binding_callback,
    parse_expense_category_callback,
    parse_menu_add_kind_callback,
    parse_menu_callback,
    parse_menu_pay_kind_callback,
    parse_pay_callback,
    parse_period_callback,
    pay_keyboard,
    period_keyboard,
)
from app.db import SessionLocal
from app.models import BotSubscriber, OneTimeExpense, Server, Subscription, User
from app.services.billing import (
    NotOwnerError,
    collect_digest,
    mark_paid_once,
    monthly_summary,
    owned_payments,
    search_owned,
)
from app.services.notify import (
    fmt_money,
    render_digest,
    render_entries,
    render_history,
    render_monthly_summary,
    render_search,
)
from app.services.settings import remind_soon_days
from app.services.settings import tz as get_tz

router = Router()

BINDING_HINT = (
    "Чтобы я показывал ваши данные, привяжите аккаунт: войдите в веб-интерфейс "
    "→ Настройки → Telegram, укажите свой ID (узнать: напишите @userinfobot) и "
    "подтвердите привязку в этом чате."
)

HELP_TEXT = (
    "Я — Remember, веду учёт серверов и подписок.\n\n"
    "Команды:\n"
    "/menu — главное меню\n"
    "/today — платежи на сегодня\n"
    "/soon — платежи на ближайшие N дней\n"
    "/overdue — просроченные платежи\n"
    "/stats — сводка за текущий месяц\n"
    "/addexpense — быстро добавить разовый расход\n"
    "/history [N] — последние платежи (N до 30)\n"
    "/find <запрос> — поиск по серверам, подпискам и расходам\n"
    "/list — серверы и подписки\n"
    "/add — быстро добавить позицию\n"
    "/subscribe — подписаться на дайджесты\n"
    "/unsubscribe — отписаться от дайджестов\n"
    "/help — справка\n\n"
    "По кнопке «Оплачено ✓» платёж фиксируется, дата сдвигается вперёд."
)

MAIN_MENU_TEXT = (
    "📋 Главное меню\n\n"
    "Подписки, серверы, оплаты и добавление — всё здесь."
)


session_factory: async_sessionmaker[AsyncSession] = SessionLocal


def set_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    """Override the DB session factory (used by tests and the main entrypoint)."""
    global session_factory
    session_factory = factory


async def today(session: AsyncSession) -> date:
    """Current date in the timezone configured in settings."""
    zone_name = await get_tz(session) or "UTC"
    try:
        tzinfo = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        tzinfo = ZoneInfo("UTC")
    return datetime.now(tzinfo).date()


def _digest_only(full: dict, key: str) -> dict:
    """Trim a collect_digest result to a single section keeping digest shape."""
    totals = {
        "due_today": "total_today",
        "due_soon": "total_soon",
        "overdue": "total_overdue",
    }
    result = {
        "due_today": [],
        "due_soon": [],
        "overdue": [],
        "total_today": 0,
        "total_soon": 0,
        "total_overdue": 0,
    }
    result[key] = full[key]
    result[totals[key]] = full[totals[key]]
    return result


async def _section_response(
    session: AsyncSession, key: str, owner_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    soon_days = await remind_soon_days(session)
    full = await collect_digest(
        session, await today(session), soon_days, owner_id=owner_id
    )
    digest = _digest_only(full, key)
    return render_digest(digest, soon_days=soon_days), digest_pay_keyboard(digest)


async def _subscriber(
    session: AsyncSession, message: Message
) -> BotSubscriber | None:
    return await session.scalar(
        select(BotSubscriber).where(BotSubscriber.chat_id == message.chat.id)
    )


async def _owner(session: AsyncSession, chat_id: int) -> User | None:
    """Resolve the confirmed owner of a chat, repairing the cached user_id."""
    sub = await session.scalar(
        select(BotSubscriber).where(BotSubscriber.chat_id == chat_id)
    )
    if sub is not None and sub.user_id is not None:
        user = await session.get(User, sub.user_id)
        if user is not None and user.is_active and user.telegram_id_confirmed:
            return user
    user = await session.scalar(select(User).where(User.telegram_id == chat_id))
    if user is not None and user.is_active and user.telegram_id_confirmed:
        if sub is None:
            session.add(BotSubscriber(chat_id=chat_id, user_id=user.id, is_active=True))
        else:
            sub.user_id = user.id
        await session.commit()
        return user
    return None


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with session_factory() as session:
        sub = await _subscriber(session, message)
        if sub is None:
            name = message.chat.title
            if name is None and message.from_user is not None:
                name = message.from_user.full_name
            sub = BotSubscriber(chat_id=message.chat.id, name=name, is_active=True)
            session.add(sub)
        elif not sub.is_active:
            sub.is_active = True
        user = await session.scalar(
            select(User).where(User.telegram_id == message.chat.id)
        )
        if user is not None and user.telegram_id_confirmed:
            sub.user_id = user.id
            sub.is_active = True
        await session.commit()
    if user is not None and user.telegram_id_confirmed:
        await message.answer(
            f"Привет, {user.username}! Дайджесты и команды будут приходить сюда.",
            reply_markup=main_menu_keyboard(),
        )
    elif user is not None and user.telegram_id is not None:
        await message.answer(
            binding_notice_text(user.username),
            reply_markup=confirm_binding_keyboard(user.id),
        )
    else:
        await message.answer(f"{HELP_TEXT}\n\n{BINDING_HINT}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        await session.commit()
        if user is None:
            await message.answer(BINDING_HINT)
            return
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


async def _menu_owner(callback: CallbackQuery) -> User | None:
    """Resolve the chat owner for a menu callback, or None when not bound."""
    async with session_factory() as session:
        chat_id = callback.message.chat.id if callback.message is not None else 0
        user = await _owner(session, chat_id)
        await session.commit()
        return user


async def _menu_binding_hint(callback: CallbackQuery) -> None:
    await callback.answer("Сначала привяжите аккаунт")
    if callback.message is not None:
        try:
            await callback.message.answer(BINDING_HINT)
        except TelegramBadRequest:
            return


async def _menu_edit(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    """Replace the current menu message, keeping a single message in chat."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        return


async def _menu_list(
    session: AsyncSession, owner_id: int, target_type: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Active items of one kind with pay buttons and a back-to-menu button."""
    if target_type == "server":
        model = Server
        active = Server.is_active.is_(True)
        title, empty = "🖥 Серверы", "Серверов нет"
    else:
        model = Subscription
        active = Subscription.status == "active"
        title, empty = "📡 Подписки", "Подписок нет"
    items = (
        await session.scalars(
            select(model)
            .where(model.owner_id == owner_id, active)
            .order_by(model.name)
        )
    ).all()
    entries = [
        {
            "name": item.name,
            "amount_rub": item.amount_rub,
            "next_due": item.next_due,
            "target_type": target_type,
            "target_id": item.id,
        }
        for item in items
    ]
    return render_entries(title, entries, empty), menu_entries_keyboard(entries)


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery, state: FSMContext) -> None:
    action = parse_menu_callback(callback.data)
    if action is not None:
        await _menu_action(callback, action)
        return
    target_type = parse_menu_pay_kind_callback(callback.data)
    if target_type is not None:
        await _menu_pay_kind(callback, target_type)
        return
    target_type = parse_menu_add_kind_callback(callback.data)
    if target_type is not None:
        await _menu_add_kind(callback, state, target_type)
        return
    await callback.answer("Некорректная кнопка")


async def _menu_action(callback: CallbackQuery, action: str) -> None:
    user = await _menu_owner(callback)
    if user is None:
        await _menu_binding_hint(callback)
        return
    if action == "main":
        await _menu_edit(callback, MAIN_MENU_TEXT, main_menu_keyboard())
    elif action in ("pay", "add"):
        text = "Что оплачиваем?" if action == "pay" else "Что добавляем?"
        await _menu_edit(callback, text, menu_kind_keyboard(action))
    elif action in ("subs", "servers"):
        target_type = "subscription" if action == "subs" else "server"
        async with session_factory() as session:
            text, keyboard = await _menu_list(session, user.id, target_type)
        await _menu_edit(callback, text, keyboard)
    else:
        async with session_factory() as session:
            soon_days = await remind_soon_days(session)
            digest = await collect_digest(
                session, await today(session), soon_days, owner_id=user.id
            )
        text = f"📊 Дашборд\n\n{render_digest(digest, soon_days=soon_days)}"
        await _menu_edit(callback, text, menu_digest_keyboard(digest))


async def _menu_pay_kind(callback: CallbackQuery, target_type: str) -> None:
    user = await _menu_owner(callback)
    if user is None:
        await _menu_binding_hint(callback)
        return
    async with session_factory() as session:
        text, keyboard = await _menu_list(session, user.id, target_type)
    await _menu_edit(callback, text, keyboard)


async def _menu_add_kind(
    callback: CallbackQuery, state: FSMContext, target_type: str
) -> None:
    user = await _menu_owner(callback)
    if user is None:
        await _menu_binding_hint(callback)
        return
    await state.update_data(owner_id=user.id, target_type=target_type)
    await state.set_state(AddStates.name)
    await callback.answer("Сервер" if target_type == "server" else "Подписка")
    if callback.message is None:
        return
    try:
        await callback.message.edit_text("Название:")
    except TelegramBadRequest:
        return


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        sub = await _subscriber(session, message)
        if sub is None:
            session.add(
                BotSubscriber(
                    chat_id=message.chat.id, user_id=user.id, is_active=True
                )
            )
        else:
            sub.user_id = user.id
            sub.is_active = True
        await session.commit()
    await message.answer("Дайджесты включены ✅")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        sub = await _subscriber(session, message)
        if sub is None:
            session.add(BotSubscriber(chat_id=message.chat.id, is_active=False))
        else:
            sub.is_active = False
        await session.commit()
    await message.answer("Дайджесты выключены. Вернуть их всегда можно через /subscribe")


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        text, keyboard = await _section_response(session, "due_today", user.id)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("soon"))
async def cmd_soon(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        text, keyboard = await _section_response(session, "due_soon", user.id)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("overdue"))
async def cmd_overdue(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        text, keyboard = await _section_response(session, "overdue", user.id)
    await message.answer(text, reply_markup=keyboard)


async def _list_text(
    session: AsyncSession, owner_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    lines: list[str] = ["🖥 Серверы", ""]
    servers = (
        await session.scalars(
            select(Server).where(Server.owner_id == owner_id).order_by(Server.name)
        )
    ).all()
    entries: list[dict] = []
    if servers:
        for server in servers:
            status = "" if server.is_active else " (выключен)"
            lines.append(
                f"• {server.name} — {server.next_due:%d.%m.%Y} — "
                f"{fmt_money(server.amount_rub)}{status}"
            )
            entries.append(
                {
                    "name": server.name,
                    "kind": "server",
                    "amount_rub": server.amount_rub,
                    "next_due": server.next_due,
                    "target_type": "server",
                    "target_id": server.id,
                }
            )
    else:
        lines.append("—")
    lines += ["", "📡 Подписки", ""]
    subscriptions = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.owner_id == owner_id)
            .order_by(Subscription.name)
        )
    ).all()
    if subscriptions:
        for sub in subscriptions:
            status = " (пауза)" if sub.status != "active" else ""
            lines.append(
                f"• {sub.name} — {sub.next_due:%d.%m.%Y} — "
                f"{fmt_money(sub.amount_rub)}{status}"
            )
            entries.append(
                {
                    "name": sub.name,
                    "kind": "subscription",
                    "amount_rub": sub.amount_rub,
                    "next_due": sub.next_due,
                    "target_type": "subscription",
                    "target_id": sub.id,
                }
            )
    else:
        lines.append("—")
    return "\n".join(lines), pay_keyboard(entries)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        text, keyboard = await _list_text(session, user.id)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        ref_month = await today(session)
        summary = await monthly_summary(session, user.id, ref_month, today=ref_month)
        await session.commit()
    await message.answer(render_monthly_summary(summary))


async def _target_names(
    session: AsyncSession, owner_id: int
) -> dict[str, dict[int, str]]:
    servers = (
        await session.scalars(select(Server).where(Server.owner_id == owner_id))
    ).all()
    subscriptions = (
        await session.scalars(
            select(Subscription).where(Subscription.owner_id == owner_id)
        )
    ).all()
    return {
        "server": {server.id: server.name for server in servers},
        "subscription": {
            subscription.id: subscription.name for subscription in subscriptions
        },
    }


async def _history_rows(
    session: AsyncSession, owner_id: int, limit: int
) -> list[dict]:
    payments = await owned_payments(session, owner_id, limit=limit)
    names = await _target_names(session, owner_id)
    users = (await session.scalars(select(User))).all()
    user_names = {user.id: user.username for user in users}
    rows: list[dict] = []
    for payment in payments:
        rows.append(
            {
                "paid_date": payment.paid_date,
                "name": names.get(payment.target_type, {}).get(
                    payment.target_id, "—"
                ),
                "amount_rub": payment.amount_rub,
                "paid_by": (
                    user_names.get(payment.paid_by) if payment.paid_by else None
                ),
                "notes": payment.notes,
            }
        )
    return rows


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    limit = 10
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        raw = args[1].strip()
        try:
            limit = int(raw)
        except ValueError:
            await message.answer("Использование: /history [N], N от 1 до 30")
            return
        if limit < 1 or limit > 30:
            await message.answer("N должно быть от 1 до 30")
            return
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        rows = await _history_rows(session, user.id, limit)
        await session.commit()
    await message.answer(render_history(rows, limit))


@router.message(Command("find"))
async def cmd_find(message: Message) -> None:
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "Использование: /find <запрос>, например /find gpu node"
        )
        return
    query = args[1].strip()
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return
        results = await search_owned(session, user.id, query)
        await session.commit()
    await message.answer(render_search(query, results))


class AddStates(StatesGroup):
    """FSM steps for the /add command."""

    kind = State()
    name = State()
    amount = State()
    period = State()
    next_due = State()


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        await session.commit()
        if user is None:
            await message.answer(BINDING_HINT)
            return
        owner_id = user.id
    await state.update_data(owner_id=owner_id)
    await state.set_state(AddStates.kind)
    await message.answer("Что добавляем?", reply_markup=add_kind_keyboard())


@router.message(AddStates.kind)
async def add_kind_wait_buttons(message: Message) -> None:
    await message.answer("Выберите тип кнопкой ниже:", reply_markup=add_kind_keyboard())


@router.callback_query(AddStates.kind, F.data)
async def add_choose_kind(callback: CallbackQuery, state: FSMContext) -> None:
    target_type = parse_add_kind_callback(callback.data)
    if target_type is None:
        await callback.answer("Некорректная кнопка")
        return
    await state.update_data(target_type=target_type)
    await state.set_state(AddStates.name)
    await callback.answer("Сервер" if target_type == "server" else "Подписка")
    if callback.message is None:
        return
    await callback.message.delete()
    await callback.message.answer("Название:")


@router.message(AddStates.name, F.text)
async def add_enter_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым")
        return
    if len(name) > 100:
        await message.answer("Слишком длинное название (до 100 символов)")
        return
    await state.update_data(name=name)
    await state.set_state(AddStates.amount)
    await message.answer("Сумма в рублях (целое число):")


@router.message(AddStates.amount, F.text)
async def add_enter_amount(message: Message, state: FSMContext) -> None:
    clean = message.text.strip().replace(" ", "").replace("₽", "")
    try:
        amount = int(clean)
    except ValueError:
        await message.answer("Введите сумму целым числом в рублях:")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля:")
        return
    await state.update_data(amount_rub=amount)
    await state.set_state(AddStates.period)
    await message.answer("Период оплаты:", reply_markup=period_keyboard())


@router.callback_query(AddStates.period, F.data)
async def add_choose_period(callback: CallbackQuery, state: FSMContext) -> None:
    period = parse_period_callback(callback.data)
    if period is None:
        await callback.answer("Некорректная кнопка")
        return
    await state.update_data(period=period)
    await state.set_state(AddStates.next_due)
    await callback.answer(PERIOD_LABELS[period])
    if callback.message is None:
        return
    await callback.message.delete()
    await callback.message.answer("Дата следующего платежа (ГГГГ-ММ-ДД):")


@router.message(AddStates.next_due, F.text)
async def add_enter_next_due(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    try:
        next_due = date.fromisoformat(raw)
    except ValueError:
        await message.answer("Формат даты: ГГГГ-ММ-ДД, например 2026-10-01")
        return

    data = await state.get_data()
    target_type = data["target_type"]
    owner_id = data["owner_id"]
    async with session_factory() as session:
        if target_type == "server":
            item = Server(
                name=data["name"],
                kind="vm",
                provider="",
                amount_rub=data["amount_rub"],
                period=data["period"],
                next_due=next_due,
                is_active=True,
                owner_id=owner_id,
            )
        else:
            item = Subscription(
                name=data["name"],
                provider="",
                amount_rub=data["amount_rub"],
                period=data["period"],
                next_due=next_due,
                status="active",
                owner_id=owner_id,
            )
        session.add(item)
        try:
            await session.commit()
        except IntegrityError:
            await message.answer("Позиция с таким названием уже существует")
            await state.clear()
            return
        await session.refresh(item)

    await state.clear()
    label = "Сервер" if target_type == "server" else "Подписка"
    await message.answer(
        f"✅ {label} «{item.name}» добавлен(а): {fmt_money(item.amount_rub)} "
        f"— до {item.next_due:%d.%m.%Y}"
    )


class ExpenseStates(StatesGroup):
    """FSM steps for the /addexpense command."""

    name = State()
    amount = State()
    category = State()
    paid_date = State()


@router.message(Command("addexpense"))
async def cmd_addexpense(message: Message, state: FSMContext) -> None:
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        await session.commit()
        if user is None:
            await message.answer(BINDING_HINT)
            return
        owner_id = user.id
    await state.update_data(owner_id=owner_id)
    await state.set_state(ExpenseStates.name)
    await message.answer("Название расхода:")


@router.message(ExpenseStates.name, F.text)
async def expense_enter_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым")
        return
    if len(name) > 200:
        await message.answer("Слишком длинное название (до 200 символов)")
        return
    await state.update_data(name=name)
    await state.set_state(ExpenseStates.amount)
    await message.answer("Сумма в рублях (целое число):")


@router.message(ExpenseStates.amount, F.text)
async def expense_enter_amount(message: Message, state: FSMContext) -> None:
    clean = message.text.strip().replace(" ", "").replace("₽", "")
    try:
        amount = int(clean)
    except ValueError:
        await message.answer("Введите сумму целым числом в рублях:")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля:")
        return
    await state.update_data(amount_rub=amount)
    await state.set_state(ExpenseStates.category)
    await message.answer("Категория:", reply_markup=expense_category_keyboard())


@router.callback_query(ExpenseStates.category, F.data)
async def expense_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = parse_expense_category_callback(callback.data)
    if category is None:
        await callback.answer("Некорректная кнопка")
        return
    await state.update_data(category=category)
    await state.set_state(ExpenseStates.paid_date)
    await callback.answer(category)
    if callback.message is None:
        return
    await callback.message.delete()
    await callback.message.answer(
        "Дата оплаты (ДД.ММ.ГГГГ) или Enter — сегодня:"
    )


@router.message(ExpenseStates.paid_date, F.text)
async def expense_enter_date(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if raw:
        try:
            day, month, year = (int(part) for part in raw.split("."))
            paid_date = date(year, month, day)
        except ValueError:
            await message.answer("Формат даты: ДД.ММ.ГГГГ, например 15.09.2026")
            return
    else:
        async with session_factory() as session:
            paid_date = await today(session)

    data = await state.get_data()
    async with session_factory() as session:
        expense = OneTimeExpense(
            owner_id=data["owner_id"],
            title=data["name"],
            amount_rub=data["amount_rub"],
            paid_date=paid_date,
            category=data["category"],
        )
        session.add(expense)
        await session.commit()
        await session.refresh(expense)

    await state.clear()
    await message.answer(
        f"✅ Расход «{expense.title}» — {fmt_money(expense.amount_rub)}, "
        f"{expense.paid_date:%d.%m.%Y}"
    )


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять")
        return
    await state.clear()
    await message.answer("Отменено")


async def _get_target(
    session: AsyncSession, target_type: str, target_id: int
) -> Server | Subscription:
    model = Server if target_type == "server" else Subscription
    entity = await session.get(model, target_id)
    if entity is None:
        raise ValueError(f"{target_type} {target_id} not found")
    return entity


@router.callback_query(F.data.startswith("pay:"))
async def on_pay(callback: CallbackQuery) -> None:
    parsed = parse_pay_callback(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка")
        return
    target_type, target_id = parsed

    async with session_factory() as session:
        chat_id = callback.message.chat.id if callback.message is not None else 0
        user = await _owner(session, chat_id)
        if user is None:
            await session.commit()
            await callback.answer("Сначала привяжите аккаунт")
            if callback.message is not None:
                try:
                    await callback.message.answer(BINDING_HINT)
                except TelegramBadRequest:
                    return
            return
        try:
            entity = await _get_target(session, target_type, target_id)
            _, created = await mark_paid_once(
                session,
                target_type,
                target_id,
                amount_rub=entity.amount_rub,
                paid_date=await today(session),
                owner_id=user.id,
                paid_by=user.id,
            )
        except (ValueError, NotOwnerError):
            await callback.answer("Позиция не найдена")
            return
        await session.commit()
        await session.refresh(entity)

    if not created:
        await callback.answer("Платёж за этот период уже отмечен")
        return
    await callback.answer("Оплачено ✓")
    if callback.message is not None:
        digest_text = (
            f"✅ {entity.name}\n"
            f"Оплачено ✓ {fmt_money(entity.amount_rub)}.\n"
            f"Следующий платёж: {entity.next_due:%d.%m.%Y}"
        )
        try:
            await callback.message.edit_text(
                digest_text, reply_markup=back_to_menu_keyboard()
            )
        except TelegramBadRequest:
            return


@router.callback_query(F.data.startswith("tg_confirm:"))
async def on_tg_confirm(callback: CallbackQuery) -> None:
    user_id = parse_confirm_binding_callback(callback.data)
    if user_id is None:
        await callback.answer("Некорректная кнопка")
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
        chat_id = callback.message.chat.id if callback.message is not None else 0
        if user is None or not user.is_active or user.telegram_id != chat_id:
            await callback.answer("Привязка отклонена: несовпадение чата")
            return
        user.telegram_id_confirmed = True
        sub = await session.scalar(
            select(BotSubscriber).where(BotSubscriber.chat_id == chat_id)
        )
        if sub is None:
            session.add(
                BotSubscriber(chat_id=chat_id, user_id=user.id, is_active=True)
            )
        else:
            sub.user_id = user.id
            sub.is_active = True
        await session.commit()
    await callback.answer("Привязка подтверждена ✅")
    if callback.message is not None:
        try:
            await callback.message.edit_text("Привязка подтверждена ✅")
        except TelegramBadRequest:
            return


@router.callback_query(F.data.startswith("tg_cancel:"))
async def on_tg_cancel(callback: CallbackQuery) -> None:
    user_id = parse_cancel_binding_callback(callback.data)
    if user_id is None:
        await callback.answer("Некорректная кнопка")
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is not None:
            user.telegram_id = None
            user.telegram_id_confirmed = False
            await session.commit()
    await callback.answer("Привязка отменена")
    if callback.message is not None:
        try:
            await callback.message.edit_text("Привязка отменена")
        except TelegramBadRequest:
            return