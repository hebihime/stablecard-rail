"""Wormhole's own facts, under Wormhole's own prefix.

Programs, contracts and chain ids — the things that are true about the protocol
rather than about our deployment. The endpoint we redeem on is the *chain's*
business and lives under `EVM_` (`app/chain/evm/config.py`).

Every default in here was established by probing the chains rather than by reading
the tables — not because the tables are wrong, but because one word in them means
two things. Wormhole's supported-networks page marks Solana ✅ under **Testnet**
(which for Solana is the devnet cluster) and ❌ under **Devnet** (which is their own
Tilt local network). Searching that page for "devnet" therefore finds the wrong
answer first. See docs/ARCHITECTURE.md §10.1 for the probes and what each proved.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BSC_TESTNET_TOKEN_BRIDGE",
    "SOLANA_DEVNET_CORE_BRIDGE",
    "SOLANA_DEVNET_TOKEN_BRIDGE",
    "WORMHOLE_CHAIN_BSC",
    "WORMHOLE_CHAIN_SOLANA",
    "WORMHOLE_TESTNET_API_URL",
    "WormholeSettings",
    "get_wormhole_settings",
]

#: Wormhole's chain ids, which are **not** EVM chain ids (BSC is 4 here and 56/97
#: there). They are what a VAA carries, so they are what a transfer is addressed
#: with.
WORMHOLE_CHAIN_SOLANA = 1
WORMHOLE_CHAIN_BSC = 4

#: The two Solana programs from the docs' Testnet tab, both verified deployed and
#: executable on `api.devnet.solana.com` and absent from `api.testnet.solana.com`.
SOLANA_DEVNET_CORE_BRIDGE = "3u8hJUVTA4jH1wYAyUur7FFZVQ8H635K3tSHHF4ssjQ5"
SOLANA_DEVNET_TOKEN_BRIDGE = "DZnkkTmCiFWfYTfT41X3Rd1kDgozqzxWaHqsw6W4x2oe"

#: The Token Bridge on BSC testnet — where a transfer is redeemed, and where
#: `wrappedAsset(1, <devnet USDC>)` already answers with an attested token.
BSC_TESTNET_TOKEN_BRIDGE = "0x9dcF9D205C9De35334D646BeE44b2D2859712A09"

#: Wormholescan's testnet deployment, which serves signed VAAs over plain HTTP.
#: That is what makes this integration possible without the TypeScript SDK.
WORMHOLE_TESTNET_API_URL = "https://api.testnet.wormholescan.io"


class WormholeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="WORMHOLE_",
        extra="ignore",
        case_sensitive=False,
    )

    core_program: str = SOLANA_DEVNET_CORE_BRIDGE
    token_bridge_program: str = SOLANA_DEVNET_TOKEN_BRIDGE
    destination_token_bridge: str = BSC_TESTNET_TOKEN_BRIDGE
    api_url: str = WORMHOLE_TESTNET_API_URL
    source_chain_id: int = WORMHOLE_CHAIN_SOLANA
    destination_chain_id: int = WORMHOLE_CHAIN_BSC
    request_timeout_seconds: float = 20.0

    @field_validator("api_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        # Callers join paths onto this, and `//v1/...` comes back as a 404 that
        # reads exactly like a VAA the guardians have not signed yet.
        return value.rstrip("/")


@lru_cache
def get_wormhole_settings() -> WormholeSettings:
    return WormholeSettings()
