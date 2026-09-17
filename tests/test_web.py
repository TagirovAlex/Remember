from datetime import date
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.bot.manager import bot_manager
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, OneTimeExpense, Server, Subscription, User
from app.security import hash_password
from app.services import settings as settings_svc

PASSWORD = "secret-pass"
SID = "remember_sid"


@pytest.fixture
async def engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def app(engine, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", PASSWORD)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_session] = override_get_session
    async with session_factory() as session:
        await ensure_admin_user(session)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post(
        "/login", data={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 303
    return response


async def _login_as(
    client: httpx.AsyncClient, username: str, password: str
) -> httpx.Response:
    return await client.post(
        "/login", data={"username": username, "password": password}
    )


async def _add_subscription(session_factory, name: str, next_due: date) -> int:
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        sub = Subscription(
            name=name,
            provider="TestProvider",
            amount_rub=900,
            period="monthly",
            next_due=next_due,
            status="active",
            owner_id=admin.id,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return sub.id


async def test_login_rejects_wrong_password(client):
    response = await client.post(
        "/login", data={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 303
    assert "/login?error=" in response.headers["location"]


async def test_login_success_sets_cookie(client):
    response = await _login(client)
    assert response.cookies.get(SID)
    assert client.cookies.get(SID)


async def test_dashboard_requires_auth(client):
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_dashboard_authed(client):
    await _login(client)
    response = await client.get("/")
    assert response.status_code == 200
    assert "Дашборд" in response.text


async def test_dashboard_with_entities(client, session_factory):
    await _add_subscription(session_factory, "Netflix", date(2026, 9, 20))
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="GPU-A",
            kind="gpu",
            provider="Hetzner",
            amount_rub=12000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
    await _login(client)
    response = await client.get("/")
    assert response.status_code == 200
    assert "GPU-A" in response.text
    assert "Netflix" in response.text
    assert "12 000 ₽" in response.text
    assert "900 ₽" in response.text


async def test_pay_server_via_web(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="GPU-B",
            kind="vm",
            provider="Hetzner",
            amount_rub=1000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
        server_id = server.id
    await _login(client)
    response = await client.post(
        f"/servers/{server_id}/pay", data={"next": "/servers"}
    )
    assert response.status_code == 303
    assert "msg" in response.headers["location"]
    async with session_factory() as session:
        server = await session.get(Server, server_id)
        assert server.next_due == date(2026, 10, 16)


async def test_pay_server_twice_same_day_does_not_double_rollover(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="GPU-C",
            kind="vm",
            provider="Hetzner",
            amount_rub=1000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        session.add(server)
        await session.commit()
        server_id = server.id
    await _login(client)
    first = await client.post(
        f"/servers/{server_id}/pay", data={"next": "/servers"}
    )
    assert first.status_code == 303
    assert "Оплачено ✓" in unquote(first.headers["location"])

    second = await client.post(
        f"/servers/{server_id}/pay", data={"next": "/servers"}
    )
    assert second.status_code == 303
    assert "уже отмечено сегодня" in unquote(second.headers["location"])

    async with session_factory() as session:
        server = await session.get(Server, server_id)
        assert server.next_due == date(2026, 10, 16)


async def test_api_mark_paid_requires_auth(client):
    response = await client.post(
        "/api/payments/mark",
        json={
            "target_type": "subscription",
            "target_id": 1,
            "amount_rub": 900,
            "paid_date": "2026-09-20",
        },
    )
    assert response.status_code == 401


async def test_api_mark_paid_subscription_idempotent(client, session_factory):
    sub_id = await _add_subscription(session_factory, "Netflix", date(2026, 9, 20))
    await _login(client)
    payload = {
        "target_type": "subscription",
        "target_id": sub_id,
        "amount_rub": 900,
        "paid_date": "2026-09-20",
    }
    first = await client.post("/api/payments/mark", json=payload)
    assert first.status_code == 200
    data = first.json()
    assert data["status"] == "ok"
    assert data["already"] is False
    assert data["new_next_due"] == "2026-10-20"
    payment_id = data["payment"]["id"]

    second = await client.post("/api/payments/mark", json=payload)
    assert second.status_code == 200
    data2 = second.json()
    assert data2["already"] is True
    assert data2["payment"]["id"] == payment_id
    assert data2["new_next_due"] == "2026-10-20"


async def test_api_mark_paid_bad_target_type(client):
    await _login(client)
    response = await client.post(
        "/api/payments/mark",
        json={"target_type": "expense", "target_id": 1, "amount_rub": 10, "paid_date": "2026-09-20"},
    )
    assert response.status_code == 400


async def test_api_mark_paid_missing_target(client):
    await _login(client)
    response = await client.post(
        "/api/payments/mark",
        json={"target_type": "server", "target_id": 999, "amount_rub": 10, "paid_date": "2026-09-20"},
    )
    assert response.status_code == 404


async def test_settings_page_authed(client):
    await _login(client)
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "Настройки" in response.text


async def test_settings_update_and_validate(client, session_factory):
    await _login(client)
    form = {
        "morning_enabled": "on",
        "morning_time": "08:30",
        "evening_reminder_enabled": "",
        "evening_reminder_time": "19:00",
        "remind_soon_days": "10",
        "tz": "Europe/Moscow",
    }
    response = await client.post("/settings", data=form)
    assert response.status_code == 303
    async with session_factory() as session:
        assert await settings_svc.get_setting(session, "remind_soon_days") == "10"
        assert await settings_svc.get_setting(session, "morning_time") == "08:30"

    page = await client.get("/settings")
    assert 'value="10"' in page.text

    form["remind_soon_days"] = "999"
    response = await client.post("/settings", data=form)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    async with session_factory() as session:
        assert await settings_svc.get_setting(session, "remind_soon_days") == "10"


async def test_settings_page_shows_bot_status(client):
    await _login(client)
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "Telegram-бот" in response.text
    assert "Остановлен" in response.text


async def test_settings_page_shows_bot_running_connected(client, monkeypatch):
    await _login(client)
    monkeypatch.setattr(
        bot_manager,
        "status",
        lambda: {
            "running": True,
            "connected": True,
            "error": None,
            "token_configured": True,
            "bot_info": {"username": "reminder_bot", "first_name": "Reminder"},
        },
    )
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "✅ Запущен" in response.text
    assert "✅ Online — @reminder_bot (Reminder)" in response.text


async def test_settings_page_shows_bot_error(client, monkeypatch):
    await _login(client)
    monkeypatch.setattr(
        bot_manager,
        "status",
        lambda: {
            "running": False,
            "connected": False,
            "error": "API недоступен: proxy unreachable",
            "token_configured": True,
            "bot_info": None,
        },
    )
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "Ошибка:" in response.text
    assert "proxy unreachable" in response.text
    assert "Остановлен" not in response.text
    assert "Online" not in response.text


async def test_settings_enable_bot_saves_value(client, session_factory, monkeypatch):
    await _login(client)
    monkeypatch.setattr(settings, "bot_token", "123:test")

    async def fake_start() -> bool:
        return True

    monkeypatch.setattr(bot_manager, "start", fake_start)
    response = await client.post("/settings/bot/start")
    assert response.status_code == 303
    assert "Бот запущен" in unquote(response.headers["location"])
    async with session_factory() as session:
        assert await settings_svc.bot_enabled(session) is True


async def test_settings_submit_when_manager_not_configured(client, session_factory):
    await _login(client)
    response = await client.post(
        "/settings", data={"remind_soon_days": "10"}
    )
    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
    async with session_factory() as session:
        assert await settings_svc.get_setting(session, "remind_soon_days") == "10"


async def test_settings_submit_does_not_touch_bot_enabled(client, session_factory):
    await _login(client)
    async with session_factory() as session:
        await settings_svc.set_setting(session, "bot_enabled", "true")
        await session.commit()
    response = await client.post("/settings", data={"remind_soon_days": "5"})
    assert response.status_code == 303
    async with session_factory() as session:
        assert await settings_svc.bot_enabled(session) is True


async def test_settings_bot_start_without_token_shows_error(
    client, session_factory, monkeypatch
):
    await _login(client)
    monkeypatch.setattr(settings, "bot_token", "")
    response = await client.post("/settings/bot/start")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "Токен не задан" in unquote(response.headers["location"])
    async with session_factory() as session:
        assert await settings_svc.bot_enabled(session) is True


async def test_settings_bot_start_failure_shows_error_flash(client, monkeypatch):
    await _login(client)
    monkeypatch.setattr(settings, "bot_token", "123:test")

    async def fake_start() -> bool:
        return False

    monkeypatch.setattr(bot_manager, "start", fake_start)
    monkeypatch.setattr(bot_manager, "_error", "API недоступен: proxy unreachable")
    response = await client.post("/settings/bot/start")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "proxy unreachable" in unquote(response.headers["location"])


async def test_settings_submit_start_failed_shows_error_flash(client, session_factory, monkeypatch):
    await _login(client)
    async with session_factory() as session:
        await settings_svc.set_setting(session, "bot_enabled", "true")
        await session.commit()

    async def fake_apply() -> str:
        return "start-failed"

    monkeypatch.setattr(bot_manager, "apply", fake_apply)
    monkeypatch.setattr(bot_manager, "_error", "API недоступен: proxy unreachable")
    response = await client.post("/settings", data={"remind_soon_days": "10"})
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "proxy unreachable" in unquote(response.headers["location"])


async def test_settings_page_hides_proxy_password(client, session_factory):
    await _login(client)
    async with session_factory() as session:
        await settings_svc.set_setting(session, "proxy_password", "super-secret-42")
        await session.commit()
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "super-secret-42" not in response.text


async def test_settings_page_shows_proxy_section(client):
    await _login(client)
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "SOCKS5-прокси" in response.text
    assert "Хост прокси" in response.text
    assert 'name="proxy_password"' in response.text
    assert 'placeholder="оставить текущий"' in response.text
    assert "применится при следующем запуске бота" in response.text


async def test_settings_submit_saves_proxy_fields(client, session_factory):
    await _login(client)
    form = {
        "proxy_enabled": "on",
        "proxy_host": "10.0.0.5",
        "proxy_port": "3128",
        "proxy_user": "proxy-user",
        "proxy_password": "proxy-pass-1",
    }
    response = await client.post("/settings", data=form)
    assert response.status_code == 303
    async with session_factory() as session:
        assert await settings_svc.proxy_enabled(session) is True
        assert await settings_svc.proxy_host(session) == "10.0.0.5"
        assert await settings_svc.proxy_port(session) == 3128
        assert await settings_svc.proxy_user(session) == "proxy-user"
        assert await settings_svc.proxy_password(session) == "proxy-pass-1"


async def test_settings_submit_empty_proxy_password_keeps_current(
    client, session_factory
):
    await _login(client)
    async with session_factory() as session:
        await settings_svc.set_setting(session, "proxy_password", "saved-secret")
        await settings_svc.set_setting(session, "proxy_host", "old-host")
        await session.commit()
    response = await client.post(
        "/settings",
        data={"proxy_host": "new-host", "proxy_password": ""},
    )
    assert response.status_code == 303
    async with session_factory() as session:
        assert await settings_svc.proxy_host(session) == "new-host"
        assert await settings_svc.proxy_password(session) == "saved-secret"


async def test_settings_bot_stop_disables_and_redirects(
    client, session_factory, monkeypatch
):
    await _login(client)
    async with session_factory() as session:
        await settings_svc.set_setting(session, "bot_enabled", "true")
        await session.commit()
    stopped: list[bool] = []

    async def fake_stop() -> bool:
        stopped.append(True)
        return True

    monkeypatch.setattr(bot_manager, "stop", fake_stop)
    response = await client.post("/settings/bot/stop")
    assert response.status_code == 303
    assert "Бот остановлен" in unquote(response.headers["location"])
    assert stopped == [True]
    async with session_factory() as session:
        assert await settings_svc.bot_enabled(session) is False


async def _add_user(session_factory, username, password, role="user"):
    async with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _add_server_owned(session_factory, name, owner_id) -> int:
    async with session_factory() as session:
        server = Server(
            name=name,
            kind="gpu",
            provider="Hetzner",
            amount_rub=1000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=owner_id,
        )
        session.add(server)
        await session.commit()
        await session.refresh(server)
        return server.id


async def test_isolation_lists_hide_foreign_entities(client, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
        alice = await session.scalar(select(User).where(User.username == "alice"))
        alice_id = alice.id
    await _add_server_owned(session_factory, "admin-only", admin_id)
    await _add_server_owned(session_factory, "alice-only", alice_id)

    await _login_as(client, "alice", "alice-pass")
    response = await client.get("/servers")
    assert response.status_code == 200
    assert "alice-only" in response.text
    assert "admin-only" not in response.text

    dashboard = await client.get("/")
    assert "alice-only" in dashboard.text
    assert "admin-only" not in dashboard.text


async def test_isolation_edit_delete_pay_foreign_entity_404(client, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
        alice = await session.scalar(select(User).where(User.username == "alice"))
        alice_id = alice.id
    admin_server_id = await _add_server_owned(session_factory, "admin-server", admin_id)
    await _add_server_owned(session_factory, "alice-server", alice_id)

    await _login_as(client, "alice", "alice-pass")
    edit = await client.get(f"/servers/{admin_server_id}/edit")
    assert edit.status_code == 404
    delete = await client.post(f"/servers/{admin_server_id}/delete")
    assert delete.status_code == 404
    pay = await client.post(f"/servers/{admin_server_id}/pay", data={"next": "/servers"})
    assert pay.status_code == 404
    update = await client.post(
        f"/servers/{admin_server_id}", data={"name": "hacked", "amount_rub": 1}
    )
    assert update.status_code == 404

    async with session_factory() as session:
        server = await session.get(Server, admin_server_id)
        assert server is not None
        assert server.name == "admin-server"


async def test_isolation_creation_sets_owner(client, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    await _login_as(client, "alice", "alice-pass")

    response = await client.post(
        "/servers",
        data={
            "name": "my-gpu",
            "kind": "gpu",
            "provider": "Hetzner",
            "amount_rub": "5000",
            "period": "monthly",
            "next_due": "2026-10-01",
            "is_active": "on",
        },
    )
    assert response.status_code == 303
    response = await client.post(
        "/subscriptions",
        data={
            "name": "my-sub",
            "provider": "Netflix",
            "amount_rub": "900",
            "period": "monthly",
            "next_due": "2026-10-05",
            "status": "active",
            "auto_renewal": "on",
        },
    )
    assert response.status_code == 303
    response = await client.post(
        "/expenses",
        data={
            "title": "my-mouse",
            "amount_rub": "100",
            "paid_date": "2026-09-01",
        },
    )
    assert response.status_code == 303

    async with session_factory() as session:
        alice = await session.scalar(select(User).where(User.username == "alice"))
        server = await session.scalar(
            select(Server).where(Server.name == "my-gpu")
        )
        sub = await session.scalar(
            select(Subscription).where(Subscription.name == "my-sub")
        )
        expense = await session.scalar(
            select(OneTimeExpense).where(OneTimeExpense.title == "my-mouse")
        )
        assert server is not None and server.owner_id == alice.id
        assert sub is not None and sub.owner_id == alice.id
        assert expense is not None and expense.owner_id == alice.id


async def test_new_entity_forms_render(client):
    await _login(client)
    for path in ("/servers/new", "/subscriptions/new", "/expenses/new"):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'method="post"' in response.text


async def test_edit_entity_forms_render(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        server = Server(
            name="GPU-A",
            kind="gpu",
            provider="Hetzner",
            amount_rub=12000,
            period="monthly",
            next_due=date(2026, 9, 16),
            is_active=True,
            owner_id=admin.id,
        )
        sub = Subscription(
            name="Netflix",
            provider="Netflix",
            amount_rub=900,
            period="monthly",
            next_due=date(2026, 9, 20),
            status="active",
            owner_id=admin.id,
        )
        expense = OneTimeExpense(
            title="Мышь",
            amount_rub=100,
            paid_date=date(2026, 9, 1),
            owner_id=admin.id,
        )
        session.add_all([server, sub, expense])
        await session.commit()
        server_id, sub_id, expense_id = server.id, sub.id, expense.id
    await _login(client)
    for path in (
        f"/servers/{server_id}/edit",
        f"/subscriptions/{sub_id}/edit",
        f"/expenses/{expense_id}/edit",
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'method="post"' in response.text


async def test_nonexistent_page_404_html(client):
    response = await client.get("/nonexistent")
    assert response.status_code == 404
    assert "Страница не найдена" in response.text
    assert "404" in response.text


async def test_forbidden_403_html_page(client, session_factory):
    await _add_user(session_factory, "alice", "alice-pass")
    await _login_as(client, "alice", "alice-pass")
    response = await client.get("/users")
    assert response.status_code == 403
    assert "Доступ запрещён" in response.text


async def test_unauth_html_page_redirects_to_login(client):
    response = await client.get("/servers")
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_api_error_returns_json(client):
    response = await client.get("/api/unknown")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


async def test_unhandled_exception_500_html(app):
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "admin", "password": PASSWORD})
        response = await ac.get("/boom")
        assert response.status_code == 500
        assert "ошибка сервера" in response.text
        assert "RuntimeError" not in response.text