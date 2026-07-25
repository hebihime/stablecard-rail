"""Lithic's configuration, owned by Lithic's adapter.

`app/core/config.py` used to carry a `lithic_api_key` field, and a
`stripe_issuing_*` one would have followed in phase 4. That made "adding an
issuer is one adapter file plus one registry entry" quietly false — and worse, it
was invisible to `tests/test_module_boundaries.py`, because the coupling ran
adapter -> `get_settings()` -> a field named after the adapter, never as an import
of the adapter. A dead `evm_deposit_mock_webhook_secret` sat in `core/`,
`.env.example` and `docker-compose.yml` read by nobody, which is what that kind of
invisible coupling looks like once the adapter it was named for is gone.

So each adapter declares its own settings, with its own env prefix. The variable
names are unchanged — `LITHIC_API_KEY` is still `LITHIC_API_KEY` — because the
prefix carries the adapter's name. What changes is that `core/` no longer knows
this provider exists, which is now enforced (see
`test_core_config_declares_no_adapter_specific_fields` and
`test_no_adapter_reads_core_settings`).

The `.env` locations are repeated rather than imported from `core/`: a two-entry
tuple is a cheaper duplication than a shared module every adapter must depend on.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.issuers.lithic.signing import DEFAULT_TOLERANCE_SECONDS

__all__ = ["LithicSettings", "get_lithic_settings"]


class LithicSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Running from `backend/` or from the repo root both find the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="LITHIC_",
        extra="ignore",
        case_sensitive=False,
    )

    #: Defaults to the sandbox, so a misconfigured environment cannot reach
    #: production by omission.
    api_base_url: str = "https://sandbox.lithic.com/v1"
    #: A real credential, so it defaults to empty rather than to a value: the
    #: adapter refuses to build without it, which fails at the one call that needs
    #: it instead of on every import.
    api_key: str = ""
    webhook_secret: str = ""
    request_timeout_seconds: float = 15.0
    #: Rejection window for a delivery's signature timestamp. Per-adapter rather
    #: than global: the window has to suit *this* provider's clock and retry
    #: schedule, and two providers need not agree.
    signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS


@lru_cache
def get_lithic_settings() -> LithicSettings:
    return LithicSettings()
