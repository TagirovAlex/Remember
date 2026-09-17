"""Telegram bot command handlers and callbacks (all user messages are in Russian)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import (
    PERIOD_LABELS,
    add_kind_keyboard,
    back_to_menu_keyboard,
    binding_notice_text,
    change_amount_callback,
    confirm_amount_callback,
    confirm_binding_keyboard,
    digest_pay_keyboard,
    expense_category_keyboard,
    main_menu_keyboard,
    menu_digest_keyboard,
    menu_entries_keyboard,
    menu_kind_keyboard,
    parse_add_kind_callback,
    parse_cancel_binding_callback,
    parse_change_amount_callback,
    parse_confirm_amount_callback,
    parse_confirm_binding_callback,
    parse_expense_category_callback,
    parse_menu_add_kind_callback,
    parse_menu_callback,
    parse_menu_pay_kind_callback,
    parse_pay_callback,
    parse_period_callback,
    parse_view_card_callback,
    pay_keyboard,
    period_keyboard,
    view_card_keyboard,
)
from app.db import SessionLocal
from app.models import BotSubscriber, OneTimeExpense, Payment, Server, Subscription, User
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
    "/view <id> — детальная карточка позиции\n"
    "/export — выгрузить все данные файлом\n"
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
        for i, server in enumerate(servers, 1):
            status = "" if server.is_active else " (выключен)"
            lines.append(
                f"{i}. {server.name} — {server.next_due:%d.%m.%Y} — "
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
        for i, sub in enumerate(subscriptions, 1):
            status = " (пауза)" if sub.status != "active" else ""
            lines.append(
                f"{i}. {sub.name} — {sub.next_due:%d.%m.%Y} — "
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




class ViewDateStates(StatesGroup):
    """FSM for shifting next_due via /view card."""
    wait_date = State()


class AmountChangeStates(StatesGroup):
    """FSM for changing payment amount."""
    wait_amount = State()
    wait_note = State()


# Pre-computed unicode values (avoid \u escapes inside f-strings)
_E = chr(0x2014)  # em dash
_D = chr(0x2022)  # bullet
_B = chr(0x2500) * 22  # box line
_MENU = chr(0xf3e0) + " Меню"  # house emoji
_SERVER_EMOJI = chr(0xf5a1)
_CARD_EMOJI = chr(0xf4e1)
_SPEND_EMOJI = chr(0xf4b8)
_PAY_OK = chr(0x2705)
_CHECK = chr(0x2713)
_CLOCK = chr(0x23f0)
_PAUSE = chr(0x23f8)
_PLAY = chr(0x25b6)


def _fmt_next_due(d):
    """Format a date as DD.MM.YYYY."""
    return d.strftime("%d.%m.%Y") if isinstance(d, date) else _E


def _format_card(entity, target_type):
    """Build a detailed card text for an entity."""
    if target_type == "server":
        kind_label = entity.kind or "?"
        what_runs = entity.what_runs or ""
        panel = entity.panel_url or _E
        specs = entity.specs or _E
        notes_field = entity.notes or _E
        status_text = "\u0410\u043a\u0442\u0438\u0432\u043d\u0430" if entity.is_active else "\u041d\u0435\u0430\u043a\u0442\u0438\u0432"
        cards = [
            f"{_SERVER_EMOJI} {entity.name}",
            f"\u0422\u0438\u043f: {kind_label} | {entity.provider}",
            f"\u041f\u0440\u043e\u0432\u0430\u0439\u0434\u0435\u0440: {entity.provider}",
            f"IP: {entity.ip or _E}",
            f"\u041f\u0430\u043d\u0435\u043b\u044c: {panel}",
            f"\u0421\u043f\u0435\u0446\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044f: {specs}",
            f"\u0427\u0442\u043e \u043a\u0440\u0443\u0442\u0438\u0442\u0441\u044f: {what_runs}",
            f"\u0417\u0430\u043c\u0435\u0442\u043a\u0438: {notes_field}",
            _B,
            f"{_CARD_EMOJI} \u0421\u0443\u043c\u043c\u0430: {fmt_money(entity.amount_rub)} {_D} \u041f\u0435\u0440\u0438\u043e\u0434: {PERIOD_LABELS.get(entity.period, entity.period)}",
            f"{_CLOCK} \u041e\u043f\u043b\u0430\u0442\u0430 \u0434\u043e: {_fmt_next_due(entity.next_due)}",
            f"\u0421\u0442\u0430\u0442\u0443\u0441: {status_text}",
            _B,
        ]
        return "\n".join(cards)
    elif target_type == "subscription":
        auto = "\u0414\u0430" if getattr(entity, "auto_renewal", True) else "\u041d\u0435\u0442"
        smap = {"active": "\u0410\u043a\u0442\u0438\u0432\u043d\u0430", "paused": "\u041d\u0430 \u043f\u0430\u0443\u0437\u0435", "archived": "\u0410\u0440\u0445\u0438\u0432"}
        status_txt = smap.get(entity.status, entity.status)
        cards = [
            f"{_CARD_EMOJI} {entity.name}",
            f"\u0422\u0438\u043f: {entity.provider}",
            f"\u041f\u0440\u043e\u0432\u0430\u0439\u0434\u0435\u0440: {entity.provider}",
            "Спецификация: " + _E,
            f"Заметки: {entity.notes or _E}",
            _B,
            f"{_CARD_EMOJI} Сумма: {fmt_money(entity.amount_rub)} {_D} Период: {PERIOD_LABELS.get(entity.period, entity.period)}",
            f"{_CLOCK} Оплата до: {_fmt_next_due(entity.next_due)}",
            f"Автопродление: {auto}",
            f"Статус: {status_txt}",
            _B,
        ]
        return "\n".join(cards)
    else:
        cat = entity.category or _E
        cards = [
            f"{_SPEND_EMOJI} {entity.title}",
            f"Категория: {cat}",
            f"Дата: {entity.paid_date:%d.%m.%Y}",
            _B,
            f"{_CARD_EMOJI} Сумма: {fmt_money(entity.amount_rub)}",
            _B,
        ]
        return "\n".join(cards)


async def cmd_view(message: Message) -> None:
    """Handle /view <id> command -- show detailed card for a position."""
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        await session.commit()
        if user is None:
            await message.answer("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 ID: /view <id>")
            return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 ID: /view <id>")
        return
    raw_id = args[1].strip()
    try:
        item_id = int(raw_id)
    except ValueError:
        await message.answer("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 ID: /view <id>")
        return

    async with session_factory() as session:
        chat_id = message.chat.id
        owner = await _owner(session, chat_id)
        if owner is None:
            await session.commit()
            await message.answer(BINDING_HINT)
            return

        entity = None
        target_type = None

        server = await session.get(Server, item_id)
        if server is not None and server.owner_id == owner.id:
            entity = server
            target_type = "server"

        if entity is None:
            sub = await session.get(Subscription, item_id)
            if sub is not None and sub.owner_id == owner.id:
                entity = sub
                target_type = "subscription"

        if entity is None:
            expense = await session.scalar(
                select(OneTimeExpense).where(
                    OneTimeExpense.id == item_id,
                    OneTimeExpense.owner_id == owner.id,
                )
            )
            if expense is not None:
                entity = expense
                target_type = "expense"

        await session.commit()

    if entity is None:
        await message.answer("\u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")
        return

    assert target_type is not None
    card_text = _format_card(entity, target_type)
    has_status = target_type in ("server", "subscription")
    status_value = getattr(entity, "status", None) if target_type == "subscription" else None
    kb = view_card_keyboard(target_type, item_id, has_status, status_value)
    await message.answer(card_text, reply_markup=kb)


@router.callback_query(F.data.startswith("view:"))
async def on_view_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle all view:* callbacks."""
    parsed = parse_view_card_callback(callback.data)
    if parsed is None:
        await callback.answer("\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u0430\u044f \u043a\u043d\u043e\u043f\u043a\u0430")
        return
    action, item_id = parsed

    async with session_factory() as session:
        chat_id = callback.message.chat.id if callback.message is not None else 0
        user = await _owner(session, chat_id)
        if user is None:
            await session.commit()
            await callback.answer("\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u0440\u0438\u0432\u044f\u0436\u0442\u0435 \u0430\u043a\u043a\u0430\u0443\u043d\u0442")
            return

        entity = None
        target_type = None

        server = await session.get(Server, item_id)
        if server is not None and server.owner_id == user.id:
            entity = server
            target_type = "server"

        if entity is None:
            sub = await session.get(Subscription, item_id)
            if sub is not None and sub.owner_id == user.id:
                entity = sub
                target_type = "subscription"

        if entity is None:
            expense = await session.scalar(
                select(OneTimeExpense).where(
                    OneTimeExpense.id == item_id,
                    OneTimeExpense.owner_id == user.id,
                )
            )
            if expense is not None:
                entity = expense
                target_type = "expense"

        if entity is None:
            await callback.answer("\u041f\u043e\u0437\u0438\u0446\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")
            await session.commit()
            return

        if action == "pay":
            txt = await _handle_pay_in_view(session, target_type, item_id, entity, user)
            if txt is not None:
                await callback.answer(txt)
                return

        elif action == "pause":
            from app.models import Server as SModel
            from app.models import Subscription as SubModel
            if isinstance(entity, SModel):
                entity.is_active = False
            elif isinstance(entity, SubModel):
                entity.status = "paused"
            else:
                await callback.answer("\u041f\u0430\u0443\u0437\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432 \u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a")
                await session.commit()
                return
            await session.commit()
            await session.refresh(entity)
            ct = _format_card(entity, target_type)
            sv = entity.status if target_type == "subscription" else None
            kb = view_card_keyboard(target_type, item_id, True, sv)
            await callback.answer()
            await _edit_msg(callback, ct, kb)

        elif action == "resume":
            from app.models import Server as SModel
            from app.models import Subscription as SubModel
            if isinstance(entity, SModel):
                entity.is_active = True
            elif isinstance(entity, SubModel):
                entity.status = "active"
            else:
                await callback.answer("\u0412\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432 \u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a")
                await session.commit()
                return
            await session.commit()
            await session.refresh(entity)
            ct = _format_card(entity, target_type)
            sv = entity.status if target_type == "subscription" else None
            kb = view_card_keyboard(target_type, item_id, True, sv)
            await callback.answer()
            await _edit_msg(callback, ct, kb)

        elif action == "shift":
            await state.set_state(ViewDateStates.wait_date)
            await state.update_data(item_id=item_id, target_type=target_type)
            msg_text = (
                "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u0443\u044e \u0434\u0430\u0442\u0443 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u0439 \u043e\u043f\u043b\u0430\u0442\u044b\n"
                "(\u0414\u0414.\u041c\u041c.\u0413\u0413\u0413\u0413, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440 15.10.2026)\n\n"
                "\u041d\u0430\u0436\u043c\u0438\u0442\u0435 /cancel \u0434\u043b\u044f \u043e\u0442\u043c\u0435\u043d\u044b."
            )
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        msg_text,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="menu:main")]
                        ]),
                    )
                except TelegramBadRequest:
                    pass
            await callback.answer()

        elif action == "delete:confirm":
            yes_cb = f"view:delete:true:{item_id}:{target_type}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u0414\u0430, \u0442\u043e\u0447\u043d\u043e", callback_data=yes_cb)],
                [InlineKeyboardButton(text="\u041d\u0435\u0442, \u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c", callback_data="menu:main")],
            ])
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u043f\u043e\u0437\u0438\u0446\u0438\u044e?",
                        reply_markup=kb,
                    )
                except TelegramBadRequest:
                    pass
            await callback.answer()

    await callback.answer()


async def _handle_pay_in_view(session, target_type, item_id, entity, user):
    """Pay button handler inside view card. Returns error text or None."""
    try:
        _, created = await mark_paid_once(
            session,
            target_type,
            item_id,
            amount_rub=entity.amount_rub,
            paid_date=await today(session),
            owner_id=user.id,
            paid_by=user.id,
        )
    except NotOwnerError:
        return "\u041f\u043e\u0437\u0438\u0446\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430"
    await session.commit()
    if not created:
        return "\u041f\u043b\u0430\u0442\u0435\u0436 \u0437\u0430 \u044d\u0442\u043e\u0442 \u043f\u0435\u0440\u0438\u043e\u0434 \u0443\u0436\u0435 \u043e\u0442\u043c\u0435\u0447\u0435\u043d"
    await session.refresh(entity)
    next_str = _fmt_next_due(entity.next_due)
    return f"{_PAY_OK} {entity.name}\n\u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e {_CHECK} {fmt_money(entity.amount_rub)}.\n\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u043f\u043b\u0430\u0442\u0435\u0436: {next_str}"


@router.callback_query(F.data.startswith("view:delete:"))
async def on_delete_execute(callback: CallbackQuery) -> None:
    """Execute deletion when confirm:delete:true:id:type."""
    data = callback.data
    parts = data.split(":")
    if len(parts) != 5 or parts[1] != "delete" or parts[2] != "true":
        await callback.answer("\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u0430\u044f \u043a\u043d\u043e\u043f\u043a\u0430")
        return
    try:
        exc_item_id = int(parts[3])
        exc_target_type = parts[4]
    except (ValueError, IndexError):
        await callback.answer("\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u0430\u044f \u043a\u043d\u043e\u043f\u043a\u0430")
        return

    async with session_factory() as session:
        chat_id = callback.message.chat.id if callback.message is not None else 0
        user = await _owner(session, chat_id)
        if user is None:
            await session.commit()
            return

        model_cls = Server if exc_target_type == "server" else (Subscription if exc_target_type == "subscription" else OneTimeExpense)
        entity = await session.get(model_cls, exc_item_id)
        if entity is None or entity.owner_id != user.id:
            await callback.answer("\u041f\u043e\u0437\u0438\u0446\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")
            await session.commit()
            return

        if exc_target_type in ("server", "subscription"):
            stmt = select(Payment).where(
                Payment.target_type == exc_target_type,
                Payment.target_id == exc_item_id,
            )
            payments = (await session.scalars(stmt)).all()
            for p in payments:
                await session.delete(p)

        await session.delete(entity)
        await session.commit()

    txt = "\u0423\u0434\u0430\u043b\u0435\u043d\u043e"
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                txt,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{_MENU}", callback_data="menu:main")]
                ]),
            )
        except TelegramBadRequest:
            pass
    await callback.answer()


async def _amount_confirm_text(entity, original_amount):
    """Build the intermediate payment confirmation message."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    t = "subscription" if isinstance(entity, Subscription) else "server"
    text = (
        f"{_PAY_OK} \u041e\u043f\u043b\u0430\u0442\u0430 \u0437\u0430 \u043f\u043e\u0437\u0438\u0446\u0438\u044e \u0443\u0441\u043f\u0435\u0448\u043d\u043e\n"
        f"\u0421\u0443\u043c\u043c\u0430 \u0431\u044b\u043b\u0430 {fmt_money(original_amount)}. \u0412\u0432\u0435\u0441\u0442\u0438 \u0434\u0440\u0443\u0433\u0443\u044e?\n"
        f"(\u2014 \u2014 \u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c {fmt_money(original_amount)}",
                callback_data=confirm_amount_callback(t, entity.id),
            )
        ],
        [
            InlineKeyboardButton(
                text="\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0441\u0443\u043c\u043c\u0443",
                callback_data=change_amount_callback(t, entity.id),
            )
        ],
    ])
    return text, kb


@router.callback_query(F.data.startswith("confirm_amount_ok:"))
async def on_confirm_amount_ok(callback: CallbackQuery) -> None:
    """Confirm payment with original amount."""
    parsed = parse_confirm_amount_callback(callback.data)
    if parsed is None:
        await callback.answer("\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u0430\u044f \u043a\u043d\u043e\u043f\u043a\u0430")
        return
    target_type, target_id = parsed

    async with session_factory() as session:
        chat_id = callback.message.chat.id if callback.message is not None else 0
        user = await _owner(session, chat_id)
        if user is None:
            await session.commit()
            return

        entity = await _get_target_local(session, target_type, target_id)
        try:
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
            await callback.answer("\u041f\u043e\u0437\u0438\u0446\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")
            await session.commit()
            return

        if not created:
            await callback.answer("\u041f\u043b\u0430\u0442\u0435\u0436 \u0443\u0436\u0435 \u043e\u0442\u043c\u0435\u0447\u0435\u043d")
            await session.commit()
            return

        await session.commit()
        await session.refresh(entity)

    next_str = _fmt_next_due(entity.next_due)
    txt = f"{_PAY_OK} \u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e {_CHECK} \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u043e\u043f\u043b\u0430\u0442\u0430: {next_str}"
    await callback.answer()
    if callback.message is not None:
        try:
            await callback.message.edit_text(txt, reply_markup=back_to_menu_keyboard())
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("change_amount:"))
async def on_change_amount(callback: CallbackQuery, state: FSMContext) -> None:
    """Start amount change FSM."""
    parsed = parse_change_amount_callback(callback.data)
    if parsed is None:
        await callback.answer("\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u0430\u044f \u043a\u043d\u043e\u043f\u043a\u0430")
        return
    target_type, target_id = parsed

    async with session_factory() as session:
        entity = await _get_target_local(session, target_type, target_id)
        default_amt = entity.amount_rub

    await state.update_data(target_type=target_type, target_id=target_id, default_amount=default_amt)
    await state.set_state(AmountChangeStates.wait_amount)
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                f"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u0443\u044e \u0441\u0443\u043c\u043c\u0443 (\u0446\u0435\u043b\u043e\u0435 \u0447\u0438\u0441\u043b\u043e > 0):\n"
                f"(\u2014 \u2014 \u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c {fmt_money(default_amt)})",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="menu:main")]
                ]),
            )
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.message(AmountChangeStates.wait_amount, F.text)
async def amt_enter_amount(message: Message, state: FSMContext) -> None:
    """Handle amount input step."""
    clean = message.text.strip()
    if clean == "\u2014":
        await state.set_state(AmountChangeStates.wait_note)
        await state.update_data(payment_amount=None)
        await message.answer(
            "\u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u0437\u0430\u043c\u0435\u0442\u043a\u0443 (\u0438\u043b\u0438 Enter \u2014 \u043f\u0443\u0441\u0442\u043e):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="menu:main")]
            ]),
        )
        return

    try:
        amount = int(clean)
    except ValueError:
        await message.answer("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0446\u0435\u043b\u043e\u0435 \u0447\u0438\u0441\u043b\u043e:")
        return
    if amount <= 0:
        await message.answer("\u0421\u0443\u043c\u043c\u0430 \u0434\u043e\u043b\u0436\u043d\u0430 \u0431\u044b\u0442\u044c \u0431\u043e\u043b\u044c\u0448\u0435 \u043d\u0443\u043b\u044f:")
        return

    await state.update_data(payment_amount=amount)
    await state.set_state(AmountChangeStates.wait_note)
    await message.answer(
        "\u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u0437\u0430\u043c\u0435\u0442\u043a\u0443 (\u0438\u043b\u0438 Enter \u2014 \u043f\u0443\u0441\u0442\u043e):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="menu:main")]
        ]),
    )


@router.message(AmountChangeStates.wait_note, F.text)
async def amt_enter_note(message: Message, state: FSMContext) -> None:
    """Handle note input and complete payment."""
    text_input = message.text.strip().lower()
    data = await state.get_data()
    target_type = data.get("target_type")
    target_id = data.get("target_id")
    payment_amount = data.get("payment_amount")

    if text_input == "cancel":
        await state.clear()
        await message.answer(
            "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{_MENU}", callback_data="menu:main")]
            ]),
        )
        return

    notes_val = message.text.strip() or None

    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            return

        try:
            _, created = await mark_paid_once(
                session,
                target_type,
                target_id,
                amount_rub=data.get("default_amount", 0),
                paid_date=await today(session),
                notes=notes_val,
                paid_by=user.id,
                owner_id=user.id,
                payment_amount=payment_amount,
            )
        except (ValueError, NotOwnerError):
            await message.answer("\u041f\u043e\u0437\u0438\u0446\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")
            await state.clear()
            return

        if not created:
            await message.answer("\u041f\u043b\u0430\u0442\u0435\u0436 \u0443\u0436\u0435 \u043e\u0442\u043c\u0435\u0447\u0435\u043d")
            await state.clear()
            return

        await session.commit()

    await state.clear()
    await message.answer(
        f"{_PAY_OK} \u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e {_CHECK} \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u043e\u043f\u043b\u0430\u0442\u0430."
    )


async def _get_target_local(session, target_type, target_id):
    """Get target without owner validation (internal use)."""
    model = Server if target_type == "server" else Subscription
    entity = await session.get(model, target_id)
    if entity is None:
        raise ValueError(f"{target_type} {target_id} not found")
    return entity


async def _edit_msg(callback, text, keyboard):
    """Helper to edit callback message, swallowing errors."""
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass


# ============================================================
# Feature 3: /export
# ============================================================


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    """Export user data as a CSV file."""
    async with session_factory() as session:
        user = await _owner(session, message.chat.id)
        if user is None:
            await session.commit()
            return

        servers = (
            await session.scalars(
                select(Server).where(Server.owner_id == user.id).order_by(Server.id)
            )
        ).all()
        subscriptions = (
            await session.scalars(
                select(Subscription)
                .where(Subscription.owner_id == user.id)
                .order_by(Subscription.id)
            )
        ).all()
        expenses = (
            await session.scalars(
                select(OneTimeExpense)
                .where(OneTimeExpense.owner_id == user.id)
                .order_by(OneTimeExpense.id)
            )
        ).all()
        payments = await owned_payments(session, user.id)

        username = user.username or "user"
        export_date = (await today(session)).strftime("%d.%m.%Y")

        buf_lines = []
        buf_lines.append(f"# Remember {_E} \u044d\u043a\u0441\u043f\u043e\u0440\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f {username}")
        buf_lines.append(f"# \u0414\u0430\u0442\u0430 \u044d\u043a\u0441\u043f\u043e\u0440\u0442\u0430: {export_date}")
        buf_lines.append("")

        # Servers
        buf_lines.append("\u0421\u0415\u0420\u0412\u0415\u0420\u042b")
        buf_lines.append("ID | Название | Тип | Провайдер | IP | Сумма | Период | До | Активен")
        for s in servers:
            active = "Yes" if s.is_active else "No"
            ip = s.ip or _E
            buf_lines.append(
                f"{s.id} | {s.name} | {s.kind} | {s.provider} | {ip} | "
                f"{fmt_money(s.amount_rub)} | {s.period} | {_fmt_next_due(s.next_due)} | {active}"
            )
        buf_lines.append("")

        # Subscriptions
        buf_lines.append("\u041f\u041e\u0414\u041f\u0418\u0421\u041a\u0418")
        buf_lines.append("ID | Название | Провайдер | Сумма | Период | До | Статус")
        for sub in subscriptions:
            buf_lines.append(
                f"{sub.id} | {sub.name} | {sub.provider} | {fmt_money(sub.amount_rub)} | "
                f"{sub.period} | {_fmt_next_due(sub.next_due)} | {sub.status}"
            )
        buf_lines.append("")

        # Expenses
        buf_lines.append("\u0420\u0410\u0421\u0425\u041e\u0414\u042b")
        buf_lines.append("ID | Название | Сумма | Категория | Дата")
        for exp in expenses:
            cat = exp.category or _E
            buf_lines.append(
                f"{exp.id} | {exp.title} | {fmt_money(exp.amount_rub)} | "
                f"{cat} | {exp.paid_date:%d.%m.%Y}"
            )
        buf_lines.append("")

        # Payments
        buf_lines.append("\u041f\u041b\u0410\u0422\u0415\u0416\u0418")
        buf_lines.append("Дата | Что | Сумма | Кто отметил | Период")

        users_list = (await session.scalars(select(User))).all()
        user_map = {u.id: u.username for u in users_list}

        srv_names = {s.id: s.name for s in servers}
        sub_names = {s.id: s.name for s in subscriptions}

        for p in payments:
            name = srv_names.get(p.target_id, sub_names.get(p.target_id, _E))
            by_user = user_map.get(p.paid_by) if p.paid_by else _E
            period_start = p.due_date.strftime("%d.%m")
            buf_lines.append(
                f"{p.paid_date:%d.%m} | {name} | {fmt_money(p.amount_rub)} | "
                f"{by_user} | {period_start}"
            )

        csv_content = "\n".join(buf_lines)
        filename = f"remember_export_{(await today(session)).isoformat()}.txt"
        file_bytes = csv_content.encode("utf-8")

    from aiogram.types import BufferedInputFile
    doc = BufferedInputFile(file_bytes, filename=filename)
    await message.answer_document(
        doc,
        caption="Данные Remember (экспорт)",
    )
