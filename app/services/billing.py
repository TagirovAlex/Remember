import calendar
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OneTimeExpense, Payment, Server, Subscription

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
    payment_amount: int | None = None,
) -> tuple[Payment, bool]:
    """Mark a payment once per day, returning (payment, created).

    When ``payment_amount`` is provided it overrides the stored amount for
    the Payment record only -- the entity's ``amount_rub`` (tariff rate) stays
    untouched.  Pass ``None`` to use the entity default.

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
    effective_amount = amount_rub if payment_amount is None else payment_amount
    payment = await mark_paid(
        session, target_type, target_id, effective_amount, paid_date, notes, paid_by,
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


async def owned_payments(
    session: AsyncSession, owner_id: int, limit: int | None = None
) -> list[Payment]:
    """Payments whose target (server/subscription) belongs to ``owner_id``."""
    owned_server_ids = select(Server.id).where(Server.owner_id == owner_id)
    owned_sub_ids = select(Subscription.id).where(Subscription.owner_id == owner_id)
    statement = (
        select(Payment)
        .where(
            or_(
                and_(
                    Payment.target_type == "server",
                    Payment.target_id.in_(owned_server_ids),
                ),
                and_(
                    Payment.target_type == "subscription",
                    Payment.target_id.in_(owned_sub_ids),
                ),
            )
        )
        .order_by(Payment.paid_date.desc(), Payment.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list((await session.scalars(statement)).all())


def _month_bounds(month: date) -> tuple[date, date]:
    """First and last day of the month containing ``month``."""
    first = month.replace(day=1)
    last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    return first, last


async def _payments_in_range(
    session: AsyncSession, owner_id: int, first: date, last: date
) -> list[Payment]:
    """Payments of owned targets recorded within an inclusive date range."""
    owned_server_ids = select(Server.id).where(Server.owner_id == owner_id)
    owned_sub_ids = select(Subscription.id).where(Subscription.owner_id == owner_id)
    return list(
        (
            await session.scalars(
                select(Payment).where(
                    Payment.paid_date >= first,
                    Payment.paid_date <= last,
                    or_(
                        and_(
                            Payment.target_type == "server",
                            Payment.target_id.in_(owned_server_ids),
                        ),
                        and_(
                            Payment.target_type == "subscription",
                            Payment.target_id.in_(owned_sub_ids),
                        ),
                    ),
                )
            )
        ).all()
    )


async def _expenses_in_range(
    session: AsyncSession, owner_id: int, first: date, last: date
) -> list[OneTimeExpense]:
    """One-off expenses of an owner recorded within an inclusive date range."""
    return list(
        (
            await session.scalars(
                select(OneTimeExpense).where(
                    OneTimeExpense.owner_id == owner_id,
                    OneTimeExpense.paid_date >= first,
                    OneTimeExpense.paid_date <= last,
                )
            )
        ).all()
    )


async def monthly_summary(
    session: AsyncSession,
    owner_id: int,
    ref_month: date,
    today: date,
) -> dict:
    """Aggregate one month's finances for an owner (used by the /stats command).

    Returns paid totals (recurring payments + one-off expenses), the top
    expense categories, the previous month's total, and the amount still due
    from ``today`` to the end of ``ref_month``.
    """
    first, last = _month_bounds(ref_month)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    prev_last = _month_bounds(prev_first)[1]

    payments = await _payments_in_range(session, owner_id, first, last)
    expenses = await _expenses_in_range(session, owner_id, first, last)
    prev_payments = await _payments_in_range(session, owner_id, prev_first, prev_last)
    prev_expenses = await _expenses_in_range(session, owner_id, prev_first, prev_last)

    recurring_sum = sum(payment.amount_rub for payment in payments)
    one_time_sum = sum(expense.amount_rub for expense in expenses)
    prev_total = (
        sum(payment.amount_rub for payment in prev_payments)
        + sum(expense.amount_rub for expense in prev_expenses)
    )

    categories: dict[str, int] = {}
    for expense in expenses:
        category = expense.category or "Без категории"
        categories[category] = categories.get(category, 0) + expense.amount_rub
    top = sorted(categories.items(), key=lambda item: (-item[1], item[0]))[:3]
    top_categories = [{"category": category, "sum": total} for category, total in top]

    due_remaining_count = 0
    due_remaining_sum = 0
    if today <= last:
        servers = (
            await session.scalars(
                select(Server).where(
                    Server.is_active.is_(True),
                    Server.owner_id == owner_id,
                    Server.next_due >= today,
                    Server.next_due <= last,
                )
            )
        ).all()
        subscriptions = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status == "active",
                    Subscription.owner_id == owner_id,
                    Subscription.next_due >= today,
                    Subscription.next_due <= last,
                )
            )
        ).all()
        due_remaining_count = len(servers) + len(subscriptions)
        due_remaining_sum = sum(server.amount_rub for server in servers) + sum(
            subscription.amount_rub for subscription in subscriptions
        )

    return {
        "month": first,
        "total_paid": recurring_sum + one_time_sum,
        "recurring_count": len(payments),
        "recurring_sum": recurring_sum,
        "one_time_count": len(expenses),
        "one_time_sum": one_time_sum,
        "top_categories": top_categories,
        "prev_month_total": prev_total,
        "due_remaining_count": due_remaining_count,
        "due_remaining_sum": due_remaining_sum,
    }


async def search_owned(
    session: AsyncSession, owner_id: int, query: str, limit: int = 15
) -> list[dict]:
    """Search an owner's servers, subscriptions and expenses by substring.

    Matches ``name``/``provider`` on servers and subscriptions and
    ``title``/``category`` on one-off expenses; returns up to ``limit``
    rows of {"target_type", "name", "amount_rub", "due"}.
    """
    pattern = f"%{query}%"
    servers = (
        await session.scalars(
            select(Server)
            .where(
                Server.owner_id == owner_id,
                or_(Server.name.ilike(pattern), Server.provider.ilike(pattern)),
            )
            .limit(limit)
        )
    ).all()
    subscriptions = (
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.owner_id == owner_id,
                or_(
                    Subscription.name.ilike(pattern),
                    Subscription.provider.ilike(pattern),
                ),
            )
            .limit(limit)
        )
    ).all()
    expenses = (
        await session.scalars(
            select(OneTimeExpense)
            .where(
                OneTimeExpense.owner_id == owner_id,
                or_(
                    OneTimeExpense.title.ilike(pattern),
                    OneTimeExpense.category.ilike(pattern),
                ),
            )
            .limit(limit)
        )
    ).all()

    results: list[dict] = [
        {
            "target_type": "server",
            "name": server.name,
            "amount_rub": server.amount_rub,
            "due": server.next_due,
        }
        for server in servers
    ]
    results.extend(
        {
            "target_type": "subscription",
            "name": subscription.name,
            "amount_rub": subscription.amount_rub,
            "due": subscription.next_due,
        }
        for subscription in subscriptions
    )
    results.extend(
        {
            "target_type": "expense",
            "name": expense.title,
            "amount_rub": expense.amount_rub,
            "due": expense.paid_date,
        }
        for expense in expenses
    )
    return results[:limit]