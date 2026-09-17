from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings; secrets come from environment only."""

    bot_token: str = ""
    proxy_url: str = ""
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