import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Setting

_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

logger = logging.getLogger(__name__)

SETTING_META: list[dict[str, str]] = [
    {
        "key": "morning_enabled",
        "default": "true",
        "description": "Утренний дайджест включён",
        "type": "bool",
    },
    {
        "key": "morning_time",
        "default": "09:00",
        "description": "Время утреннего дайджеста",
        "type": "time",
    },
    {
        "key": "evening_reminder_enabled",
        "default": "true",
        "description": "Вечернее напоминание включено",
        "type": "bool",
    },
    {
        "key": "evening_reminder_time",
        "default": "19:00",
        "description": "Время вечернего напоминания",
        "type": "time",
    },
    {
        "key": "remind_soon_days",
        "default": "7",
        "description": "Горизонт «ближайших» платежей (1–30)",
        "type": "int",
    },
    {
        "key": "backup_enabled",
        "default": "false",
        "description": "Автоматический бэкап по расписанию",
        "type": "bool",
    },
    {
        "key": "backup_time",
        "default": "03:00",
        "description": "Время автоматического бэкапа",
        "type": "time",
    },
    {
        "key": "backup_keep",
        "default": "14",
        "description": "Сколько бэкапов хранить (1–100)",
        "type": "int",
    },
    {
        "key": "session_ttl_days",
        "default": "1",
        "description": "Срок жизни веб-сессии (дней, 1–30)",
        "type": "int",
    },
    {
        "key": "bot_enabled",
        "default": "false",
        "description": "Telegram-бот включён",
        "type": "bool",
    },
    {
        "key": "proxy_enabled",
        "default": "false",
        "description": "SOCKS5-прокси включён",
        "type": "bool",
    },
    {
        "key": "proxy_host",
        "default": "",
        "description": "Хост прокси",
        "type": "str",
    },
    {
        "key": "proxy_port",
        "default": "",
        "description": "Порт прокси",
        "type": "int",
    },
    {
        "key": "proxy_user",
        "default": "",
        "description": "Логин прокси (если есть)",
        "type": "str",
    },
    {
        "key": "proxy_password",
        "default": "",
        "description": "Пароль прокси (не отображается)",
        "type": "str",
    },
    {
        "key": "tz",
        "default": "",
        "description": "Часовой пояс для «сегодня»",
        "type": "str",
    },
]

_INT_RANGES: dict[str, tuple[int, int]] = {
    "remind_soon_days": (1, 30),
    "session_ttl_days": (1, 30),
    "backup_keep": (1, 100),
    "proxy_port": (1, 65535),
}


def _meta(key: str) -> dict[str, str]:
    return next(item for item in SETTING_META if item["key"] == key)


def _default(key: str) -> str:
    meta = _meta(key)
    if key == "tz":
        return settings.tz
    return meta["default"]


def _validate(key: str, value: str) -> None:
    vtype = _meta(key)["type"]
    if vtype == "bool" and value not in ("true", "false"):
        raise ValueError("setting must be 'true' or 'false'")
    if vtype == "time" and not _TIME_RE.fullmatch(value):
        raise ValueError("time must be in HH:MM 24h format")
    if vtype == "int":
        if key == "proxy_port" and not value:
            return
        lo, hi = _INT_RANGES.get(key, (1, 30))
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if not lo <= parsed <= hi:
            raise ValueError(f"{key} must be in {lo}..{hi}")


async def get_setting(session: AsyncSession, key: str) -> str | None:
    """Return raw setting value or None if the key is missing."""
    setting = await session.get(Setting, key)
    return setting.value if setting else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    """Store a validated setting value; caller commits the session."""
    _validate(key, value)
    setting = await session.get(Setting, key)
    if setting is None:
        setting = Setting(key=key, value=value, updated_at=datetime.now(UTC))
        session.add(setting)
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)


async def ensure_defaults(session: AsyncSession) -> None:
    """Write missing settings with their defaults; existing ones are kept."""
    now = datetime.now(UTC)
    for meta in SETTING_META:
        key = meta["key"]
        if await session.get(Setting, key) is not None:
            continue
        session.add(
            Setting(
                key=key,
                value=_default(key),
                description=meta["description"],
                updated_at=now,
            )
        )


async def morning_enabled(session: AsyncSession) -> bool:
    """Whether the morning digest is enabled (defaults to true)."""
    value = await get_setting(session, "morning_enabled")
    return (value or _default("morning_enabled")) == "true"


async def morning_time(session: AsyncSession) -> str:
    """Morning digest time as HH:MM."""
    value = await get_setting(session, "morning_time")
    return value or _default("morning_time")


async def evening_reminder_enabled(session: AsyncSession) -> bool:
    """Whether the evening reminder is enabled (defaults to true)."""
    value = await get_setting(session, "evening_reminder_enabled")
    return (value or _default("evening_reminder_enabled")) == "true"


async def evening_reminder_time(session: AsyncSession) -> str:
    """Evening reminder time as HH:MM."""
    value = await get_setting(session, "evening_reminder_time")
    return value or _default("evening_reminder_time")


async def remind_soon_days(session: AsyncSession) -> int:
    """Horizon for 'soon' payments in days (default 7)."""
    value = await get_setting(session, "remind_soon_days")
    return int(value or _default("remind_soon_days"))


async def backup_enabled(session: AsyncSession) -> bool:
    """Whether the scheduled backup is enabled (defaults to false)."""
    value = await get_setting(session, "backup_enabled")
    return (value or _default("backup_enabled")) == "true"


async def backup_time(session: AsyncSession) -> str:
    """Scheduled backup time as HH:MM."""
    value = await get_setting(session, "backup_time")
    return value or _default("backup_time")


async def backup_keep(session: AsyncSession) -> int:
    """How many backups to keep (default 14)."""
    value = await get_setting(session, "backup_keep")
    return int(value or _default("backup_keep"))


async def session_ttl_days(session: AsyncSession) -> int:
    """Web session lifetime in days (default 1)."""
    value = await get_setting(session, "session_ttl_days")
    return int(value or _default("session_ttl_days"))


async def bot_enabled(session: AsyncSession) -> bool:
    """Whether the Telegram bot is enabled (defaults to false)."""
    value = await get_setting(session, "bot_enabled")
    return (value or _default("bot_enabled")) == "true"


async def proxy_enabled(session: AsyncSession) -> bool:
    """Whether the SOCKS5 proxy is enabled (defaults to false)."""
    value = await get_setting(session, "proxy_enabled")
    return (value or _default("proxy_enabled")) == "true"


async def proxy_host(session: AsyncSession) -> str:
    """Proxy host."""
    value = await get_setting(session, "proxy_host")
    return value or _default("proxy_host")


async def proxy_port(session: AsyncSession) -> int | None:
    """Proxy port, or None when not configured."""
    value = await get_setting(session, "proxy_port")
    if value is None:
        value = _default("proxy_port")
    if not value:
        return None
    return int(value)


async def proxy_user(session: AsyncSession) -> str:
    """Proxy username."""
    value = await get_setting(session, "proxy_user")
    return value or _default("proxy_user")


async def proxy_password(session: AsyncSession) -> str:
    """Proxy password."""
    value = await get_setting(session, "proxy_password")
    return value or _default("proxy_password")


async def proxy_url(session: AsyncSession) -> str:
    """Build a SOCKS5 proxy URL from settings; "" when disabled or host is empty."""
    if not await proxy_enabled(session):
        return ""
    host = await proxy_host(session)
    if not host:
        logger.warning("proxy is enabled but proxy_host is empty; proxy is off")
        return ""
    port = await proxy_port(session) or 1080
    user = await proxy_user(session)
    password = await proxy_password(session)
    logger.info("proxy configured for %s:%s", host, port)
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user and password else ""
    return f"socks5://{auth}{host}:{port}"


async def tz(session: AsyncSession) -> str:
    """Time zone name, falls back to env TZ."""
    value = await get_setting(session, "tz")
    return value or settings.tz