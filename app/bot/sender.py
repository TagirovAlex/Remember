"""Digest broadcasting for the scheduler (aiogram Bot is passed in by the caller).

Digests are built per user and go only to chats with a confirmed binding
(``BotSubscriber.user_id`` set and pointing at an active user with
``telegram_id_confirmed``).
"""

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import today
from app.bot.keyboards import digest_pay_keyboard, pay_keyboard
from app.db import SessionLocal
from app.models import BotSubscriber, User
from app.services.billing import collect_digest
from app.services.notify import fmt_money, render_digest
from app.services.settings import remind_soon_days


async def _bound_subscribers(session: AsyncSession) -> list[BotSubscriber]:
    return list(
        (
            await session.scalars(
                select(BotSubscriber)
                .join(User, BotSubscriber.user_id == User.id)
                .where(
                    BotSubscriber.is_active.is_(True),
                    User.is_active.is_(True),
                    User.telegram_id_confirmed.is_(True),
                )
            )
        ).all()
    )


def _overdue_text(digest: dict) -> str | None:
    if not digest["overdue"]:
        return None
    lines = ["⚠️ Просрочено"]
    for entry in digest["overdue"]:
        due = entry["next_due"].strftime("%d.%m.%Y")
        lines.append(f"• {entry['name']} — {fmt_money(entry['amount_rub'])} — {due}")
    return "\n".join(lines)


async def _send_to_subscribers(
    bot: Bot, subscribers: list[BotSubscriber], text: str, keyboard
) -> int:
    sent = 0
    for subscriber in subscribers:
        try:
            await bot.send_message(
                chat_id=subscriber.chat_id, text=text, reply_markup=keyboard
            )
            sent += 1
        except TelegramAPIError:
            continue
    return sent


async def send_digest_to_subscribers(
    bot: Bot, factory: async_sessionmaker[AsyncSession] | None = None
) -> int:
    """Send the full morning digest per user to bound chats; return recipients count."""
    factory = factory or SessionLocal
    sent = 0
    async with factory() as session:
        subscribers = await _bound_subscribers(session)
        soon_days = await remind_soon_days(session)
        today_value = await today(session)
        by_user: dict[int, list[BotSubscriber]] = {}
        for subscriber in subscribers:
            by_user.setdefault(subscriber.user_id, []).append(subscriber)
        for user_id, chats in by_user.items():
            digest = await collect_digest(
                session, today_value, soon_days, owner_id=user_id
            )
            text = render_digest(digest, soon_days=soon_days)
            keyboard = digest_pay_keyboard(digest)
            sent += await _send_to_subscribers(bot, chats, text, keyboard)
    return sent


async def send_evening_reminder_to_subscribers(
    bot: Bot, factory: async_sessionmaker[AsyncSession] | None = None
) -> int:
    """Send the overdue reminder per user; chats without overdues get nothing."""
    factory = factory or SessionLocal
    sent = 0
    async with factory() as session:
        subscribers = await _bound_subscribers(session)
        soon_days = await remind_soon_days(session)
        today_value = await today(session)
        by_user: dict[int, list[BotSubscriber]] = {}
        for subscriber in subscribers:
            by_user.setdefault(subscriber.user_id, []).append(subscriber)
        for user_id, chats in by_user.items():
            digest = await collect_digest(
                session, today_value, soon_days, owner_id=user_id
            )
            text = _overdue_text(digest)
            if text is None:
                continue
            keyboard = pay_keyboard(digest["overdue"])
            sent += await _send_to_subscribers(bot, chats, text, keyboard)
    return sent