from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """Application user with role and theme preferences."""

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_telegram_id", "telegram_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(260))
    role: Mapped[str] = mapped_column(String(20), default="user")
    theme: Mapped[str] = mapped_column(String(10), default="light")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_id_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Server(Base):
    """Rented server (GPU/VM/dedicated) with its billing schedule.

    ``owner_id`` is NOT NULL in the ORM but may stay physically nullable on
    databases migrated from v1 (migrate_schema adds it without NOT NULL);
    the application always sets the owner explicitly.
    """

    __tablename__ = "servers"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_servers_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    specs: Mapped[str | None] = mapped_column(Text, nullable=True)
    what_runs: Mapped[str | None] = mapped_column(Text, nullable=True)
    panel_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_rub: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(20))
    next_due: Mapped[date] = mapped_column(Date)
    billing_anchor: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Subscription(Base):
    """Recurring service subscription.

    ``owner_id`` is NOT NULL in the ORM; see Server for the migrated-schema note.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_subscriptions_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    amount_rub: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(20))
    next_due: Mapped[date] = mapped_column(Date)
    billing_anchor: Mapped[date | None] = mapped_column(Date, nullable=True)
    auto_renewal: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OneTimeExpense(Base):
    """One-off expense not tied to a recurring schedule."""

    __tablename__ = "one_time_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    amount_rub: Mapped[int] = mapped_column(Integer)
    paid_date: Mapped[date] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Payment(Base):
    """Recorded payment covering a due period (idempotent per target+due)."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "due_date", name="uq_payment_target_due"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[int] = mapped_column(Integer)
    amount_rub: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    paid_date: Mapped[date] = mapped_column(Date)
    paid_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Setting(Base):
    """Application setting stored in the database."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BotSubscriber(Base):
    """Telegram chat subscribed to digests.

    ``user_id`` caches the bound user; it is set on binding confirmation and in
    /start so digest senders can group chats per owner.
    """

    __tablename__ = "bot_subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)