import calendar
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, Server, Subscription

_PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


class NotOwnerError(Exception):
    """Raised when an operation targets an entity owned by another user."""


def _period_months(period: str) -> int:
    try:
        return _PERIOD_MONTHS[period]
    except KeyError as exc:
        raise ValueError(f"unknown period: {period}") from exc


def add_period(d: date, period: str) -> date:
    """Shift a date by one month/quarter/year, clamping the day to the month length."""
    months = _period_months(period)
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def advance_next_due(next_due: date, period: str, paid_date: date | None = None) -> date:
    """Advance next_due from the original date so the schedule never drifts."""
    return add_period(next_due, period)


async def _get_target(
    session: AsyncSession, target_type: str, target_id: int
) -> Server | Subscription:
    if target_type == "server":
        target = await session.get(Server, target_id)
    elif target_type == "subscription":
        target = await session.get(Subscription, target_id)
    else:
        raise ValueError(f"unknown target_type: {target_type}")
    if target is None:
        raise ValueError(f"{target_type} {target_id} not found")
    return target


async def mark_paid(
    session: AsyncSession,
    target_type: str,
    target_id: int,
    amount_rub: int,
    paid_date: date,
    notes: str | None = None,
    paid_by: int | None = None,
    *,
    owner_id: int,
) -> Payment:
    """Record a payment for the current due period and roll next_due over.

    Idempotent: if a Payment already exists for (target_type, target_id, due_date)
    it is returned as-is and the schedule is not advanced twice.
    Raises NotOwnerError when the target belongs to another user.
    """
    target = await _get_target(session, target_type, target_id)
    if target.owner_id != owner_id:
        raise NotOwnerError(f"{target_type} {target_id} is not owned by user {owner_id}")
    existing = await session.scalar(
        select(Payment).where(
            Payment.target_type == target_type,
            Payment.target_id == target_id,
            Payment.due_date == target.next_due,
        )
    )
    if existing is not None:
        return existing
    payment = Payment(
        target_type=target_type,
        target_id=target_id,
        amount_rub=amount_rub,
        due_date=target.next_due,
        paid_date=paid_date,
        notes=notes,
        paid_by=paid_by,
    )
    session.add(payment)
    target.next_due = advance_next_due(target.next_due, target.period, paid_date)
    return payment


async def mark_paid_once(
    session: AsyncSession,
    target_type: str,
    target_id: int,
    amount_rub: int,
    paid_date: date,
    notes: str | None = None,
    paid_by: int | None = None,
    *,
    owner_id: int,
) -> tuple[Payment, bool]:
    """Mark a payment once per day, returning (payment, created).

    When a Payment already exists for (target_type, target_id, paid_date) the
    existing row is returned as-is with created=False and next_due is not moved.
    Otherwise mark_paid() is called and (payment, True) is returned.
    """
    existing = await session.scalar(
        select(Payment).where(
            Payment.target_type == target_type,
            Payment.target_id == target_id,
            Payment.paid_date == paid_date,
        )
    )
    if existing is not None:
        return existing, False
    payment = await mark_paid(
        session, target_type, target_id, amount_rub, paid_date, notes, paid_by,
        owner_id=owner_id,
    )
    return payment, True


def _entry(target: Server | Subscription, target_type: str) -> dict:
    return {
        "name": target.name,
        "kind": target_type,
        "amount_rub": target.amount_rub,
        "next_due": target.next_due,
        "target_type": target_type,
        "target_id": target.id,
    }


def _bucket(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: e["next_due"])


async def collect_digest(
    session: AsyncSession, today: date, soon_days: int, *, owner_id: int
) -> dict:
    """Collect payments due today, within soon_days, and overdue for one owner."""
    servers = (
        await session.scalars(
            select(Server).where(
                Server.is_active.is_(True), Server.owner_id == owner_id
            )
        )
    ).all()
    subscriptions = (
        await session.scalars(
            select(Subscription).where(
                Subscription.status == "active", Subscription.owner_id == owner_id
            )
        )
    ).all()

    horizon = today + timedelta(days=soon_days)
    due_today: list[dict] = []
    due_soon: list[dict] = []
    overdue: list[dict] = []
    total_today = total_soon = total_overdue = 0

    items = [
        *((s, "server") for s in servers),
        *((s, "subscription") for s in subscriptions),
    ]
    for target, target_type in items:
        entry = _entry(target, target_type)
        if target.next_due < today:
            overdue.append(entry)
            total_overdue += target.amount_rub
        elif target.next_due == today:
            due_today.append(entry)
            total_today += target.amount_rub
        elif target.next_due <= horizon:
            due_soon.append(entry)
            total_soon += target.amount_rub

    return {
        "due_today": _bucket(due_today),
        "due_soon": _bucket(due_soon),
        "overdue": _bucket(overdue),
        "total_today": total_today,
        "total_soon": total_soon,
        "total_overdue": total_overdue,
    }