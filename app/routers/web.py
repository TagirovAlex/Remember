import logging
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    SESSION_COOKIE,
    authenticate_user,
    create_session_cookie,
    get_current_user,
    get_session_secret,
    require_admin,
    session_ttl_seconds,
)
from app.bot.manager import bot_manager
from app.config import settings
from app.db import get_session
from app.models import BotSubscriber, OneTimeExpense, Payment, Server, Subscription, User
from app.ratelimit import rate_limiter
from app.schemas import (
    OneTimeExpenseBase,
    ServerBase,
    SubscriptionBase,
    TelegramIdUpdate,
    UserCreate,
    UserUpdate,
)
from app.security import hash_password, verify_password
from app.services import settings as settings_svc
from app.services.backup import create_backup, list_backups, resolve_backup
from app.services.billing import collect_digest, mark_paid_once, owned_payments
from app.services.images import delete_image, image_path, image_url, save_image
from app.services.notify import fmt_money

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

logger = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _set_session_cookie(
    response: RedirectResponse, request: Request, token: str, max_age: int
) -> None:
    """Attach the signed session cookie to a response."""
    secure = (
        request.headers.get("x-forwarded-proto", "") == "https"
        or request.url.scheme == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
    )

KIND_LABELS = {"gpu": "GPU", "vm": "VM", "dedicated": "Dedicated"}
PERIOD_LABELS = {"monthly": "Ежемесячно", "quarterly": "Раз в квартал", "yearly": "Раз в год"}
STATUS_LABELS = {"active": "Активна", "paused": "На паузе", "archived": "Архив"}

SERVER_COLUMNS = [
    {"label": "Картинка", "key": "image"},
    {"label": "Название", "key": "name"},
    {"label": "Тип", "key": "kind"},
    {"label": "Провайдер", "key": "provider"},
    {"label": "Локация", "key": "location"},
    {"label": "Сумма", "key": "amount"},
    {"label": "Период", "key": "period"},
    {"label": "Оплата до", "key": "next_due"},
    {"label": "Активен", "key": "is_active"},
]
SUBSCRIPTION_COLUMNS = [
    {"label": "Картинка", "key": "image"},
    {"label": "Название", "key": "name"},
    {"label": "Провайдер", "key": "provider"},
    {"label": "Сумма", "key": "amount"},
    {"label": "Период", "key": "period"},
    {"label": "Оплата до", "key": "next_due"},
    {"label": "Статус", "key": "status"},
    {"label": "Автопродление", "key": "auto_renewal"},
]
EXPENSE_COLUMNS = [
    {"label": "Картинка", "key": "image"},
    {"label": "Название", "key": "name"},
    {"label": "Сумма", "key": "amount"},
    {"label": "Дата", "key": "paid_date"},
    {"label": "Категория", "key": "category"},
    {"label": "Заметки", "key": "notes"},
]

_ENTITY_MODELS = {
    "server": Server,
    "subscription": Subscription,
    "expense": OneTimeExpense,
}

def _bool_mark(value: bool) -> str:
    return "✓" if value else "—"


def _field_or_empty(owner: object, attr: str) -> str:
    """Return ``owner.attr`` as a string, or an empty string for None."""
    if owner is None:
        return ""
    return getattr(owner, attr, None) or ""


def _get_zone(name: str | None) -> ZoneInfo:
    for candidate in (name or settings.tz, settings.tz, "UTC"):
        try:
            return ZoneInfo(candidate)
        except (KeyError, TypeError, ValueError):
            continue
    return ZoneInfo("UTC")


async def _today(session: AsyncSession) -> date:
    """Today's date in the configured time zone."""
    tzname = await settings_svc.tz(session)
    zone = _get_zone(tzname)
    return datetime.now(zone).date()


def _opt_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _server_row(server: Server) -> dict:
    return {
        "id": server.id,
        "target_type": "server",
        "name": server.name,
        "kind": KIND_LABELS.get(server.kind, server.kind),
        "provider": server.provider,
        "location": server.location or "—",
        "amount": fmt_money(server.amount_rub),
        "amount_rub": server.amount_rub,
        "period": PERIOD_LABELS.get(server.period, server.period),
        "next_due": server.next_due.isoformat(),
        "is_active": _bool_mark(server.is_active),
        "image": image_url("server", server.id) if server.image else None,
        "pay": server.is_active,
    }


def _sub_row(sub: Subscription) -> dict:
    return {
        "id": sub.id,
        "target_type": "subscription",
        "name": sub.name,
        "provider": sub.provider,
        "amount": fmt_money(sub.amount_rub),
        "amount_rub": sub.amount_rub,
        "period": PERIOD_LABELS.get(sub.period, sub.period),
        "next_due": sub.next_due.isoformat(),
        "status": STATUS_LABELS.get(sub.status, sub.status),
        "auto_renewal": _bool_mark(sub.auto_renewal),
        "image": image_url("subscription", sub.id) if sub.image else None,
        "pay": sub.status == "active",
    }


def _expense_row(expense: OneTimeExpense) -> dict:
    return {
        "id": expense.id,
        "name": expense.title,
        "amount": fmt_money(expense.amount_rub),
        "paid_date": expense.paid_date.isoformat(),
        "category": expense.category or "—",
        "notes": expense.notes or "—",
        "image": image_url("expense", expense.id) if expense.image else None,
    }


def _payment_row(payment: Payment, names: dict[str, str], user_names: dict[int, str]) -> dict:
    type_label = "Сервер" if payment.target_type == "server" else "Подписка"
    name = names.get(payment.target_type, {}).get(payment.target_id, "—")
    return {
        "type": type_label,
        "name": name,
        "amount": fmt_money(payment.amount_rub),
        "due_date": payment.due_date.isoformat(),
        "paid_date": payment.paid_date.isoformat(),
        "who": user_names.get(payment.paid_by, "—") if payment.paid_by else "—",
        "notes": payment.notes or "—",
    }


def _redirect_back(next_url: str | None, default: str, msg: str) -> RedirectResponse:
    target = next_url or default
    if not target.startswith("/"):
        target = default
    return RedirectResponse(f"{target}?msg={quote(msg)}", status_code=303)


def _entity_image(name: str, entity_type: str, entity_id: int) -> str | None:
    return image_url(entity_type, entity_id) if name else None


@router.get(
    "/images/{entity_type}/{entity_id}", dependencies=[Depends(get_current_user)]
)
async def entity_image(
    request: Request, entity_type: str, entity_id: int, session: SessionDep
):
    """Serve the stored image of an owned entity; 404 otherwise."""
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=404)
    entity = await session.get(model, entity_id)
    if entity is None or entity.owner_id != request.state.user.id or not entity.image:
        raise HTTPException(status_code=404)
    path = image_path(entity.image)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/webp", headers={"Cache-Control": "max-age=3600"}
    )


def _server_fields(server: Server | None) -> list[dict]:
    return [
        {"name": "name", "label": "Название", "value": server.name if server else "", "type": "text", "required": True},
        {
            "name": "kind",
            "label": "Тип",
            "value": server.kind if server else "gpu",
            "type": "select",
            "options": [("gpu", "GPU"), ("vm", "VM"), ("dedicated", "Dedicated")],
        },
        {"name": "provider", "label": "Провайдер", "value": server.provider if server else "", "type": "text", "required": True},
        {"name": "location", "label": "Локация", "value": _field_or_empty(server, "location"), "type": "text"},
        {"name": "ip", "label": "IP-адрес", "value": _field_or_empty(server, "ip"), "type": "text"},
        {"name": "specs", "label": "Спецификация", "value": _field_or_empty(server, "specs"), "type": "textarea"},
        {"name": "what_runs", "label": "Что развёрнуто", "value": _field_or_empty(server, "what_runs"), "type": "textarea"},
        {"name": "panel_url", "label": "Панель управления", "value": _field_or_empty(server, "panel_url"), "type": "text"},
        {"name": "notes", "label": "Заметки", "value": _field_or_empty(server, "notes"), "type": "textarea"},
        {"name": "amount_rub", "label": "Сумма, ₽", "value": server.amount_rub if server else "", "type": "number", "required": True},
        {
            "name": "period",
            "label": "Период",
            "value": server.period if server else "monthly",
            "type": "select",
            "options": [("monthly", "Ежемесячно"), ("quarterly", "Раз в квартал"), ("yearly", "Раз в год")],
        },
        {"name": "next_due", "label": "Следующая оплата", "value": server.next_due.isoformat() if server else "", "type": "date", "required": True},
        {"name": "billing_anchor", "label": "Точка отсчёта", "value": server.billing_anchor.isoformat() if server and server.billing_anchor else "", "type": "date"},
        {"name": "is_active", "label": "Сервер включён", "value": server.is_active if server else True, "type": "checkbox"},
    ]


def _sub_fields(sub: Subscription | None) -> list[dict]:
    return [
        {"name": "name", "label": "Название", "value": sub.name if sub else "", "type": "text", "required": True},
        {"name": "provider", "label": "Провайдер", "value": sub.provider if sub else "", "type": "text", "required": True},
        {"name": "amount_rub", "label": "Сумма, ₽", "value": sub.amount_rub if sub else "", "type": "number", "required": True},
        {
            "name": "period",
            "label": "Период",
            "value": sub.period if sub else "monthly",
            "type": "select",
            "options": [("monthly", "Ежемесячно"), ("quarterly", "Раз в квартал"), ("yearly", "Раз в год")],
        },
        {"name": "next_due", "label": "Следующая оплата", "value": sub.next_due.isoformat() if sub else "", "type": "date", "required": True},
        {"name": "billing_anchor", "label": "Точка отсчёта", "value": sub.billing_anchor.isoformat() if sub and sub.billing_anchor else "", "type": "date"},
        {"name": "auto_renewal", "label": "Автопродление", "value": sub.auto_renewal if sub else True, "type": "checkbox"},
        {
            "name": "status",
            "label": "Статус",
            "value": sub.status if sub else "active",
            "type": "select",
            "options": [("active", "Активна"), ("paused", "На паузе"), ("archived", "Архив")],
        },
        {"name": "url", "label": "Ссылка", "value": _field_or_empty(sub, "url"), "type": "text"},
        {"name": "notes", "label": "Заметки", "value": _field_or_empty(sub, "notes"), "type": "textarea"},
    ]


def _expense_fields(expense: OneTimeExpense | None) -> list[dict]:
    return [
        {"name": "title", "label": "Название", "value": expense.title if expense else "", "type": "text", "required": True},
        {"name": "amount_rub", "label": "Сумма, ₽", "value": expense.amount_rub if expense else "", "type": "number", "required": True},
        {"name": "paid_date", "label": "Дата оплаты", "value": expense.paid_date.isoformat() if expense else "", "type": "date", "required": True},
        {"name": "category", "label": "Категория", "value": _field_or_empty(expense, "category"), "type": "text"},
        {"name": "notes", "label": "Заметки", "value": _field_or_empty(expense, "notes"), "type": "textarea"},
    ]


def _parse_server(form: dict[str, str]) -> ServerBase:
    return ServerBase(
        name=form.get("name", "").strip(),
        kind=form.get("kind", ""),
        provider=form.get("provider", "").strip(),
        location=form.get("location") or None,
        ip=form.get("ip") or None,
        specs=form.get("specs") or None,
        what_runs=form.get("what_runs") or None,
        panel_url=form.get("panel_url") or None,
        notes=form.get("notes") or None,
        amount_rub=int(form.get("amount_rub") or 0),
        period=form.get("period", ""),
        next_due=date.fromisoformat(form.get("next_due", "")),
        billing_anchor=_opt_date(form.get("billing_anchor")),
        is_active=form.get("is_active") == "on",
    )


def _parse_sub(form: dict[str, str]) -> SubscriptionBase:
    return SubscriptionBase(
        name=form.get("name", "").strip(),
        provider=form.get("provider", "").strip(),
        amount_rub=int(form.get("amount_rub") or 0),
        period=form.get("period", ""),
        next_due=date.fromisoformat(form.get("next_due", "")),
        billing_anchor=_opt_date(form.get("billing_anchor")),
        auto_renewal=form.get("auto_renewal") == "on",
        status=form.get("status", "active"),
        url=form.get("url") or None,
        notes=form.get("notes") or None,
    )


def _parse_expense(form: dict[str, str]) -> OneTimeExpenseBase:
    return OneTimeExpenseBase(
        title=form.get("title", "").strip(),
        amount_rub=int(form.get("amount_rub") or 0),
        paid_date=date.fromisoformat(form.get("paid_date", "")),
        category=form.get("category") or None,
        notes=form.get("notes") or None,
    )


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
async def login_submit(request: Request, session: SessionDep):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    user = await authenticate_user(session, username, password)
    if user is None:
        return RedirectResponse(f"/login?error={quote('Неверный логин или пароль')}", status_code=303)
    secret = await get_session_secret(session)
    ttl = await session_ttl_seconds(session)
    token = create_session_cookie(secret, user.id, user.session_epoch, ttl)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, request, token, ttl)
    return response


@router.get("/register")
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
async def register_submit(request: Request, session: SessionDep):
    """Open self-registration; creates a user with the ``user`` role."""
    ip = request.client.host if request.client is not None else "unknown"
    if not rate_limiter.allow(ip):
        raise HTTPException(
            status_code=429, detail="Слишком много попыток. Попробуйте позже"
        )
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    if password != confirm:
        return RedirectResponse(
            f"/register?error={quote('Пароли не совпадают')}", status_code=303
        )
    try:
        data = UserCreate(username=username, password=password)
    except ValidationError as exc:
        return RedirectResponse(
            f"/register?error={quote(str(exc))}", status_code=303
        )
    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        role="user",
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return RedirectResponse(
            f"/register?error={quote('Пользователь с таким именем уже существует')}",
            status_code=303,
        )
    secret = await get_session_secret(session)
    ttl = await session_ttl_seconds(session)
    token = create_session_cookie(secret, user.id, user.session_epoch, ttl)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, request, token, ttl)
    return response


@router.get("/logout", dependencies=[Depends(get_current_user)])
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/", dependencies=[Depends(get_current_user)])
async def dashboard(request: Request, session: SessionDep):
    user = request.state.user
    today = await _today(session)
    soon_days = await settings_svc.remind_soon_days(session)
    digest = await collect_digest(session, today, soon_days, owner_id=user.id)
    servers = (
        await session.scalars(
            select(Server)
            .where(Server.is_active.is_(True), Server.owner_id == user.id)
            .order_by(Server.name)
        )
    ).all()
    subs = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.status == "active", Subscription.owner_id == user.id)
            .order_by(Subscription.name)
        )
    ).all()
    payments = await owned_payments(session, user.id, limit=10)
    names = await _target_names(session, user.id)
    user_names = await _user_names(session)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "today": today.isoformat(),
            "soon_days": soon_days,
            "digest": digest,
            "servers": [_server_row(s) for s in servers],
            "subscriptions": [_sub_row(s) for s in subs],
            "recent_payments": [_payment_row(p, names, user_names) for p in payments],
        },
    )


async def _target_names(
    session: AsyncSession, owner_id: int
) -> dict[str, dict[int, str]]:
    servers = (
        await session.scalars(select(Server).where(Server.owner_id == owner_id))
    ).all()
    subs = (
        await session.scalars(
            select(Subscription).where(Subscription.owner_id == owner_id)
        )
    ).all()
    return {
        "server": {s.id: s.name for s in servers},
        "subscription": {s.id: s.name for s in subs},
    }


async def _user_names(session: AsyncSession) -> dict[int, str]:
    users = (await session.scalars(select(User))).all()
    return {user.id: user.username for user in users}


@router.get("/servers", dependencies=[Depends(get_current_user)])
async def list_servers(request: Request, session: SessionDep):
    servers = (
        await session.scalars(
            select(Server)
            .where(Server.owner_id == request.state.user.id)
            .order_by(Server.name)
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "entity_list.html",
        {
            "title": "Серверы",
            "columns": SERVER_COLUMNS,
            "rows": [_server_row(s) for s in servers],
            "add_url": "/servers/new",
            "edit_url": "/servers",
            "next_url": "/servers",
        },
    )


@router.get("/servers/new", dependencies=[Depends(get_current_user)])
async def new_server(request: Request):
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": "Новый сервер",
            "action": "/servers",
            "fields": _server_fields(None),
            "image_url": None,
            "image_delete_url": None,
        },
    )


@router.get("/servers/{server_id}/edit", dependencies=[Depends(get_current_user)])
async def edit_server(
    request: Request, server_id: int, session: SessionDep
):
    server = await session.scalar(
        select(Server).where(
            Server.id == server_id, Server.owner_id == request.state.user.id
        )
    )
    if server is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": f"Сервер — {server.name}",
            "action": f"/servers/{server_id}",
            "fields": _server_fields(server),
            "image_url": _entity_image(server.image, "server", server.id),
            "image_delete_url": f"/servers/{server_id}/image/delete",
        },
    )


@router.post("/servers", dependencies=[Depends(get_current_user)])
async def create_server(request: Request, session: SessionDep):
    form = await request.form()
    try:
        data = _parse_server(dict(form))
        server = Server(**data.model_dump(), owner_id=request.state.user.id)
        session.add(server)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/servers/new?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            server.image = await save_image(image, "server", server.id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/servers/new?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/servers?msg={quote('Сервер добавлен')}", status_code=303)


@router.post("/servers/{server_id}", dependencies=[Depends(get_current_user)])
async def update_server(
    request: Request, server_id: int, session: SessionDep
):
    server = await session.scalar(
        select(Server).where(
            Server.id == server_id, Server.owner_id == request.state.user.id
        )
    )
    if server is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    try:
        data = _parse_server(dict(form))
        for key, value in data.model_dump().items():
            setattr(server, key, value)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/servers/{server_id}/edit?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            server.image = await save_image(image, "server", server_id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/servers/{server_id}/edit?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/servers?msg={quote('Сервер обновлён')}", status_code=303)


@router.post("/servers/{server_id}/delete", dependencies=[Depends(get_current_user)])
async def delete_server(
    request: Request, server_id: int, session: SessionDep
):
    server = await session.scalar(
        select(Server).where(
            Server.id == server_id, Server.owner_id == request.state.user.id
        )
    )
    if server is None:
        raise HTTPException(status_code=404)
    await session.execute(delete(Payment).where(Payment.target_type == "server", Payment.target_id == server_id))
    await session.delete(server)
    await session.commit()
    try:
        await delete_image("server", server_id)
    except ValueError:
        pass
    return RedirectResponse(f"/servers?msg={quote('Сервер удалён')}", status_code=303)


@router.post("/servers/{server_id}/pay", dependencies=[Depends(get_current_user)])
async def pay_server(
    request: Request, server_id: int, session: SessionDep
):
    server = await session.scalar(
        select(Server).where(
            Server.id == server_id, Server.owner_id == request.state.user.id
        )
    )
    if server is None or not server.is_active:
        raise HTTPException(status_code=404)
    form = await request.form()
    user = request.state.user
    _, created = await mark_paid_once(
        session, "server", server_id, server.amount_rub, await _today(session),
        paid_by=user.id, owner_id=user.id,
    )
    await session.commit()
    msg = "Оплачено ✓" if created else "уже отмечено сегодня"
    return _redirect_back(form.get("next"), "/servers", msg)


@router.post("/servers/{server_id}/image/delete", dependencies=[Depends(get_current_user)])
async def delete_server_image(
    request: Request, server_id: int, session: SessionDep
):
    """Remove the server's image and clear the column."""
    server = await session.scalar(
        select(Server).where(
            Server.id == server_id, Server.owner_id == request.state.user.id
        )
    )
    if server is None:
        raise HTTPException(status_code=404)
    try:
        await delete_image("server", server_id)
    except ValueError:
        pass
    server.image = None
    await session.commit()
    return RedirectResponse(f"/servers/{server_id}/edit?msg={quote('Картинка удалена')}", status_code=303)


@router.get("/subscriptions", dependencies=[Depends(get_current_user)])
async def list_subscriptions(request: Request, session: SessionDep):
    subs = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.owner_id == request.state.user.id)
            .order_by(Subscription.name)
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "entity_list.html",
        {
            "title": "Подписки",
            "columns": SUBSCRIPTION_COLUMNS,
            "rows": [_sub_row(s) for s in subs],
            "add_url": "/subscriptions/new",
            "edit_url": "/subscriptions",
            "next_url": "/subscriptions",
        },
    )


@router.get("/subscriptions/new", dependencies=[Depends(get_current_user)])
async def new_subscription(request: Request):
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": "Новая подписка",
            "action": "/subscriptions",
            "fields": _sub_fields(None),
            "image_url": None,
            "image_delete_url": None,
        },
    )


@router.get("/subscriptions/{sub_id}/edit", dependencies=[Depends(get_current_user)])
async def edit_subscription(
    request: Request, sub_id: int, session: SessionDep
):
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == sub_id, Subscription.owner_id == request.state.user.id
        )
    )
    if sub is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": f"Подписка — {sub.name}",
            "action": f"/subscriptions/{sub_id}",
            "fields": _sub_fields(sub),
            "image_url": _entity_image(sub.image, "subscription", sub.id),
            "image_delete_url": f"/subscriptions/{sub_id}/image/delete",
        },
    )


@router.post("/subscriptions", dependencies=[Depends(get_current_user)])
async def create_subscription(request: Request, session: SessionDep):
    form = await request.form()
    try:
        data = _parse_sub(dict(form))
        sub = Subscription(**data.model_dump(), owner_id=request.state.user.id)
        session.add(sub)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/subscriptions/new?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            sub.image = await save_image(image, "subscription", sub.id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/subscriptions/new?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/subscriptions?msg={quote('Подписка добавлена')}", status_code=303)


@router.post("/subscriptions/{sub_id}", dependencies=[Depends(get_current_user)])
async def update_subscription(
    request: Request, sub_id: int, session: SessionDep
):
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == sub_id, Subscription.owner_id == request.state.user.id
        )
    )
    if sub is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    try:
        data = _parse_sub(dict(form))
        for key, value in data.model_dump().items():
            setattr(sub, key, value)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/subscriptions/{sub_id}/edit?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            sub.image = await save_image(image, "subscription", sub_id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/subscriptions/{sub_id}/edit?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/subscriptions?msg={quote('Подписка обновлена')}", status_code=303)


@router.post("/subscriptions/{sub_id}/delete", dependencies=[Depends(get_current_user)])
async def delete_subscription(
    request: Request, sub_id: int, session: SessionDep
):
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == sub_id, Subscription.owner_id == request.state.user.id
        )
    )
    if sub is None:
        raise HTTPException(status_code=404)
    await session.execute(
        delete(Payment).where(
            Payment.target_type == "subscription", Payment.target_id == sub_id
        )
    )
    await session.delete(sub)
    await session.commit()
    try:
        await delete_image("subscription", sub_id)
    except ValueError:
        pass
    return RedirectResponse(f"/subscriptions?msg={quote('Подписка удалена')}", status_code=303)


@router.post("/subscriptions/{sub_id}/pay", dependencies=[Depends(get_current_user)])
async def pay_subscription(
    request: Request, sub_id: int, session: SessionDep
):
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == sub_id, Subscription.owner_id == request.state.user.id
        )
    )
    if sub is None or sub.status != "active":
        raise HTTPException(status_code=404)
    form = await request.form()
    user = request.state.user
    _, created = await mark_paid_once(
        session, "subscription", sub_id, sub.amount_rub, await _today(session),
        paid_by=user.id, owner_id=user.id,
    )
    await session.commit()
    msg = "Оплачено ✓" if created else "уже отмечено сегодня"
    return _redirect_back(form.get("next"), "/subscriptions", msg)


@router.post(
    "/subscriptions/{sub_id}/image/delete", dependencies=[Depends(get_current_user)]
)
async def delete_subscription_image(
    request: Request, sub_id: int, session: SessionDep
):
    """Remove the subscription's image and clear the column."""
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == sub_id, Subscription.owner_id == request.state.user.id
        )
    )
    if sub is None:
        raise HTTPException(status_code=404)
    try:
        await delete_image("subscription", sub_id)
    except ValueError:
        pass
    sub.image = None
    await session.commit()
    return RedirectResponse(
        f"/subscriptions/{sub_id}/edit?msg={quote('Картинка удалена')}", status_code=303
    )


@router.get("/expenses", dependencies=[Depends(get_current_user)])
async def list_expenses(request: Request, session: SessionDep):
    expenses = (
        await session.scalars(
            select(OneTimeExpense)
            .where(OneTimeExpense.owner_id == request.state.user.id)
            .order_by(OneTimeExpense.paid_date.desc())
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "entity_list.html",
        {
            "title": "Расходы",
            "columns": EXPENSE_COLUMNS,
            "rows": [_expense_row(e) for e in expenses],
            "add_url": "/expenses/new",
            "edit_url": "/expenses",
            "next_url": "/expenses",
        },
    )


@router.get("/expenses/new", dependencies=[Depends(get_current_user)])
async def new_expense(request: Request):
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": "Новый расход",
            "action": "/expenses",
            "fields": _expense_fields(None),
            "image_url": None,
            "image_delete_url": None,
        },
    )


@router.get("/expenses/{expense_id}/edit", dependencies=[Depends(get_current_user)])
async def edit_expense(
    request: Request, expense_id: int, session: SessionDep
):
    expense = await session.scalar(
        select(OneTimeExpense).where(
            OneTimeExpense.id == expense_id,
            OneTimeExpense.owner_id == request.state.user.id,
        )
    )
    if expense is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "entity_form.html",
        {
            "title": f"Расход — {expense.title}",
            "action": f"/expenses/{expense_id}",
            "fields": _expense_fields(expense),
            "image_url": _entity_image(expense.image, "expense", expense.id),
            "image_delete_url": f"/expenses/{expense_id}/image/delete",
        },
    )


@router.post("/expenses", dependencies=[Depends(get_current_user)])
async def create_expense(request: Request, session: SessionDep):
    form = await request.form()
    try:
        data = _parse_expense(dict(form))
        expense = OneTimeExpense(**data.model_dump(), owner_id=request.state.user.id)
        session.add(expense)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/expenses/new?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            expense.image = await save_image(image, "expense", expense.id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/expenses/new?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/expenses?msg={quote('Расход добавлен')}", status_code=303)


@router.post("/expenses/{expense_id}", dependencies=[Depends(get_current_user)])
async def update_expense(
    request: Request, expense_id: int, session: SessionDep
):
    expense = await session.scalar(
        select(OneTimeExpense).where(
            OneTimeExpense.id == expense_id,
            OneTimeExpense.owner_id == request.state.user.id,
        )
    )
    if expense is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    try:
        data = _parse_expense(dict(form))
        for key, value in data.model_dump().items():
            setattr(expense, key, value)
        await session.commit()
    except (ValueError, IntegrityError) as exc:
        await session.rollback()
        return RedirectResponse(f"/expenses/{expense_id}/edit?error={quote(str(exc))}", status_code=303)
    image = form.get("image")
    if image is not None and (image.filename or "").strip():
        try:
            expense.image = await save_image(image, "expense", expense_id)
            await session.commit()
        except ValueError as exc:
            return RedirectResponse(f"/expenses/{expense_id}/edit?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/expenses?msg={quote('Расход обновлён')}", status_code=303)


@router.post("/expenses/{expense_id}/delete", dependencies=[Depends(get_current_user)])
async def delete_expense(
    request: Request, expense_id: int, session: SessionDep
):
    expense = await session.scalar(
        select(OneTimeExpense).where(
            OneTimeExpense.id == expense_id,
            OneTimeExpense.owner_id == request.state.user.id,
        )
    )
    if expense is None:
        raise HTTPException(status_code=404)
    await session.delete(expense)
    await session.commit()
    try:
        await delete_image("expense", expense_id)
    except ValueError:
        pass
    return RedirectResponse(f"/expenses?msg={quote('Расход удалён')}", status_code=303)


@router.post(
    "/expenses/{expense_id}/image/delete", dependencies=[Depends(get_current_user)]
)
async def delete_expense_image(
    request: Request, expense_id: int, session: SessionDep
):
    """Remove the expense's image and clear the column."""
    expense = await session.scalar(
        select(OneTimeExpense).where(
            OneTimeExpense.id == expense_id,
            OneTimeExpense.owner_id == request.state.user.id,
        )
    )
    if expense is None:
        raise HTTPException(status_code=404)
    try:
        await delete_image("expense", expense_id)
    except ValueError:
        pass
    expense.image = None
    await session.commit()
    return RedirectResponse(
        f"/expenses/{expense_id}/edit?msg={quote('Картинка удалена')}", status_code=303
    )


@router.get("/payments", dependencies=[Depends(get_current_user)])
async def payments_page(request: Request, session: SessionDep):
    user = request.state.user
    payments = await owned_payments(session, user.id)
    names = await _target_names(session, user.id)
    user_names = await _user_names(session)
    return templates.TemplateResponse(
        request,
        "payments.html",
        {"rows": [_payment_row(p, names, user_names) for p in payments]},
    )


@router.get("/settings", dependencies=[Depends(get_current_user)])
async def settings_page(request: Request, session: SessionDep):
    """Render the settings page with app settings and the bot status."""
    user = request.state.user
    items = []
    for meta in settings_svc.SETTING_META:
        value = await settings_svc.get_setting(session, meta["key"])
        if value is None:
            value = meta["default"] if meta["key"] != "tz" else settings.tz
        if meta["key"] == "proxy_password":
            value = ""
        items.append({**meta, "value": value})
    status = bot_manager.status()
    bot_status = {
        "running": status["running"],
        "connected": status["connected"],
        "error": status["error"],
        "token_configured": status["token_configured"],
        "bot_info": status["bot_info"],
        "enabled": await settings_svc.bot_enabled(session),
    }
    proxy = {item["key"]: item["value"] for item in items if item["key"].startswith("proxy_")}
    telegram = {
        "telegram_id": user.telegram_id,
        "confirmed": user.telegram_id_confirmed,
    }
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings_items": items,
            "bot_status": bot_status,
            "proxy": proxy,
            "telegram": telegram,
        },
    )


@router.post("/settings/telegram", dependencies=[Depends(get_current_user)])
async def settings_telegram_submit(request: Request, session: SessionDep):
    """Set or clear the user's Telegram id; changing it resets confirmation."""
    user = request.state.user
    form = await request.form()
    raw = str(form.get("telegram_id") or "").strip()
    if not raw:
        user.telegram_id = None
        user.telegram_id_confirmed = False
        await session.commit()
        return RedirectResponse("/settings?msg=Telegram отвязан", status_code=303)
    try:
        data = TelegramIdUpdate(telegram_id=int(raw))
    except (ValueError, ValidationError):
        return RedirectResponse(
            f"/settings?error={quote('Telegram ID должен быть целым числом в диапазоне (0, 2^63)')}",
            status_code=303,
        )
    telegram_id = data.telegram_id
    taken = await session.scalar(
        select(User).where(User.telegram_id == telegram_id, User.id != user.id)
    )
    if taken is not None:
        return RedirectResponse(
            f"/settings?error={quote('Этот Telegram уже привязан к другому аккаунту')}",
            status_code=303,
        )
    user.telegram_id = telegram_id
    user.telegram_id_confirmed = False
    await session.commit()
    chat_exists = await session.scalar(
        select(BotSubscriber.id).where(BotSubscriber.chat_id == telegram_id)
    )
    notified = False
    if chat_exists is not None and bot_manager.is_running():
        notified = await bot_manager.send_binding_notice(
            user.id, telegram_id, user.username
        )
    if notified:
        return RedirectResponse(
            "/settings?msg=Telegram сохранён — подтвердите привязку в чате",
            status_code=303,
        )
    return RedirectResponse(
        "/settings?msg=Telegram сохранён — ожидает подтверждения",
        status_code=303,
    )


@router.post("/settings", dependencies=[Depends(get_current_user)])
async def settings_submit(request: Request, session: SessionDep):
    """Save settings and reconcile the Telegram bot state."""
    form = await request.form()
    updates: dict[str, str] = {}
    for meta in settings_svc.SETTING_META:
        key = meta["key"]
        if key == "bot_enabled":
            continue
        if meta["type"] == "bool":
            updates[key] = "true" if form.get(key) == "on" else "false"
        else:
            raw = form.get(key)
            value = (raw or "").strip() if raw is not None else ""
            if not value:
                continue
            updates[key] = value
    try:
        for key, value in updates.items():
            await settings_svc.set_setting(session, key, value)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        return RedirectResponse(f"/settings?error={quote(str(exc))}", status_code=303)

    result = "not-configured"
    try:
        result = await bot_manager.apply()
    except Exception:
        logger.exception("bot_manager.apply() failed after settings update")
    if result == "started":
        return RedirectResponse(f"/settings?msg={quote('Бот запущен')}", status_code=303)
    if result == "stopped":
        return RedirectResponse(f"/settings?msg={quote('Бот остановлен')}", status_code=303)
    if result == "no-token":
        return RedirectResponse(
            f"/settings?error={quote('Бот не запущен: не задан BOT_TOKEN в окружении')}",
            status_code=303,
        )
    if result == "start-failed":
        detail = bot_manager.error or "Не удалось запустить бота"
        return RedirectResponse(f"/settings?error={quote(detail)}", status_code=303)
    return RedirectResponse("/settings?msg=Настройки сохранены", status_code=303)


@router.post("/settings/bot/start", dependencies=[Depends(get_current_user)])
async def bot_start(request: Request, session: SessionDep):
    """Enable and start the Telegram bot; error out when BOT_TOKEN is missing."""
    await settings_svc.set_setting(session, "bot_enabled", "true")
    await session.commit()
    if not settings.bot_token:
        return RedirectResponse(
            f"/settings?error={quote('Токен не задан (BOT_TOKEN)')}", status_code=303
        )
    started = await bot_manager.start()
    if not started:
        detail = bot_manager.error or "Не удалось запустить бота"
        return RedirectResponse(f"/settings?error={quote(detail)}", status_code=303)
    return RedirectResponse(f"/settings?msg={quote('Бот запущен')}", status_code=303)


@router.post("/settings/bot/stop", dependencies=[Depends(get_current_user)])
async def bot_stop(request: Request, session: SessionDep):
    """Disable and stop the Telegram bot."""
    await settings_svc.set_setting(session, "bot_enabled", "false")
    await session.commit()
    await bot_manager.stop()
    return RedirectResponse(f"/settings?msg={quote('Бот остановлен')}", status_code=303)


@router.post("/settings/password", dependencies=[Depends(get_current_user)])
async def change_password(request: Request, session: SessionDep):
    form = await request.form()
    old = str(form.get("old") or "")
    new = str(form.get("new") or "")
    confirm = str(form.get("confirm") or "")
    if new != confirm or not new:
        return RedirectResponse(f"/settings?error={quote('Пароли не совпадают')}", status_code=303)
    user = await get_current_user(request, session)
    if not verify_password(old, user.password_hash):
        return RedirectResponse(f"/settings?error={quote('Неверный текущий пароль')}", status_code=303)
    user.password_hash = hash_password(new)
    user.session_epoch += 1
    await session.commit()
    secret = await get_session_secret(session)
    ttl = await session_ttl_seconds(session)
    token = create_session_cookie(secret, user.id, user.session_epoch, ttl)
    response = RedirectResponse("/settings?msg=Пароль изменён", status_code=303)
    _set_session_cookie(response, request, token, ttl)
    return response


@router.post("/theme", dependencies=[Depends(get_current_user)])
async def set_theme(request: Request, session: SessionDep):
    form = await request.form()
    theme = str(form.get("theme") or "")
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False}, status_code=400)
    user = request.state.user
    user.theme = theme
    await session.commit()
    return JSONResponse({"ok": True})


def _telegram_status(user: User) -> str:
    if not user.telegram_id:
        return "—"
    if user.telegram_id_confirmed:
        return f"{user.telegram_id} (подтверждено)"
    return f"{user.telegram_id} (ожидает)"


def _user_fields(user: User | None, is_create: bool) -> list[dict]:
    return [
        {
            "name": "username",
            "label": "Логин (username)",
            "value": user.username if user else "",
            "type": "text",
            "required": True,
            "readonly": not is_create,
        },
        {
            "name": "password",
            "label": "Пароль" if is_create else "Новый пароль (пусто — не менять)",
            "value": "",
            "type": "password",
            "required": is_create,
        },
        {
            "name": "role",
            "label": "Роль",
            "value": user.role if user else "user",
            "type": "select",
            "options": [("user", "Пользователь"), ("admin", "Администратор")],
        },
        {
            "name": "is_active",
            "label": "Активен",
            "value": user.is_active if user else True,
            "type": "checkbox",
        },
        {
            "name": "telegram",
            "label": "Telegram",
            "value": _telegram_status(user) if user else "—",
            "type": "text",
            "readonly": True,
            "hint": "Привязку выполняет сам пользователь в Настройках",
        },
    ]


async def _active_admin_count(session: AsyncSession) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        )
        or 0
    )


@router.get("/users", dependencies=[Depends(require_admin)])
async def users_page(request: Request, session: SessionDep):
    users = (await session.scalars(select(User).order_by(User.username))).all()
    rows = [
        {
            "id": user.id,
            "username": user.username,
            "role": "Администратор" if user.role == "admin" else "Пользователь",
            "is_active": _bool_mark(user.is_active),
            "telegram": _telegram_status(user),
            "created_at": user.created_at.strftime("%d.%m.%Y") if user.created_at else "—",
        }
        for user in users
    ]
    return templates.TemplateResponse(request, "users.html", {"rows": rows})


@router.get("/users/new", dependencies=[Depends(require_admin)])
async def new_user(request: Request):
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {"title": "Новый пользователь", "action": "/users", "fields": _user_fields(None, True)},
    )


@router.post("/users", dependencies=[Depends(require_admin)])
async def create_user(request: Request, session: SessionDep):
    form = await request.form()
    try:
        data = UserCreate(
            username=str(form.get("username") or "").strip(),
            password=str(form.get("password") or ""),
            role=str(form.get("role") or "user"),
            is_active=form.get("is_active") == "on",
        )
    except ValidationError as exc:
        return RedirectResponse(f"/users/new?error={quote(str(exc))}", status_code=303)
    session.add(
        User(
            username=data.username,
            password_hash=hash_password(data.password),
            role=data.role,
            is_active=data.is_active,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return RedirectResponse(
            f"/users/new?error={quote('Пользователь с таким именем уже существует')}",
            status_code=303,
        )
    return RedirectResponse(f"/users?msg={quote('Пользователь создан')}", status_code=303)


@router.get("/users/{user_id}/edit", dependencies=[Depends(require_admin)])
async def edit_user(request: Request, session: SessionDep, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": f"Пользователь — {user.username}",
            "action": f"/users/{user_id}",
            "fields": _user_fields(user, False),
        },
    )


@router.post("/users/{user_id}", dependencies=[Depends(require_admin)])
async def update_user(request: Request, session: SessionDep, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    try:
        data = UserUpdate(
            role=str(form.get("role") or "user"),
            is_active=form.get("is_active") == "on",
            password=(str(form.get("password") or "") or None),
        )
    except ValidationError as exc:
        return RedirectResponse(
            f"/users/{user_id}/edit?error={quote(str(exc))}", status_code=303
        )
    will_be_admin = data.role == "admin" and data.is_active
    is_last_admin = user.role == "admin" and user.is_active and await _active_admin_count(session) <= 1
    if is_last_admin and not will_be_admin:
        return RedirectResponse(
            "/users?error=" + quote("Нельзя деактивировать последнего активного администратора"),
            status_code=303,
        )
    user.role = data.role
    user.is_active = data.is_active
    if data.password:
        user.password_hash = hash_password(data.password)
        user.session_epoch += 1
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return RedirectResponse(
            "/users?error=" + quote("Пользователь с таким именем уже существует"),
            status_code=303,
        )
    return RedirectResponse(f"/users?msg={quote('Пользователь обновлён')}", status_code=303)


@router.post("/users/{user_id}/delete", dependencies=[Depends(require_admin)])
async def delete_user(request: Request, session: SessionDep, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    current = request.state.user
    if user.id == current.id:
        return RedirectResponse(
            "/users?error=" + quote("Нельзя удалить самого себя"), status_code=303
        )
    if user.role == "admin" and user.is_active and await _active_admin_count(session) <= 1:
        return RedirectResponse(
            "/users?error=" + quote("Нельзя удалить последнего активного администратора"),
            status_code=303,
        )
    await session.execute(update(Payment).where(Payment.paid_by == user.id).values(paid_by=None))
    owned_server_ids = select(Server.id).where(Server.owner_id == user.id)
    owned_sub_ids = select(Subscription.id).where(Subscription.owner_id == user.id)
    await session.execute(
        delete(Payment).where(
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
    )
    await session.execute(delete(OneTimeExpense).where(OneTimeExpense.owner_id == user.id))
    await session.execute(delete(Server).where(Server.owner_id == user.id))
    await session.execute(delete(Subscription).where(Subscription.owner_id == user.id))
    await session.execute(
        update(BotSubscriber).where(BotSubscriber.user_id == user.id).values(user_id=None)
    )
    await session.delete(user)
    await session.commit()
    return RedirectResponse(f"/users?msg={quote('Пользователь удалён')}", status_code=303)


@router.get("/backups", dependencies=[Depends(require_admin)])
async def backups_page(request: Request):
    return templates.TemplateResponse(request, "backups.html", {"backups": list_backups()})


@router.post("/backups/create", dependencies=[Depends(require_admin)])
async def backups_create(request: Request, session: SessionDep):
    await create_backup()
    return RedirectResponse("/backups?msg=Бэкап создан", status_code=303)


@router.get("/backups/download/{name}", dependencies=[Depends(require_admin)])
async def backups_download(name: str):
    path = resolve_backup(name)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.post("/backups/{name}/delete", dependencies=[Depends(require_admin)])
async def backups_delete(name: str):
    path = resolve_backup(name)
    if path is None:
        raise HTTPException(status_code=404)
    path.unlink(missing_ok=True)
    return RedirectResponse("/backups?msg=Бэкап удалён", status_code=303)