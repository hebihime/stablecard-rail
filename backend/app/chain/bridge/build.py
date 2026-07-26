"""Choosing a bridge, without becoming a registry.

§9.3 declined a bridge registry, and the reason still holds: a registry exists so
that *stored data* can name an implementation, which is why `issuers/` has one —
an intent carries a `provider_id` and something has to resolve it. No intent
carries a bridge. A funding intent's `bridge_ref` is the reference a bridge gave
us, not a choice a caller made, so nothing needs to look a bridge up after the
fact.

What phase 6 does need is a way for the *entry points* — the worker, the demo — to
build one of two implementations from configuration. That is this module: a
function, not a lookup table, and the mapping is exhaustive and local.

`BRIDGE_PROVIDER` defaults to `simulated`, and SPEC.md §5.2 is explicit about why:
a recorded walk-through of the funding pipeline must not be able to fail because
somebody else's testnet is down. The real adapter is opt-in, per run.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.chain.bridge.base import BridgeProvider
from app.chain.bridge.simulated import SimulatedBridge, SimulatedBridgeSettings
from app.chain.bridge.wormhole.adapter import WormholeBridge
from app.chain.bridge.wormhole.client import WormholescanClient
from app.chain.bridge.wormhole.config import get_wormhole_settings
from app.chain.bridge.wormhole.redeemer import Redeemer
from app.chain.config import get_solana_settings
from app.chain.evm.config import get_evm_settings
from app.chain.evm.rpc import EvmRpcClient
from app.chain.evm.signer import LocalPrivateKeySigner
from app.chain.rpc import SolanaRpcClient
from app.chain.signer import LocalKeypairSigner

__all__ = ["BridgeChoice", "BridgeSelection", "build_bridge"]

logger = logging.getLogger(__name__)


class BridgeChoice(StrEnum):
    SIMULATED = "simulated"
    WORMHOLE = "wormhole"


class BridgeSelection(BaseSettings):
    """Which bridge a process uses, under its own prefix (§7.4's rule).

    Not in `app/core/config.py`: core does not know what bridges exist, and a
    field there would be a change to core for every implementation added.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="BRIDGE_",
        extra="ignore",
        case_sensitive=False,
    )

    #: The simulator, so a demo cannot fail because of a third party (SPEC.md §5.2).
    provider: BridgeChoice = BridgeChoice.SIMULATED


def build_bridge(choice: BridgeChoice | None = None) -> BridgeProvider:
    """The bridge this process should use.

    Raises:
        SignerError: the real bridge was asked for and a key it needs is missing.
            Eagerly, and by design — §8.11's rule is to validate early when the
            failure would otherwise be misattributed, and a worker that starts
            without a redeemer key would look healthy until a transfer needed
            delivering.
    """
    selected = choice or BridgeSelection().provider
    if selected is BridgeChoice.SIMULATED:
        return SimulatedBridge.from_settings(SimulatedBridgeSettings())

    solana = get_solana_settings()
    wormhole = get_wormhole_settings()
    evm = get_evm_settings()

    logger.info(
        "using the real bridge: %s -> chain %s, redeeming on chain id %s",
        solana.rpc_url,
        wormhole.destination_chain_id,
        evm.chain_id,
    )
    return WormholeBridge(
        solana=SolanaRpcClient(rpc_url=solana.rpc_url, timeout=solana.request_timeout_seconds),
        guardians=WormholescanClient(
            api_url=wormhole.api_url, timeout=wormhole.request_timeout_seconds
        ),
        redeemer=Redeemer(
            EvmRpcClient(rpc_url=evm.rpc_url, timeout=evm.request_timeout_seconds),
            signer=LocalPrivateKeySigner.from_env_value(evm.redeemer_private_key),
            token_bridge=wormhole.destination_token_bridge,
            chain_id=evm.chain_id,
        ),
        signer=LocalKeypairSigner.from_env_value(solana.deposit_keypair),
        settings=wormhole,
        mint=solana.usdc_mint,
        decimals=solana.usdc_decimals,
    )
