from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from the environment.

    Holds secrets plus the few bootstrap values used before the database is
    available; per-installation and per-user options live in the settings table.
    """

    bot_token: str = ""
    admin_password: str = ""
    secret_key: str = ""
    database_path: str = "data/remember.db"
    tz: str = "Europe/Moscow"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()


settings = get_settings()