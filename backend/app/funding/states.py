"""Funding intent states and the legal-transition table (SPEC.md §5.1).

The table is the specification of this machine; `advance()` is the only code
permitted to act on it. Two conventions are worth stating explicitly:

**Retries are self-transitions.** SPEC.md §5.3 has the reconciler retry with
backoff *up to a cap* and only then mark `FAILED_*`. So a retry does not leave the
state it is retrying — `BRIDGING -> BRIDGING` bumps `retry_count` and ledgers the
attempt. Only the three states that wait on an external system are retryable.

**`FAILED_*` states are terminal.** A failed intent is a closed record; recovery
means opening a new intent, which keeps the ledger honest about what happened
rather than overwriting it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "FAILURE_STATES",
    "RETRYABLE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "FundingState",
    "is_legal_transition",
    "is_terminal",
    "legal_targets",
]


class FundingState(StrEnum):
    """Lifecycle of a single top-up. Stored as its own name."""

    PENDING = "PENDING"
    DEPOSIT_CONFIRMED = "DEPOSIT_CONFIRMED"
    BRIDGING = "BRIDGING"
    BRIDGED = "BRIDGED"
    FUNDING = "FUNDING"
    FUNDED = "FUNDED"
    SETTLED = "SETTLED"

    FAILED_DEPOSIT = "FAILED_DEPOSIT"
    FAILED_BRIDGE = "FAILED_BRIDGE"
    FAILED_FUNDING = "FAILED_FUNDING"
    FAILED_SETTLEMENT = "FAILED_SETTLEMENT"

    @property
    def is_failure(self) -> bool:
        return self.name.startswith("FAILED_")


_S = FundingState

#: Every transition this system permits. Anything absent here raises.
TRANSITIONS: Mapping[FundingState, frozenset[FundingState]] = {
    # Awaiting a finalized USDC deposit. Self-loop = watcher re-checked the chain.
    _S.PENDING: frozenset({_S.PENDING, _S.DEPOSIT_CONFIRMED, _S.FAILED_DEPOSIT}),
    # Deposit finalized; handing off to the bridge. FAILED_BRIDGE covers a bridge
    # order that could not even be submitted.
    _S.DEPOSIT_CONFIRMED: frozenset({_S.BRIDGING, _S.FAILED_BRIDGE}),
    # In flight across the bridge. Self-loop = destination not yet confirmed.
    _S.BRIDGING: frozenset({_S.BRIDGING, _S.BRIDGED, _S.FAILED_BRIDGE}),
    # Funds landed on the destination chain; calling the issuer next.
    _S.BRIDGED: frozenset({_S.FUNDING, _S.FAILED_FUNDING}),
    # Issuer `fund_card` in flight. Self-loop = still awaiting confirmation.
    _S.FUNDING: frozenset({_S.FUNDING, _S.FUNDED, _S.FAILED_FUNDING}),
    # Card credited; awaiting the provider's settlement event to reconcile.
    _S.FUNDED: frozenset({_S.SETTLED, _S.FAILED_SETTLEMENT}),
    # --- terminal ---
    _S.SETTLED: frozenset(),
    _S.FAILED_DEPOSIT: frozenset(),
    _S.FAILED_BRIDGE: frozenset(),
    _S.FAILED_FUNDING: frozenset(),
    _S.FAILED_SETTLEMENT: frozenset(),
}

FAILURE_STATES: frozenset[FundingState] = frozenset(
    state for state in FundingState if state.is_failure
)

TERMINAL_STATES: frozenset[FundingState] = frozenset(
    state for state, targets in TRANSITIONS.items() if not targets
)

#: States that wait on an external system and may therefore be retried in place.
RETRYABLE_STATES: frozenset[FundingState] = frozenset(
    state for state, targets in TRANSITIONS.items() if state in targets
)


def legal_targets(state: FundingState) -> frozenset[FundingState]:
    return TRANSITIONS[state]


def is_legal_transition(frm: FundingState, to: FundingState) -> bool:
    return to in TRANSITIONS[frm]


def is_terminal(state: FundingState) -> bool:
    return state in TERMINAL_STATES
