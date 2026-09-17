from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import Server, Subscription
from app.services.billing import mark_paid_once

router = APIRouter(tags=["api"])

_TARGET_TYPES = ("server", "subscription")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class MarkPaidPayload(BaseModel):
    target_type: str
    target_id: int
    amount_rub: int
    paid_date: date
    notes: str | None = None

    @field_validator("amount_rub")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_rub must be positive")
        return v


async def _get_target(session: AsyncSession, target_type: str, target_id: int):
    model = Server if target_type == "server" else Subscription
    return await session.get(model, target_id)


def _payment_dict(payment) -> dict:
    return {
        "id": payment.id,
        "target_type": payment.target_type,
        "target_id": payment.target_id,
        "amount_rub": payment.amount_rub,
        "due_date": payment.due_date.isoformat(),
        "paid_date": payment.paid_date.isoformat(),
        "paid_by": payment.paid_by,
        "notes": payment.notes,
    }


@router.post("/api/payments/mark", dependencies=[Depends(get_current_user)])
async def mark_payment(
    payload: MarkPaidPayload, request: Request, session: SessionDep
) -> dict:
    """Mark a due payment as paid and roll the schedule over (daily idempotent)."""
    if payload.target_type not in _TARGET_TYPES:
        raise HTTPException(status_code=400, detail="bad target_type")
    target = await _get_target(session, payload.target_type, payload.target_id)
    if target is None or target.owner_id != request.state.user.id:
        raise HTTPException(status_code=404, detail="entity not found")
    payment, created = await mark_paid_once(
        session,
        payload.target_type,
        payload.target_id,
        payload.amount_rub,
        payload.paid_date,
        payload.notes,
        request.state.user.id,
        owner_id=request.state.user.id,
    )
    await session.commit()
    await session.refresh(target)
    return {
        "status": "ok",
        "already": not created,
        "payment": _payment_dict(payment),
        "new_next_due": target.next_due.isoformat(),
    }