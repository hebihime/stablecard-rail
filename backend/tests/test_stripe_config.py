"""Stripe Issuing's settings belong to Stripe Issuing's package (phase 4).

`tests/test_module_boundaries.py` already asserts the *structural* half — that
`app/core/config.py` declares nothing named after a provider, and that no adapter
imports the app's settings. These tests cover the half a structural check cannot
see: that the variables an operator sets actually reach the adapter, and that the
defaults are the safe ones.
"""

from __future__ import annotations

import pytest

from app.issuers.stripe_issuing.config import StripeIssuingSettings, get_stripe_issuing_settings
from app.issuers.stripe_issuing.signing import DEFAULT_TOLERANCE_SECONDS


def settings() -> StripeIssuingSettings:
    """Settings that ignore any `.env` on disk.

    Otherwise these tests would pass or fail depending on whether the machine
    running them happens to have Stripe credentials configured. `_env_file=None`
    is pydantic-settings' documented way to disable file loading; it is absent
    from the generated `__init__` signature, which is the whole of the ignore
    below.
    """
    return StripeIssuingSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_env_prefix_names_the_package() -> None:
    # Two adapters sharing a prefix would read each other's variables.
    assert StripeIssuingSettings.model_config["env_prefix"] == "STRIPE_ISSUING_"


def test_credentials_default_to_empty_rather_than_to_a_value() -> None:
    # An adapter that cannot be *built* without credentials takes `GET /providers`
    # down for every other provider, because `registry.describe()` instantiates
    # them all. So the emptiness is deliberate and the failure is deferred to the
    # first call that needs a key.
    built = settings()
    assert built.api_key == ""
    assert built.webhook_secret == ""


def test_the_defaults_are_stripes_documented_ones() -> None:
    built = settings()
    assert built.api_base_url == "https://api.stripe.com/v1"
    assert built.signature_tolerance_seconds == DEFAULT_TOLERANCE_SECONDS
    assert built.request_timeout_seconds > 0


def test_the_api_version_is_unpinned_by_default() -> None:
    # Deliberate: the only version-dependent behaviour this adapter cares about is
    # whether a lapsed authorization reports `expired` or `reversed`, and it treats
    # both as a reversal. Pinning to a version string that cannot be checked
    # against the live API from here would risk 400-ing every call.
    assert settings().api_version == ""


def test_every_setting_is_reachable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The names here are the contract with `.env.example`. Changing one is an
    # operator-visible change.
    monkeypatch.setenv("STRIPE_ISSUING_API_BASE_URL", "https://api.stripe.test/v1")
    monkeypatch.setenv("STRIPE_ISSUING_API_KEY", "sk_test_from_env")
    monkeypatch.setenv("STRIPE_ISSUING_WEBHOOK_SECRET", "whsec_from_env")
    monkeypatch.setenv("STRIPE_ISSUING_API_VERSION", "2025-03-31.basil")
    monkeypatch.setenv("STRIPE_ISSUING_REQUEST_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("STRIPE_ISSUING_SIGNATURE_TOLERANCE_SECONDS", "60")

    built = settings()

    assert built.api_base_url == "https://api.stripe.test/v1"
    assert built.api_key == "sk_test_from_env"
    assert built.webhook_secret == "whsec_from_env"
    assert built.api_version == "2025-03-31.basil"
    assert built.request_timeout_seconds == 3.5
    assert built.signature_tolerance_seconds == 60


def test_an_unprefixed_variable_is_not_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # `STRIPE_API_KEY` would be the obvious name to reach for, and it is not this
    # adapter's: the prefix is what keeps the guard in test_module_boundaries.py
    # meaningful, and what would keep a future `stripe_connect` adapter separate.
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_wrong_variable")
    assert settings().api_key == ""


def test_settings_are_read_once_per_process() -> None:
    # Cached, like every other adapter's: settings are read at first use, not at
    # import, and not per call.
    assert get_stripe_issuing_settings() is get_stripe_issuing_settings()


def test_unrelated_variables_do_not_break_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    # `extra="ignore"`: one `.env` serves the whole project, so this class sees
    # every other adapter's variables too and must not object to them.
    monkeypatch.setenv("STRIPE_ISSUING_SOMETHING_WE_DO_NOT_KNOW", "1")
    assert settings().api_key == ""
