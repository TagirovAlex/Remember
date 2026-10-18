from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Period = Literal["monthly", "quarterly", "yearly"]
Role = Literal["admin", "user"]
Theme = Literal["light", "dark"]


def _positive(v: int | None) -> int | None:
    if v is not None and v <= 0:
        raise ValueError("amount_rub must be positive")
    return v


class UserCreate(BaseModel):
    """Payload for creating a user account."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    role: Role = "user"
    is_active: bool = True


class UserUpdate(BaseModel):
    """Payload for editing a user (password omitted keeps the current one)."""

    role: Role = "user"
    is_active: bool = True
    password: str | None = Field(default=None, min_length=4, max_length=128)


def _positive_telegram_id(v: int | None) -> int | None:
    if v is not None and not (0 < v < 2**63):
        raise ValueError("telegram_id must be in (0, 2^63)")
    return v


class TelegramIdUpdate(BaseModel):
    """Payload for binding the user's Telegram account in /settings."""

    telegram_id: int | None = None

    _telegram_id = field_validator("telegram_id")(_positive_telegram_id)


class ServerBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["gpu", "vm", "dedicated"]
    provider: str = Field(min_length=1, max_length=100)
    location: str | None = None
    ip: str | None = None
    specs: str | None = None
    what_runs: str | None = None
    panel_url: str | None = None
    notes: str | None = None
    amount_rub: int
    period: Period
    next_due: date
    billing_anchor: date | None = None
    is_active: bool = True

    _amount = field_validator("amount_rub")(_positive)


class SubscriptionBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    amount_rub: int
    period: Period
    next_due: date
    billing_anchor: date | None = None
    auto_renewal: bool = True
    status: Literal["active", "paused", "archived"] = "active"
    url: str | None = None
    notes: str | None = None

    _amount = field_validator("amount_rub")(_positive)


class OneTimeExpenseBase(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    amount_rub: int
    paid_date: date
    category: str | None = None
    notes: str | None = None

    _amount = field_validator("amount_rub")(_positive)
