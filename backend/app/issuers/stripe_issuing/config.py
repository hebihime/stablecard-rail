"""Stripe Issuing's configuration, owned by Stripe Issuing's adapter.

The prefix is `STRIPE_ISSUING_`, matching the package name, which is what
`tests/test_module_boundaries.py::test_each_adapter_that_needs_configuration_declares_its_own`
requires: two adapters sharing a prefix would read each other's variables.
Nothing in `app/core/` knows this provider exists, and the same test suite fails
if that stops being true (docs/ARCHITECTURE.md §7.4).

**Test mode is a property of the key, not of the URL.** Lithic has a separate
sandbox host, so `LithicSettings.api_base_url` can default somewhere harmless and
a misconfigured environment cannot reach production by omission. Stripe has one
host and decides test-vs-live from the `sk_test_` / `sk_live_` prefix on the key
itself, so that defence is unavailable here. `client.checked_api_key` provides it
instead, by refusing a live key outright — this project is sandbox-only by
construction (SPEC.md §2), and a live Issuing key would move real money.

The `.env` locations are repeated rather than imported from `core/`: a two-entry
tuple is a cheaper duplication than a shared module every adapter must depend on.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.issuers.stripe_issuing.signing import DEFAULT_TOLERANCE_SECONDS

__all__ = ["StripeIssuingSettings", "get_stripe_issuing_settings"]


class StripeIssuingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Running from `backend/` or from the repo root both find the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="STRIPE_ISSUING_",
        extra="ignore",
        case_sensitive=False,
    )

    api_base_url: str = "https://api.stripe.com/v1"
    #: A real credential, so it defaults to empty rather than to a value. Nothing
    #: validates it here: `registry.describe()` builds every registered adapter to
    #: report its funding model, and `GET /providers` calls that, so an adapter
    #: that refused to *exist* without credentials would take a route down for the
    #: providers that are configured (docs/ARCHITECTURE.md §8.6).
    api_key: str = ""
    webhook_secret: str = ""
    #: Pins the `Stripe-Version` header when set. Left empty by default: the one
    #: version-dependent behaviour this adapter cares about is whether a lapsed
    #: authorization reports `expired` or `reversed`, and it treats both as a
    #: reversal — so a pin buys nothing, and pinning to a version string that
    #: cannot be checked against the live API risks 400-ing every call.
    api_version: str = ""
    request_timeout_seconds: float = 15.0
    #: Rejection window for a delivery's signature timestamp. Per-adapter rather
    #: than global: the window has to suit *this* provider's clock and retry
    #: schedule, and two providers need not agree.
    signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS


@lru_cache
def get_stripe_issuing_settings() -> StripeIssuingSettings:
    return StripeIssuingSettings()
