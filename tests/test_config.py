"""Environment settings tests: bind address and port from env/.env."""

from app.config import Settings


def test_host_port_defaults(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    config = Settings(_env_file=None)
    assert config.host == "0.0.0.0"
    assert config.port == 8000


def test_host_port_from_environment(monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8001")
    config = Settings(_env_file=None)
    assert config.host == "127.0.0.1"
    assert config.port == 8001
