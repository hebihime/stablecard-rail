"""Application settings.

Every value is env-supplied and documented in `.env.example`. No secret has a
default here; the defaults that do exist are local-development addresses or
thresholds.

**Nothing provider-specific belongs in this file.** Each adapter declares its own
settings class with its own env prefix (`app/issuers/lithic/config.py`), because a
`lithic_api_key` field here made "adding an issuer is one adapter file plus one
registry entry" false in a way no import-graph test could see. Two tests in
`tests/test_module_boundaries.py` now hold that line: no field here may be named
after an adapter, and no adapter may read this module.
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

    # --- webhooks (SPEC.md §4) ---------------------------------------------
    #: How long a `(provider_id, event_id)` dedup claim is remembered in Redis.
    #: The ledger's unique index is the durable backstop once this expires.
    webhook_dedup_ttl_seconds: int = 86_400
    #: Delay before each successive handler retry. Its length is the attempt cap:
    #: an event that exhausts it is dead-lettered.
    webhook_retry_backoff_seconds: tuple[int, ...] = (2, 8, 32, 128, 512)
    # The signature receiving window is deliberately *not* here. Only adapters
    # read it, and it has to suit one provider's clock skew and retry schedule at
    # a time — so it lives in each adapter's own settings.

    # --- the funding engine (SPEC.md §5.2) ---------------------------------
    #: Self-transitions allowed per state before an intent is failed. The count
    #: lives on the intent, so the cap survives a restart.
    funding_max_retries: int = 5
    #: Route the bridge is asked for. Opaque labels a bridge either supports or
    #: does not (docs/ARCHITECTURE.md §9.2) — not an enum of chains.
    funding_source_chain: str = "solana-devnet"
    funding_destination_chain: str = "gnosis-chiado"
    #: Where a bridge order is delivered when the issuer is a fiat rail and has
    #: no on-chain address of its own (§9.3). Not a secret: an address.
    funding_settlement_address: str = ""

    # --- the reconciler (SPEC.md §5.3) ------------------------------------
    #: How long a state must be unchanged before an intent counts as stuck.
    reconciler_stuck_after_seconds: int = 120
    #: The backoff doubles per retry from the threshold above; this caps it, so a
    #: much-retried intent is still looked at eventually.
    reconciler_max_backoff_seconds: int = 3_600
    #: Intents examined per pass.
    reconciler_batch_limit: int = 50

    # --- the worker loop ---------------------------------------------------
    #: How long a hop is left alone before its *first* attempt. Retries are the
    #: reconciler's business and are paced by its backoff instead.
    worker_first_attempt_after_seconds: float = 3.0
    #: Seconds between passes when the worker runs as a loop rather than --once.
    worker_interval_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
