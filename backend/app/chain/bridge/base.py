"""The bridge abstraction (SPEC.md §5.2) — what a cross-chain transfer looks like.

Deliberately two calls: **submit an order** and **ask about it**. That is the
shape every bridge and aggregator actually offers, because a cross-chain transfer
cannot be synchronous — the source chain must finalize before the destination
chain can be told anything. Anything richer (quotes, route selection, fee
estimation) belongs inside an implementation until a caller needs it.

Two rules carried over from `issuers/base.py`, for the same reasons:

* **Money is integer minor units**, and a bridged amount arrives *net of fees*,
  so `amount_in` and `amount_out` are separate fields rather than one amount that
  changes meaning. The engine funds a card with what arrived (SPEC.md §11).
* **References are opaque strings.** `bridge_ref` is the provider's; we store it
  and hand it back, and never parse it.

Chains are opaque strings too (`"solana-devnet"`, `"gnosis-chiado"`): a route is
something a provider either supports or does not, and inventing a chain taxonomy
before phase 6 has to negotiate a real route would be inventing it blind.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ExternalError
from app.core.money import Money
from app.core.time import UtcDatetime

__all__ = [
    "BridgeError",
    "BridgeOrder",
    "BridgeProvider",
    "BridgeRejectedError",
    "BridgeStatus",
    "BridgeTransfer",
    "UnknownTransferError",
]


class BridgeStatus(StrEnum):
    """Where a transfer is. Three answers, because there are only three.

    Note what is *not* here: no "submitted", no "source confirmed", no
    "destination pending". Those are route-specific milestones, and a state
    machine that mirrors one provider's milestones cannot hold another's. The
    funding machine's `BRIDGING` covers all of them.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


# ----------------------------------------------------------------- errors ----


class BridgeError(ExternalError):
    """A bridge call that did not succeed.

    Carries `retryable` from `ExternalError`, so the funding engine treats a busy
    bridge and a busy issuer the same way without knowing about either.
    """


class BridgeRejectedError(BridgeError):
    """The order will not be accepted as stated — a permanent no.

    Distinct from a transfer that *fails after* acceptance: nothing was
    submitted, so no money is in flight and `FAILED_BRIDGE` is safe immediately.
    """


class UnknownTransferError(BridgeError):
    """No transfer with this reference. Asking again will not change that."""

    def __init__(self, bridge_ref: str) -> None:
        super().__init__(f"no bridge transfer with reference {bridge_ref!r}")
        self.bridge_ref = bridge_ref


# ----------------------------------------------------------------- models ----


class _Frozen(BaseModel):
    # Records of what a provider said, not working state — same config, and the
    # same reasoning, as the issuer DTOs.
    model_config = ConfigDict(frozen=True, extra="forbid")


class BridgeOrder(_Frozen):
    """A request to move `amount` from one chain to an address on another."""

    #: Our idempotency key — the funding intent id. Submitting twice with the
    #: same value must produce one transfer, because the engine retries whenever
    #: it cannot tell whether the first call landed.
    order_ref: str
    amount: Money
    source_chain: str
    destination_chain: str
    #: Where the funds must land. For a `CRYPTO_DEPOSIT` issuer this is the
    #: card's own deposit address, and arrival *is* the funding; for a fiat rail
    #: it is our settlement address and funding is a separate call (§9.3).
    destination_address: str


class BridgeTransfer(_Frozen):
    """A transfer as the provider currently describes it."""

    bridge_id: str
    #: The provider's opaque reference, stored on the intent as `bridge_ref`.
    bridge_ref: str
    order_ref: str
    status: BridgeStatus
    amount_in: Money
    #: What arrived, net of fees. `None` until it has — never a hopeful copy of
    #: `amount_in`, because the engine funds the card with this number.
    amount_out: Money | None = None
    destination_tx_ref: str | None = None
    #: Set when `status` is `FAILED`; the reason is ledgered on the intent.
    failure_reason: str | None = None
    submitted_at: UtcDatetime
    completed_at: UtcDatetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------- provider ----


class BridgeProvider(ABC):
    """What every bridge looks like from the funding engine's side."""

    #: Stable identifier for logs, ledger payloads and the demo. Not a registry
    #: key: nothing resolves a bridge from stored data (see the package docstring).
    bridge_id: str

    @abstractmethod
    async def submit(self, order: BridgeOrder) -> BridgeTransfer:
        """Place `order`, idempotently under its `order_ref`.

        Returns the transfer as accepted — normally `PENDING`, since a
        cross-chain transfer that completed synchronously would be a simulation.

        Raises:
            BridgeRejectedError: the order will not be accepted; nothing is in
                flight, so the caller may fail the intent immediately.
            BridgeError: the provider could not be reached or asked us to try
                again; `retryable` says which.
        """

    @abstractmethod
    async def status(self, bridge_ref: str) -> BridgeTransfer:
        """The provider's current view of one transfer.

        Raises:
            UnknownTransferError: no such reference.
            BridgeError: the provider could not be asked right now.
        """
