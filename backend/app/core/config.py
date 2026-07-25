"""Application settings.

Every value is env-supplied and documented in `.env.example`. No secret ever has
a default here; the defaults that do exist are local-development addresses.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Running from `backend/` or from the repo root both find the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://stablecard:stablecard@localhost:5442/stablecard"
    redis_url: str = "redis://localhost:6389/0"

    default_currency: str = "USD"


@lru_cache
def get_settings() -> Settings:
    return Settings()
