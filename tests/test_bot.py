from collections.abc import AsyncGenerator

import httpx
import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import SendPhoto
from aiogram.types import InputFile

from app.bot.bot import HttpxSession, build_bot, build_dispatcher
from app.bot.handlers import AddStates
from app.bot.keyboards import (
    add_kind_callback,
    add_kind_keyboard,
    cancel_binding_callback,
    confirm_binding_callback,
    confirm_binding_keyboard,
    digest_pay_keyboard,
    parse_add_kind_callback,
    parse_cancel_binding_callback,
    parse_confirm_binding_callback,
    parse_pay_callback,
    parse_period_callback,
    pay_callback,
    pay_keyboard,
    period_callback,
    period_keyboard,
)

FAKE_TOKEN = "123:xyz"


def test_pay_callback_roundtrip():
    assert parse_pay_callback(pay_callback("server", 5)) == ("server", 5)
    assert parse_pay_callback(pay_callback("subscription", 12)) == ("subscription", 12)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "pay",
        "pay:",
        "pay:server",
        "pay:server:",
        "pay::5",
        "pay:server:abc",
        "pay:server:0",
        "pay:server:-3",
        "pay:expense:5",
        "pay:server:5:extra",
        "unknown:server:5",
    ],
)
def test_parse_pay_callback_invalid(data):
    assert parse_pay_callback(data) is None


def test_pay_callback_invalid_target_type():
    with pytest.raises(ValueError):
        pay_callback("expense", 5)


def test_add_kind_callback_roundtrip():
    for target_type in ("server", "subscription"):
        assert parse_add_kind_callback(add_kind_callback(target_type)) == target_type


@pytest.mark.parametrize("data", [None, "", "add", "add:", "add:server:x", "add:expense"])
def test_parse_add_kind_callback_invalid(data):
    assert parse_add_kind_callback(data) is None


def test_period_callback_roundtrip():
    for period in ("monthly", "quarterly", "yearly"):
        assert parse_period_callback(period_callback(period)) == period


@pytest.mark.parametrize("data", [None, "", "period", "period:", "period:weekly", "period:monthly:x"])
def test_parse_period_callback_invalid(data):
    assert parse_period_callback(data) is None


def test_period_callback_invalid_period():
    with pytest.raises(ValueError):
        period_callback("weekly")


def test_callback_data_is_compact():
    for data in (
        pay_callback("subscription", 99999),
        add_kind_callback("subscription"),
        period_callback("quarterly"),
    ):
        assert len(data.encode()) <= 64


def test_pay_keyboard_buttons():
    entries = [
        {"target_type": "server", "target_id": 1, "name": "a"},
        {"target_type": "subscription", "target_id": 2, "name": "b"},
    ]
    keyboard = pay_keyboard(entries)
    buttons = [
        button for row in keyboard.inline_keyboard for button in row
    ]
    assert [b.callback_data for b in buttons] == ["pay:server:1", "pay:subscription:2"]
    assert all(b.text == "Оплачено ✓" for b in buttons)


def test_digest_pay_keyboard_collects_all_sections():
    digest = {
        "due_today": [{"target_type": "server", "target_id": 1}],
        "due_soon": [{"target_type": "subscription", "target_id": 2}],
        "overdue": [{"target_type": "server", "target_id": 3}],
    }
    keyboard = digest_pay_keyboard(digest)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert sorted(b.callback_data for b in buttons) == [
        "pay:server:1",
        "pay:server:3",
        "pay:subscription:2",
    ]


def test_add_kind_keyboard_and_period_keyboard():
    assert [
        b.callback_data for row in add_kind_keyboard().inline_keyboard for b in row
    ] == ["add:server", "add:subscription"]
    assert [
        b.callback_data for row in period_keyboard().inline_keyboard for b in row
    ] == ["period:monthly", "period:quarterly", "period:yearly"]


def test_confirm_binding_callback_roundtrip():
    assert parse_confirm_binding_callback(confirm_binding_callback(42)) == 42
    assert parse_cancel_binding_callback(cancel_binding_callback(42)) == 42


@pytest.mark.parametrize(
    "data",
    [None, "", "tg_confirm", "tg_confirm:", "tg_confirm:abc", "tg_confirm:0", "tg_confirm:-3"],
)
def test_parse_confirm_binding_callback_invalid(data):
    assert parse_confirm_binding_callback(data) is None


def test_confirm_binding_keyboard_buttons():
    keyboard = confirm_binding_keyboard(7)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["tg_confirm:7", "tg_cancel:7"]
    assert [b.text for b in buttons] == ["Подтвердить", "Отменить"]


def test_binding_notice_text_mentions_username():
    from app.bot.keyboards import binding_notice_text

    text = binding_notice_text("alice")
    assert "alice" in text
    assert "Подтвердить" in text
    assert "Отменить" in text


def test_add_states_described():
    assert {state.state for state in AddStates.__states__} == {
        "AddStates:kind",
        "AddStates:name",
        "AddStates:amount",
        "AddStates:period",
        "AddStates:next_due",
    }


def test_build_dispatcher_ok():
    dispatcher = build_dispatcher()
    assert dispatcher is not None


def test_build_bot_without_proxy():
    bot = build_bot(FAKE_TOKEN)
    session = bot.session
    assert isinstance(session, HttpxSession)
    assert isinstance(session.client, httpx.AsyncClient)


def test_build_bot_with_proxy():
    bot = build_bot(FAKE_TOKEN, "socks5://user:pass@proxy.example:1080")
    session = bot.session
    assert isinstance(session, HttpxSession)
    assert isinstance(session.client, httpx.AsyncClient)


class _StubInputFile(InputFile):
    """InputFile yielding known bytes in small chunks without touching disk/network."""

    def __init__(self, data: bytes, filename: str | None = None, chunk_size: int = 7):
        super().__init__(filename=filename, chunk_size=chunk_size)
        self._data = data

    async def read(self, bot: Bot) -> AsyncGenerator[bytes, None]:
        for i in range(0, len(self._data), self.chunk_size):
            yield self._data[i : i + self.chunk_size]


class _RecordingClient:
    """httpx-style client that records requests instead of sending them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs))
        return httpx.Response(200, json={"ok": True, "result": None})


async def test_make_request_uploads_file_bytes():
    payload = b"known-file-bytes-for-upload"
    session = HttpxSession(client=_RecordingClient())
    bot = Bot(
        token=FAKE_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )

    result = await session.make_request(
        bot, SendPhoto(chat_id=1, photo=_StubInputFile(payload, "report.bin"))
    )

    assert result is None
    assert session.client.calls
    _, kwargs = session.client.calls[0]
    files = kwargs["files"]
    assert files
    (content, filename) = next(iter(files.values()))
    assert filename == "report.bin"
    assert content == payload