"""A bridge that does not exist, behaving exactly like one that does.

Its job is to make the end-to-end demo reproducible: SPEC.md §5.2 keeps the
simulator as the default even after phase 6 adds a real protocol, because a
recorded walk-through of a funding pipeline should not be able to fail because
somebody else's testnet is down.

Reproducible means **no randomness and no sleeping**. Latency is measured against
a clock the caller supplies, and failures are a mode the caller selects — so the
same order produces the same references, the same amounts and the same sequence
of states on every run, and a test can move an hour in one line.

The four failure modes are the four shapes a real bridge fails in, and they are
not interchangeable:

| Mode | What the engine sees | Where it should end up |
| --- | --- | --- |
| `SUBMIT_UNAVAILABLE` | a retryable error; nothing accepted | retry, then `FAILED_BRIDGE` |
| `SUBMIT_REJECTED` | a permanent refusal; nothing accepted | `FAILED_BRIDGE`, at once |
| `TRANSFER_FAILED` | accepted, then `FAILED` on a later poll | `FAILED_BRIDGE`, with money moved |
| `STUCK` | accepted, then silence forever | the reconciler's staleness threshold |

`STUCK` is the one worth having: nothing distinguishes a stuck transfer from a
slow one except time, which is the whole argument for SPEC.md §5.3's reconciler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.chain.bridge.base import (
    BridgeError,
    BridgeOrder,
    BridgeProvider,
    BridgeRejectedError,
    BridgeStatus,
    BridgeTransfer,
    UnknownTransferError,
)
from app.core.money import Money
from app.core.time import utcnow

__all__ = [
    "BRIDGE_ID",
    "BridgeFailureMode",
    "SimulatedBridge",
    "SimulatedBridgeSettings",
]

logger = logging.getLogger(__name__)

BRIDGE_ID = "simulated"

#: The seam that replaces `sleep` in tests and the demo alike.
Clock = Callable[[], datetime]


class BridgeFailureMode(StrEnum):
    NONE = "none"
    SUBMIT_UNAVAILABLE = "submit_unavailable"
    SUBMIT_REJECTED = "submit_rejected"
    TRANSFER_FAILED = "transfer_failed"
    STUCK = "stuck"


class SimulatedBridgeSettings(BaseSettings):
    """Env-driven defaults, under a prefix nothing else answers to (§7.4).

    The `.env` locations are repeated rather than imported from `core/`, for the
    reason `issuers/lithic/config.py` gives: a two-entry tuple is a cheaper
    duplication than a shared module every component must depend on.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="SIMULATED_BRIDGE_",
        extra="ignore",
        case_sensitive=False,
    )

    #: Long enough that a demo viewer sees `BRIDGING` rather than a flicker.
    latency_seconds: float = 3.0
    #: A bridge fee in minor units, deducted from what arrives (SPEC.md §11).
    fee_minor: int = 0
    #: Failure injection is something a demo asks for, never something it gets
    #: by accident — so the default is a bridge that works.
    failure_mode: BridgeFailureMode = BridgeFailureMode.NONE


@dataclass(frozen=True, slots=True)
class _Accepted:
    """What the simulator remembers about an order it took."""

    order: BridgeOrder
    bridge_ref: str
    submitted_at: datetime


class SimulatedBridge(BridgeProvider):
    """A deterministic `BridgeProvider`. In-process state, one per process."""

    bridge_id = BRIDGE_ID

    def __init__(
        self,
        *,
        latency_seconds: float = 0.0,
        fee_minor: int = 0,
        failure_mode: BridgeFailureMode = BridgeFailureMode.NONE,
        clock: Clock = utcnow,
    ) -> None:
        if latency_seconds < 0:
            raise ValueError(f"latency_seconds must not be negative, got {latency_seconds}")
        if fee_minor < 0:
            raise ValueError(f"fee_minor must not be negative, got {fee_minor}")
        self._latency = timedelta(seconds=latency_seconds)
        self._fee_minor = fee_minor
        self._failure_mode = failure_mode
        self._clock = clock
        #: order_ref -> accepted order. Keyed on *our* reference, which is what
        #: makes `submit` idempotent for the engine's retries.
        self._accepted: dict[str, _Accepted] = {}

    @classmethod
    def from_settings(
        cls, settings: SimulatedBridgeSettings, *, clock: Clock = utcnow
    ) -> SimulatedBridge:
        return cls(
            latency_seconds=settings.latency_seconds,
            fee_minor=settings.fee_minor,
            failure_mode=settings.failure_mode,
            clock=clock,
        )

    async def submit(self, order: BridgeOrder) -> BridgeTransfer:
        if order.amount.amount_minor <= 0:
            raise BridgeRejectedError(
                f"a bridge order must move a positive amount, got {order.amount}"
            )
        if self._fee_minor >= order.amount.amount_minor:
            raise BridgeRejectedError(
                f"the bridge fee ({self._fee_minor}) would consume the whole transfer "
                f"({order.amount}); nothing would arrive"
            )

        existing = self._accepted.get(order.order_ref)
        if existing is not None:
            if existing.order != order:
                # One of the two callers is wrong about what it asked for, and
                # guessing which would be guessing with money. Stripe answers 409
                # to the same idempotency key with a different body; so do we.
                raise BridgeRejectedError(
                    f"order {order.order_ref!r} differs from the one already submitted"
                )
            return self._view(existing)

        self._inject_submit_failure(order)

        accepted = _Accepted(
            order=order,
            # Derived from our own reference rather than generated: a demo whose
            # identifiers change on every run is a demo that has to be re-recorded.
            bridge_ref=f"sim_{order.order_ref}",
            submitted_at=self._clock(),
        )
        self._accepted[order.order_ref] = accepted
        logger.info(
            "simulated bridge accepted order=%s ref=%s amount=%s",
            order.order_ref,
            accepted.bridge_ref,
            order.amount,
        )
        return self._view(accepted)

    async def status(self, bridge_ref: str) -> BridgeTransfer:
        for accepted in self._accepted.values():
            if accepted.bridge_ref == bridge_ref:
                return self._view(accepted)
        raise UnknownTransferError(bridge_ref)

    # ------------------------------------------------------------ internals --

    def _inject_submit_failure(self, order: BridgeOrder) -> None:
        if self._failure_mode is BridgeFailureMode.SUBMIT_UNAVAILABLE:
            raise BridgeError(
                f"simulated bridge is unavailable (order {order.order_ref})", retryable=True
            )
        if self._failure_mode is BridgeFailureMode.SUBMIT_REJECTED:
            raise BridgeRejectedError(
                f"simulated bridge refuses order {order.order_ref}: no route for "
                f"{order.source_chain} -> {order.destination_chain}"
            )

    def _view(self, accepted: _Accepted) -> BridgeTransfer:
        """The transfer as it stands right now, computed rather than stored.

        `completed_at` is when it *would* have completed — `submitted_at` plus
        the latency — not when somebody got round to asking. Otherwise polling
        late would move a timestamp that describes the past.
        """
        settled_at = accepted.submitted_at + self._latency
        in_flight = self._failure_mode is BridgeFailureMode.STUCK or self._clock() < settled_at

        if in_flight:
            return self._transfer(accepted, BridgeStatus.PENDING)
        if self._failure_mode is BridgeFailureMode.TRANSFER_FAILED:
            return self._transfer(
                accepted,
                BridgeStatus.FAILED,
                completed_at=settled_at,
                failure_reason="simulated destination-chain failure: transfer was not delivered",
            )
        return self._transfer(
            accepted,
            BridgeStatus.COMPLETED,
            completed_at=settled_at,
            amount_out=Money(
                accepted.order.amount.amount_minor - self._fee_minor,
                accepted.order.amount.currency,
            ),
            destination_tx_ref=f"sim_dst_{accepted.order.order_ref}",
        )

    def _transfer(
        self,
        accepted: _Accepted,
        status: BridgeStatus,
        *,
        completed_at: datetime | None = None,
        amount_out: Money | None = None,
        destination_tx_ref: str | None = None,
        failure_reason: str | None = None,
    ) -> BridgeTransfer:
        return BridgeTransfer(
            bridge_id=self.bridge_id,
            bridge_ref=accepted.bridge_ref,
            order_ref=accepted.order.order_ref,
            status=status,
            amount_in=accepted.order.amount,
            amount_out=amount_out,
            destination_tx_ref=destination_tx_ref,
            failure_reason=failure_reason,
            submitted_at=accepted.submitted_at,
            completed_at=completed_at,
            raw={
                "simulated": True,
                "latency_seconds": self._latency.total_seconds(),
                "fee_minor": self._fee_minor,
                "failure_mode": str(self._failure_mode),
                "destination_address": accepted.order.destination_address,
            },
        )
