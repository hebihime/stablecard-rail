"""Solana's configuration, owned by `chain/` (docs/ARCHITECTURE.md §7.4).

Same rule as the issuer adapters: a component's settings live with the component,
under a prefix nothing else answers to. `app/core/config.py` holds this service's
own knobs; it does not hold a `solana_rpc_url` any more than it holds a
`lithic_api_key`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["SolanaSettings", "get_solana_settings"]

#: Circle's USDC on devnet. Six decimals, like USDC everywhere.
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


class SolanaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Running from `backend/` or from the repo root both find the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="SOLANA_",
        extra="ignore",
        case_sensitive=False,
    )

    #: Defaults to devnet, so a misconfigured environment cannot reach mainnet by
    #: omission — the same reasoning as Lithic's sandbox default.
    rpc_url: str = "https://api.devnet.solana.com"
    #: The token this pipeline watches for. Not a credential; an address.
    usdc_mint: str = USDC_DEVNET_MINT
    #: USDC's decimals. Configurable because a different mint has different ones,
    #: not because this one changes.
    usdc_decimals: int = 6
    #: Signatures fetched per poll. The RPC caps this at 1000; small pages keep a
    #: single poll's work bounded and the cursor moving.
    page_limit: int = 25
    request_timeout_seconds: float = 20.0


@lru_cache
def get_solana_settings() -> SolanaSettings:
    return SolanaSettings()
