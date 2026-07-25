"""The registry is what makes "a new issuer is one file" true (SPEC.md §3.1).

Everything downstream resolves adapters by `provider_id` through this module, so
these tests pin the properties the rest of the system relies on: registration is
explicit, lookup is by opaque string, instances are shared, and an unknown
provider is a distinguishable error rather than a `KeyError` surfacing as a 500.
"""

from __future__ import annotations

import pytest

from app.issuers import registry
from app.issuers.base import CardIssuerAdapter, FundingModel
from app.issuers.evm_deposit_mock import EvmDepositMockAdapter
from tests.support import StubIssuerAdapter


def test_the_mock_adapter_is_registered_by_importing_the_package() -> None:
    # The "registry entry" half of "one adapter file + one registry entry".
    assert "evm_deposit_mock" in registry.known_providers()


def test_get_adapter_returns_the_interface_type() -> None:
    adapter = registry.get_adapter("evm_deposit_mock")
    assert isinstance(adapter, CardIssuerAdapter)
    assert adapter.provider_id == "evm_deposit_mock"
    assert adapter.funding_model is FundingModel.CRYPTO_DEPOSIT


def test_adapters_are_singletons_per_process() -> None:
    # The mock holds simulator state in memory; two instances would mean a card
    # created through one is invisible to the other.
    assert registry.get_adapter("evm_deposit_mock") is registry.get_adapter("evm_deposit_mock")


def test_unknown_provider_raises_a_typed_error_naming_what_is_available() -> None:
    with pytest.raises(registry.UnknownProviderError) as caught:
        registry.get_adapter("wells_fargo")
    assert "wells_fargo" in str(caught.value)
    assert "evm_deposit_mock" in str(caught.value)
    assert caught.value.provider_id == "wells_fargo"


def test_registration_is_lazy_so_settings_are_read_at_first_use() -> None:
    calls: list[int] = []

    def factory() -> CardIssuerAdapter:
        calls.append(1)
        return StubIssuerAdapter()

    registry.register("stub_provider", factory)
    assert calls == []

    registry.get_adapter("stub_provider")
    registry.get_adapter("stub_provider")
    assert calls == [1], "the factory must be called once, then memoized"


def test_registering_a_duplicate_provider_id_is_refused() -> None:
    registry.register("stub_provider", StubIssuerAdapter)
    with pytest.raises(registry.DuplicateProviderError):
        registry.register("stub_provider", StubIssuerAdapter)


def test_replacing_a_provider_is_possible_but_must_be_explicit() -> None:
    registry.register("stub_provider", StubIssuerAdapter)
    first = registry.get_adapter("stub_provider")
    registry.register("stub_provider", StubIssuerAdapter, replace=True)
    assert registry.get_adapter("stub_provider") is not first


def test_known_providers_is_sorted_and_immutable() -> None:
    registry.register("stub_provider", StubIssuerAdapter)
    providers = registry.known_providers()
    assert isinstance(providers, tuple)
    assert list(providers) == sorted(providers)


def test_describe_exposes_the_funding_model_taxonomy() -> None:
    # Mobile and the demo need to know a provider's funding model without
    # importing any adapter (SPEC.md §3.2: proving both models are covered).
    described = dict(registry.describe())
    assert described["evm_deposit_mock"] is FundingModel.CRYPTO_DEPOSIT
    assert described["lithic"] is FundingModel.FIAT_RAIL


def test_the_lithic_adapter_is_registered_by_importing_the_package() -> None:
    # The whole "adding an issuer is one adapter file plus one registry entry" claim,
    # from the outside: the app imports `app.issuers` and both providers resolve.
    assert "lithic" in registry.known_providers()


def test_a_provider_with_no_credentials_configured_still_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registration is by factory, so an environment without Lithic credentials boots
    # and serves the mock provider; only a call that needs a key fails, and it names
    # the variable (docs/ARCHITECTURE.md §3.1).
    from app.issuers.lithic import adapter as lithic_adapter
    from app.issuers.lithic.config import LithicSettings

    monkeypatch.setattr(lithic_adapter, "get_lithic_settings", lambda: LithicSettings(api_key=""))
    registry.reset_instances()

    assert "lithic" in registry.known_providers()
    with pytest.raises(ValueError, match="LITHIC_API_KEY"):
        registry.get_adapter("lithic")


def test_registrations_survive_an_instance_reset_but_instances_do_not() -> None:
    first = registry.get_adapter("evm_deposit_mock")
    registry.reset_instances()
    assert "evm_deposit_mock" in registry.known_providers()
    assert registry.get_adapter("evm_deposit_mock") is not first


def test_the_mock_adapter_can_be_built_straight_from_settings() -> None:
    # The factory the package registers, called directly.
    adapter = EvmDepositMockAdapter.from_settings()
    assert isinstance(adapter, EvmDepositMockAdapter)
