"""The real bridge's settings, and the destination chain's.

Two prefixes rather than one, because they answer to different owners: `WORMHOLE_`
is the protocol (programs, contracts, chain ids — facts about Wormhole), and `EVM_`
is the chain we redeem on (an RPC endpoint, a chain id, a key that pays gas). A
second route to a different EVM testnet would change the second block and none of
the first.

Every test passes `_env_file=None`, for the phase-4 reason in
`tests/test_stripe_config.py`: without it these read the developer's real `.env`
and start passing or failing on what happens to be configured.
"""

from __future__ import annotations

import pytest

from app.chain.bridge.wormhole.config import (
    BSC_TESTNET_TOKEN_BRIDGE,
    SOLANA_DEVNET_CORE_BRIDGE,
    SOLANA_DEVNET_TOKEN_BRIDGE,
    WORMHOLE_CHAIN_BSC,
    WORMHOLE_CHAIN_SOLANA,
    WormholeSettings,
    get_wormhole_settings,
)
from app.chain.evm.config import EvmSettings, get_evm_settings


def test_it_defaults_to_the_verified_testnet_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # These four values are the route docs/ARCHITECTURE.md §10.1 established by
    # probing the chains. Defaulting to them means a demo needs no configuration,
    # and — as with the Solana RPC URL — that omission cannot reach mainnet.
    for name in ("WORMHOLE_CORE_PROGRAM", "WORMHOLE_TOKEN_BRIDGE_PROGRAM", "WORMHOLE_API_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = WormholeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.core_program == SOLANA_DEVNET_CORE_BRIDGE
    assert settings.token_bridge_program == SOLANA_DEVNET_TOKEN_BRIDGE
    assert settings.destination_token_bridge == BSC_TESTNET_TOKEN_BRIDGE
    assert "testnet" in settings.api_url
    assert (settings.source_chain_id, settings.destination_chain_id) == (
        WORMHOLE_CHAIN_SOLANA,
        WORMHOLE_CHAIN_BSC,
    )


def test_every_field_answers_to_the_wormhole_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORMHOLE_CORE_PROGRAM", "11111111111111111111111111111111")
    monkeypatch.setenv("WORMHOLE_TOKEN_BRIDGE_PROGRAM", "22222222222222222222222222222222")
    monkeypatch.setenv("WORMHOLE_DESTINATION_TOKEN_BRIDGE", "0x" + "ab" * 20)
    monkeypatch.setenv("WORMHOLE_API_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("WORMHOLE_SOURCE_CHAIN_ID", "7")
    monkeypatch.setenv("WORMHOLE_DESTINATION_CHAIN_ID", "8")
    monkeypatch.setenv("WORMHOLE_REQUEST_TIMEOUT_SECONDS", "3.5")

    settings = WormholeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.core_program == "11111111111111111111111111111111"
    assert settings.token_bridge_program == "22222222222222222222222222222222"
    assert settings.destination_token_bridge == "0x" + "ab" * 20
    assert settings.api_url == "http://127.0.0.1:9999"
    assert (settings.source_chain_id, settings.destination_chain_id) == (7, 8)
    assert settings.request_timeout_seconds == 3.5


def test_the_api_url_keeps_no_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every caller joins paths onto this. A trailing slash produces `//v1/...`,
    # which Wormholescan answers with a 404 that looks like a missing VAA.
    monkeypatch.setenv("WORMHOLE_API_URL", "https://api.testnet.wormholescan.io/")

    settings = WormholeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.api_url == "https://api.testnet.wormholescan.io"


def test_the_evm_side_defaults_to_bsc_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVM_RPC_URL", raising=False)
    monkeypatch.delenv("EVM_CHAIN_ID", raising=False)

    settings = EvmSettings(_env_file=None)  # type: ignore[call-arg]

    # 97 is BSC testnet. A chain id is not decoration: it is signed into every
    # transaction, so a wrong one is rejected by the node rather than misdelivered.
    assert settings.chain_id == 97
    assert "bnbchain" in settings.rpc_url or "bsc" in settings.rpc_url


def test_the_evm_key_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A default key would be a key somebody committed. Empty means "not
    # configured", and the signer refuses to build rather than inventing one.
    monkeypatch.delenv("EVM_REDEEMER_PRIVATE_KEY", raising=False)

    assert EvmSettings(_env_file=None).redeemer_private_key == ""  # type: ignore[call-arg]


def test_every_field_answers_to_the_evm_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVM_RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("EVM_CHAIN_ID", "31337")
    monkeypatch.setenv("EVM_REDEEMER_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("EVM_REQUEST_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("EVM_RECEIPT_TIMEOUT_SECONDS", "90")

    settings = EvmSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.rpc_url == "http://127.0.0.1:8545"
    assert settings.chain_id == 31337
    assert settings.redeemer_private_key == "0x" + "11" * 32
    assert settings.request_timeout_seconds == 4.5
    assert settings.receipt_timeout_seconds == 90


def test_no_unprefixed_variable_reaches_either(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RPC_URL", "http://elsewhere")
    monkeypatch.setenv("CHAIN_ID", "1")
    monkeypatch.setenv("API_URL", "http://elsewhere")

    assert EvmSettings(_env_file=None).chain_id == 97  # type: ignore[call-arg]
    assert "elsewhere" not in EvmSettings(_env_file=None).rpc_url  # type: ignore[call-arg]
    assert "elsewhere" not in WormholeSettings(_env_file=None).api_url  # type: ignore[call-arg]


def test_the_accessors_are_memoized() -> None:
    assert get_wormhole_settings() is get_wormhole_settings()
    assert get_evm_settings() is get_evm_settings()
