"""Application settings.

Every value is env-supplied and documented in `.env.example`. No secret ever has
a default here; the defaults that do exist are local-development addresses,
thresholds, or — for the mock issuer only — a key to a fake provider that exists
solely in this process (see `evm_deposit_mock_webhook_secret`).
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

    # --- webhook signatures (SPEC.md §4) -----------------------------------
    #: Rejection window for signature timestamps, so a captured delivery cannot
    #: be replayed indefinitely.
    webhook_signature_tolerance_seconds: int = 300

    # --- issuers (SPEC.md §3) ----------------------------------------------
    #: HMAC key the in-process mock provider signs its own webhooks with. Not a
    #: credential to anything: the "provider" is a simulator in this repo. Real
    #: adapters (phases 3 and 4) get required settings with no defaults.
    evm_deposit_mock_webhook_secret: str = "dev-mock-webhook-secret-not-a-credential"


@lru_cache
def get_settings() -> Settings:
    return Settings()
