"""The destination chain's configuration (docs/ARCHITECTURE.md §7.4's rule again).

`EVM_` is the chain, not the protocol: an endpoint, a chain id, and the key that
pays gas to deliver a transfer. Wormhole's own facts — programs, contracts, chain
ids — live under `WORMHOLE_` in `app/chain/bridge/wormhole/config.py`, so pointing
this pipeline at a different EVM testnet is a change to this file's variables and
to none of that one's.

**The key here is a testnet key and it lives in the environment.** No default:
`EVM_REDEEMER_PRIVATE_KEY` empty means "not configured", and the signer refuses to
build rather than inventing one whose address nobody funded (`chain/evm/signer.py`,
the same stance as `LocalKeypairSigner`).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BSC_TESTNET_CHAIN_ID", "BSC_TESTNET_RPC_URL", "EvmSettings", "get_evm_settings"]

#: BSC testnet. Public, keyless, and rate-limited — the same bargain as the public
#: Solana devnet endpoint, and worth remembering when a live run answers 429.
BSC_TESTNET_RPC_URL = "https://data-seed-prebsc-1-s1.bnbchain.org:8545"

#: EIP-155 chain id for BSC testnet. Signed into every transaction, so a wrong one
#: is rejected by the node rather than sent to the wrong chain.
BSC_TESTNET_CHAIN_ID = 97


class EvmSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="EVM_",
        extra="ignore",
        case_sensitive=False,
    )

    #: Defaults to a testnet, for the reason `SolanaSettings.rpc_url` does: an
    #: environment that forgot to configure anything must not reach a real chain.
    rpc_url: str = BSC_TESTNET_RPC_URL
    chain_id: int = BSC_TESTNET_CHAIN_ID
    #: Pays gas to redeem transfers. Never holds the funds — a Wormhole transfer
    #: credits the recipient encoded in the VAA, whoever submits it.
    redeemer_private_key: str = ""
    request_timeout_seconds: float = 20.0
    #: How long to wait for a redemption's receipt before reporting the transfer
    #: still in flight. Not a failure when it elapses: the transaction may yet be
    #: mined, and `status()` asks again on the next pass.
    receipt_timeout_seconds: float = 120.0

    @field_validator("rpc_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_evm_settings() -> EvmSettings:
    return EvmSettings()
