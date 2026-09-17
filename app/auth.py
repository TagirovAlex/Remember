import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Setting, User
from app.security import hash_password, sign_session, verify_password, verify_session
from app.services import settings as settings_svc

logger = logging.getLogger(__name__)

SESSION_COOKIE = "remember_sid"
SESSION_SECRET_KEY = "auth_session_secret"
DEFAULT_SESSION_TTL_DAYS = 1
_LOGIN_PATH = "/login"
_ADMIN_PASSWORD_HASH_KEY = "auth_password_hash"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _deny(request: Request, *, detail: str = "Не авторизован") -> HTTPException:
    """Raise 401 for JSON API routes and a 303 redirect for HTML pages."""
    if request.url.path.startswith("/api/"):
        return HTTPException(status_code=401, detail=detail)
    return HTTPException(
        status_code=303,
        detail=detail,
        headers={"Location": _LOGIN_PATH},
    )


async def get_session_secret(session: AsyncSession) -> str:
    """Return the session signing secret.

    Uses SECRET_KEY from the environment when set; otherwise a generated
    random secret stored in the settings table.
    """
    if settings.secret_key:
        return settings.secret_key
    setting = await session.get(Setting, SESSION_SECRET_KEY)
    if setting is not None:
        return setting.value
    secret = secrets.token_hex(32)
    session.add(
        Setting(key=SESSION_SECRET_KEY, value=secret, updated_at=datetime.now(UTC))
    )
    await session.commit()
    return secret


async def session_ttl_seconds(session: AsyncSession) -> int:
    """Web session lifetime in seconds (default 1 day)."""
    try:
        days = await settings_svc.session_ttl_days(session)
    except (TypeError, ValueError):
        days = DEFAULT_SESSION_TTL_DAYS
    return max(1, days) * 24 * 3600


def create_session_cookie(
    session_secret: str, user_id: int, epoch: int, max_age_seconds: int
) -> str:
    """Build a signed session cookie carrying the user id and session epoch."""
    return sign_session(
        {"sub": str(user_id), "epoch": epoch},
        session_secret,
        max_age_seconds,
    )


async def authenticate_user(
    session: AsyncSession, username: str, password: str
) -> User | None:
    """Return the active user when credentials match, otherwise None."""
    user = await session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def get_current_user(request: Request, session: SessionDep) -> User:
    """Require a valid session cookie; return the active user.

    The loaded user is also attached to ``request.state.user`` for templates.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _deny(request)
    secret = await get_session_secret(session)
    max_age = await session_ttl_seconds(session)
    payload = verify_session(token, secret, max_age)
    user: User | None = None
    if payload and payload.get("sub") is not None:
        try:
            user_id = int(payload["sub"])
        except (TypeError, ValueError):
            user_id = None
        if user_id is not None:
            user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise _deny(request)
    if payload.get("epoch") != user.session_epoch:
        raise _deny(request)
    request.state.user = user
    return user


async def require_admin(request: Request, session: SessionDep) -> None:
    """Require an authenticated user with the admin role."""
    user = await get_current_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещён")


async def ensure_admin_user(session: AsyncSession) -> None:
    """Seed the first admin user from the legacy setting or env ADMIN_PASSWORD.

    When at least one user exists the function does nothing. Otherwise an
    ``auth_password_hash`` setting is migrated into the ``admin`` user, else the
    ADMIN_PASSWORD from the environment is used.
    """
    if await session.scalar(select(User)) is not None:
        return
    legacy = await session.get(Setting, _ADMIN_PASSWORD_HASH_KEY)
    if legacy is not None:
        session.add(
            User(username="admin", password_hash=legacy.value, role="admin")
        )
        await session.delete(legacy)
        await session.commit()
        return
    if settings.admin_password:
        session.add(
            User(
                username="admin",
                password_hash=hash_password(settings.admin_password),
                role="admin",
            )
        )
        await session.commit()
        return
    logger.warning(
        "No users in the database and ADMIN_PASSWORD is not set: "
        "the app will start with no accounts"
    )