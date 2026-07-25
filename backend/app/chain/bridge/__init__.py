"""Cross-chain transfer providers (SPEC.md §5.2).

Two implementations, one interface: `simulated` (here from phase 5, and the
default for the recorded demo) and a real protocol adapter in phase 6. The
funding engine holds a `BridgeProvider` and cannot tell them apart.

**There is no bridge registry**, unlike `issuers/`. The distinction is what the
identifier is for: a `provider_id` is written onto every funding intent and
ledger row and must be resolvable *from data*, whereas which bridge this process
uses is a deployment choice made once. So the engine takes a provider as an
argument and the composition root picks one — see docs/ARCHITECTURE.md §9.3.
"""

from app.chain.bridge.base import (
    BridgeError,
    BridgeOrder,
    BridgeProvider,
    BridgeRejectedError,
    BridgeStatus,
    BridgeTransfer,
    UnknownTransferError,
)

__all__ = [
    "BridgeError",
    "BridgeOrder",
    "BridgeProvider",
    "BridgeRejectedError",
    "BridgeStatus",
    "BridgeTransfer",
    "UnknownTransferError",
]
