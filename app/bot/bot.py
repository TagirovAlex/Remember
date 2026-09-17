"""aiogram factories: bot with a custom httpx session, dispatcher, DB session factory."""

from typing import Any, cast

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import handlers
from app.db import SessionLocal


class HttpxSession(BaseSession):
    """BaseSession backed by an httpx.AsyncClient (used for SOCKS5 proxy support)."""

    def __init__(self, client: httpx.AsyncClient, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        """Expose the underlying httpx client (useful for tests)."""
        return self._client

    def build_request_data(
        self, bot: Bot, method: TelegramMethod[TelegramType]
    ) -> tuple[dict[str, str], dict[str, InputFile]]:
        """Build form data and collect InputFile attachments (bytes are read later)."""
        data: dict[str, str] = {}
        files: dict[str, InputFile] = {}
        for key, value in method.model_dump(warnings=False).items():
            value = self.prepare_value(value, bot=bot, files=files)
            if not value:
                continue
            data[key] = str(value)
        return data, files

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        data, collected = self.build_request_data(bot, method)
        files: dict[str, tuple[bytes, str]] = {}
        for key, file in collected.items():
            files[key] = (await _read_file_bytes(file, bot), file.filename or key)
        try:
            response = await self._client.post(
                url,
                data=data,
                files=files or None,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            msg = f"{type(exc).__name__}: {exc}"
            raise TelegramNetworkError(method=method, message=msg) from exc
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=response.status_code,
            content=response.text,
        )
        return cast(TelegramType, response.result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        if headers is None:
            headers = {}
        async with self._client.stream(
            "GET", url, headers=headers, timeout=timeout
        ) as response:
            if raise_for_status:
                response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size):
                yield chunk

    async def close(self) -> None:
        await self._client.aclose()


async def _read_file_bytes(file: InputFile, bot: Bot) -> bytes:
    """Collect all bytes produced by an InputFile's async generator."""
    return b"".join([chunk async for chunk in file.read(bot)])


def build_bot(token: str, proxy_url: str = "") -> Bot:
    """Create a Bot; when proxy_url is set, route Telegram traffic through it."""
    client = httpx.AsyncClient(proxy=proxy_url) if proxy_url else httpx.AsyncClient()
    session = HttpxSession(client=client)
    return Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=None))


def build_dispatcher() -> Dispatcher:
    """Create a Dispatcher with in-memory FSM storage and all routers registered."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(handlers.router)
    return dispatcher


def create_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared DB session factory (SQLAlchemy async_sessionmaker)."""
    return SessionLocal