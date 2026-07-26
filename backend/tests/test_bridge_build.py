"""Choosing a bridge, and defaulting to the one that cannot fail on us.

The default matters more than it looks: SPEC.md §5.2 keeps the simulator in place
after phase 6 so a recorded demo cannot break because somebody else's testnet is
down. A configuration mistake that silently selected the real protocol would make
every walk-through depend on Wormhole's guardians and a BSC faucet.

Every test passes `_env_file=None` for the phase-4 reason: without it these read
the developer's real `.env`, and this developer has the real bridge configured.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from solders.keypair import Keypair

from app.chain.bridge.build import BridgeChoice, BridgeSelection, build_bridge
from app.chain.bridge.simulated import SimulatedBridge
from app.chain.bridge.wormhole.adapter import WormholeBridge
from app.chain.bridge.wormhole.config import get_wormhole_settings
from app.chain.config import get_solana_settings
from app.chain.evm.config import get_evm_settings
from app.chain.signer import SignerError

#: A throwaway devnet keypair, derived here so that nothing in this file could be
#: mistaken for a credential. The JSON-array form is what `solana-keygen` writes,
#: and `keypair_from_env_value` takes it as readily as base58.
DEVNET_KEYPAIR = json.dumps(list(bytes(Keypair.from_seed(bytes([12]) * 32))))

#: Likewise for the destination chain. Never funded, never used anywhere.
REDEEMER_KEY = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def _forget_settings() -> Iterator[None]:
    """Clear the memoized settings around every test in this file.

    `build_bridge` reads three `lru_cache`d accessors, so a test that
    monkeypatches the environment leaves them holding its values. Clearing
    afterwards as well as before is the difference between a self-contained file
    and one that makes an unrelated test fail depending on ordering.
    """
    _clear_caches()
    yield
    _clear_caches()


def test_it_defaults_to_the_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDGE_PROVIDER", raising=False)

    assert BridgeSelection(_env_file=None).provider is BridgeChoice.SIMULATED  # type: ignore[call-arg]


def test_the_real_bridge_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_PROVIDER", "wormhole")

    assert BridgeSelection(_env_file=None).provider is BridgeChoice.WORMHOLE  # type: ignore[call-arg]


def test_a_name_that_is_not_a_bridge_is_refused_rather_than_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typo must not silently fall back to the simulator: a run that thinks it is
    # exercising the real protocol and is not would be worse than a crash.
    monkeypatch.setenv("BRIDGE_PROVIDER", "wormhol")

    with pytest.raises(ValueError):
        BridgeSelection(_env_file=None)  # type: ignore[call-arg]


def test_building_the_simulator_needs_no_credentials() -> None:
    assert isinstance(build_bridge(BridgeChoice.SIMULATED), SimulatedBridge)


def test_building_the_real_bridge_assembles_the_whole_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLANA_DEPOSIT_KEYPAIR", DEVNET_KEYPAIR)
    monkeypatch.setenv("EVM_REDEEMER_PRIVATE_KEY", REDEEMER_KEY)
    _clear_caches()

    bridge = build_bridge(BridgeChoice.WORMHOLE)

    assert isinstance(bridge, WormholeBridge)
    assert bridge.bridge_id == "wormhole"


def test_the_real_bridge_refuses_to_build_without_a_redeemer_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Eagerly, per §8.11: a worker that started without this would look healthy
    # until the first transfer needed delivering, and the failure would be
    # reported as a bridge problem hours after the misconfiguration.
    monkeypatch.setenv("SOLANA_DEPOSIT_KEYPAIR", DEVNET_KEYPAIR)
    monkeypatch.setenv("EVM_REDEEMER_PRIVATE_KEY", "")
    _clear_caches()

    with pytest.raises(SignerError, match="EVM_REDEEMER_PRIVATE_KEY"):
        build_bridge(BridgeChoice.WORMHOLE)


def test_the_real_bridge_refuses_to_build_without_a_solana_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLANA_DEPOSIT_KEYPAIR", "")
    monkeypatch.setenv("EVM_REDEEMER_PRIVATE_KEY", REDEEMER_KEY)
    _clear_caches()

    with pytest.raises(SignerError, match="SOLANA_DEPOSIT_KEYPAIR"):
        build_bridge(BridgeChoice.WORMHOLE)


def _clear_caches() -> None:
    """The settings accessors are memoized, which is what makes them cheap and
    what makes them stale under monkeypatch."""
    get_solana_settings.cache_clear()
    get_wormhole_settings.cache_clear()
    get_evm_settings.cache_clear()
