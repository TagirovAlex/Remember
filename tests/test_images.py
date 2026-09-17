from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi import UploadFile
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import ensure_admin_user
from app.config import settings
from app.db import get_session
from app.factory import create_app
from app.models import Base, Server, User
from app.security import hash_password
from app.services.images import (
    MAX_BYTES,
    delete_image,
    image_path,
    images_dir,
    save_image,
)

PASSWORD = "secret-pass"


def _png_bytes(size: tuple[int, int] = (100, 100)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (200, 30, 40)).save(buf, "PNG")
    return buf.getvalue()


def _webp_bytes(size: tuple[int, int] = (100, 100)) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, (10, 200, 30, 128)).save(buf, "WEBP")
    return buf.getvalue()


def _upload(data: bytes, name: str = "pic.png") -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(data))


async def _logged_in_photos(tmp_path, monkeypatch) -> Path:
    """Point the images directory at a temp folder; return its path."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "remember.db"))
    return images_dir()


# ---------- save_image / delete_image / image_path ----------


async def test_save_image_valid_png(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    name = await save_image(_upload(_png_bytes()), "server", 1)
    assert name == "server_1.webp"
    path = image_path(name)
    assert path.is_file()
    with Image.open(path) as img:
        assert img.size == (100, 100)


async def test_save_image_too_large_raises(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Файл больше 2 МБ"):
        await save_image(_upload(b"x" * (MAX_BYTES + 1)), "server", 1)


async def test_save_image_non_image_raises(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Недопустимый формат изображения"):
        await save_image(_upload(b"this is not an image"), "server", 1)


async def test_save_image_truncated_png_raises_value_error(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    full = _png_bytes((200, 200))
    with pytest.raises(ValueError, match="Некорректное изображение"):
        await save_image(_upload(full[: len(full) // 2], "cut.png"), "server", 1)


async def test_save_image_rejects_unsupported_format(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    buf = BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "BMP")
    with pytest.raises(ValueError, match="Недопустимый формат изображения"):
        await save_image(_upload(buf.getvalue(), "pic.bmp"), "server", 1)


async def test_save_image_crops_to_square(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    name = await save_image(_upload(_png_bytes((400, 200))), "server", 1)
    with Image.open(image_path(name)) as img:
        assert img.size == (200, 200)


async def test_save_image_resizes_large_to_max(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    name = await save_image(_upload(_png_bytes((800, 600))), "server", 1)
    with Image.open(image_path(name)) as img:
        assert img.size == (256, 256)


async def test_save_image_keeps_small_as_is(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    name = await save_image(_upload(_png_bytes((100, 80))), "server", 1)
    with Image.open(image_path(name)) as img:
        assert img.size == (80, 80)


async def test_save_image_replaces_existing_file(tmp_path, monkeypatch):
    folder = await _logged_in_photos(tmp_path, monkeypatch)
    await save_image(_upload(_png_bytes((100, 100))), "server", 1)
    await save_image(_upload(_png_bytes((300, 300))), "server", 1)
    name = "server_1.webp"
    assert list(folder.glob("server_1.webp")) == [folder / name]
    with Image.open(folder / name) as img:
        assert img.size == (256, 256)


async def test_delete_image_removes_file(tmp_path, monkeypatch):
    folder = await _logged_in_photos(tmp_path, monkeypatch)
    name = await save_image(_upload(_png_bytes()), "server", 1)
    assert (folder / name).is_file()
    await delete_image("server", 1)
    assert not (folder / name).exists()


async def test_image_path_valid(tmp_path, monkeypatch):
    await _logged_in_photos(tmp_path, monkeypatch)
    assert image_path("server_12.webp") == images_dir() / "server_12.webp"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "server.webp",
        "server_1.png",
        "Server_1.webp",
        "server_-1.webp",
        "a/../server_1.webp",
    ],
)
async def test_image_path_rejects_invalid_names(tmp_path, monkeypatch, bad):
    await _logged_in_photos(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        image_path(bad)


# ---------- web routes ----------


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
async def app(engine, session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "remember.db"))

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


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login", data={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 303


def _server_form(name: str = "gpu-x") -> dict[str, str]:
    return {
        "name": name,
        "kind": "gpu",
        "provider": "Hetzner",
        "amount_rub": "5000",
        "period": "monthly",
        "next_due": "2026-10-01",
        "is_active": "on",
    }


async def _add_user(session_factory, username: str, password: str) -> int:
    async with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role="user",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _add_server(session_factory, owner_id: int, name: str) -> int:
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


async def _server_with_image(session_factory, owner_id: int) -> tuple[int, str]:
    server_id = await _add_server(session_factory, owner_id, "GPU-img")
    name = f"server_{server_id}.webp"
    folder = images_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(_webp_bytes())
    async with session_factory() as session:
        server = await session.get(Server, server_id)
        server.image = name
        await session.commit()
    return server_id, name


async def test_image_serve_requires_auth(client):
    response = await client.get("/images/server/1")
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_image_serve_own_200(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
    server_id, _ = await _server_with_image(session_factory, admin_id)
    await _login(client)
    response = await client.get(f"/images/server/{server_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "max-age=3600"


async def test_image_serve_foreign_404(client, session_factory):
    alice_id = await _add_user(session_factory, "alice", "alice-pass")
    server_id, _ = await _server_with_image(session_factory, alice_id)
    await _login(client)
    response = await client.get(f"/images/server/{server_id}")
    assert response.status_code == 404


async def test_image_serve_missing_404(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
    server_id = await _add_server(session_factory, admin_id, "GPU-noimg")
    await _login(client)
    response = await client.get(f"/images/server/{server_id}")
    assert response.status_code == 404


async def test_image_serve_bad_type_404(client):
    await _login(client)
    response = await client.get("/images/whatever/1")
    assert response.status_code == 404


async def test_upload_image_on_create_sets_image(client, session_factory):
    await _login(client)
    response = await client.post(
        "/servers",
        data=_server_form("my-gpu"),
        files={"image": ("pic.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 303
    async with session_factory() as session:
        server = await session.scalar(select(Server).where(Server.name == "my-gpu"))
        assert server.image == f"server_{server.id}.webp"
        assert (images_dir() / server.image).is_file()


async def test_upload_replaces_old_file(client, session_factory):
    await _login(client)
    first = await client.post(
        "/servers",
        data=_server_form("gpu-replace"),
        files={"image": ("a.png", _png_bytes((100, 100)), "image/png")},
    )
    assert first.status_code == 303
    async with session_factory() as session:
        server = await session.scalar(
            select(Server).where(Server.name == "gpu-replace")
        )
        server_id = server.id
        name = server.image
    with Image.open(images_dir() / name) as img:
        assert img.size == (100, 100)

    second = await client.post(
        f"/servers/{server_id}",
        data=_server_form("gpu-replace"),
        files={"image": ("b.png", _png_bytes((300, 300)), "image/png")},
    )
    assert second.status_code == 303
    async with session_factory() as session:
        server = await session.get(Server, server_id)
        assert server.image == name
    assert list(images_dir().glob(f"server_{server_id}.webp")) == [
        images_dir() / name
    ]
    with Image.open(images_dir() / name) as img:
        assert img.size == (256, 256)


async def test_delete_image_button_clears(client, session_factory):
    async with session_factory() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        admin_id = admin.id
    server_id, name = await _server_with_image(session_factory, admin_id)
    await _login(client)
    response = await client.post(f"/servers/{server_id}/image/delete")
    assert response.status_code == 303
    assert "Картинка удалена" in unquote(response.headers["location"])
    async with session_factory() as session:
        server = await session.get(Server, server_id)
        assert server.image is None
    assert not (images_dir() / name).exists()


async def test_upload_too_large_via_web_redirects_with_error(client, session_factory):
    await _login(client)
    response = await client.post(
        "/servers",
        data=_server_form("gpu-big"),
        files={"image": ("big.png", b"x" * (MAX_BYTES + 1), "image/png")},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    async with session_factory() as session:
        server = await session.scalar(select(Server).where(Server.name == "gpu-big"))
        assert server is not None
        assert server.image is None


async def test_upload_bad_format_via_web_redirects_with_error(client, session_factory):
    await _login(client)
    response = await client.post(
        "/servers",
        data=_server_form("gpu-bad"),
        files={"image": ("notes.txt", b"just text", "text/plain")},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    async with session_factory() as session:
        server = await session.scalar(select(Server).where(Server.name == "gpu-bad"))
        assert server.image is None


async def test_upload_truncated_image_redirects_with_error(client, session_factory):
    await _login(client)
    full = _png_bytes((200, 200))
    response = await client.post(
        "/servers",
        data=_server_form("gpu-cut"),
        files={"image": ("cut.png", full[: len(full) // 2], "image/png")},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    async with session_factory() as session:
        server = await session.scalar(select(Server).where(Server.name == "gpu-cut"))
        assert server.image is None