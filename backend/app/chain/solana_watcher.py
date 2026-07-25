"""Watching a Solana account for USDC deposits (SPEC.md §5.2 step 1).

Polling with a persisted cursor, not a websocket subscription. SPEC.md §5.2
allows either and prefers the subscription "if straightforward" — it is not.
`logsSubscribe` delivers at most once, from a connection that can drop silently,
with no way to ask what was missed while it was down; a poll with a cursor
answers "what happened since X" every time it runs, which is the property a
funding pipeline needs. The real reason to prefer websockets is latency, and this
pipeline waits on `finalized` commitment anyway.

**How a deposit is recognised.** Not by parsing instructions — by the account's
own balance. Every transaction carries `preTokenBalances` and `postTokenBalances`
for the accounts it touched, so the credit to a watched account is a subtraction,
and it is correct for a plain `transfer`, a `transferChecked`, a `mintTo`, or a
transfer made three programs deep inside a CPI. An instruction parser would have
to keep up with all of those; a balance diff does not care.

Three things the recorded fixtures settled, each of which would otherwise be a
bug (see `tests/fixtures/solana/README.md`):

* **A failed transaction still carries `postTokenBalances`** — the simulated ones
  from before it failed. Check `err` first or credit money that never moved.
* **A token account created by the transfer has no `preTokenBalances` entry**, not
  a zero one. Missing means zero, or every card's opening deposit is invisible.
* **`uiTokenAmount.uiAmount` is a float.** The integer string next to it is the
  amount; nothing here ever reads the float (docs/ARCHITECTURE.md §2.8).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.chain.rpc import SolanaRpcClient
from app.core.money import Money

__all__ = [
    "ConfirmedDeposit",
    "DepositPage",
    "IgnoredTransfer",
    "SolanaDepositWatcher",
]

logger = logging.getLogger(__name__)

#: What this watcher calls the chain it is on, for `cursor_key()` and the ledger.
SOLANA_DEVNET = "solana-devnet"


@dataclass(frozen=True, slots=True)
class ConfirmedDeposit:
    """A finalized token credit to the watched account.

    `signature` becomes the intent's `deposit_tx_ref`, which is unique — so this
    can be observed twice and still fund a card once.
    """

    chain: str
    deposit_address: str
    mint: str
    signature: str
    slot: int
    #: The chain's own timestamp, when it has one. `blockTime` can be null for
    #: old slots, and inventing one would be inventing the ordering with it.
    block_time: datetime | None
    #: What actually arrived, in the mint's base units.
    base_units: int
    #: The same amount as money the card can be funded with, truncated down.
    amount: Money
    #: Base units below one minor unit, which cannot be credited. Non-zero here
    #: means the deposit was not a whole number of cents.
    dust_base_units: int
    owner: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IgnoredTransfer:
    """Something touched the account and did not become a deposit.

    Kept rather than dropped, for the same reason the webhook receiver ledgers
    `unmapped` deliveries: "nothing happened" and "we decided nothing happened"
    are different, and only one of them can be audited.
    """

    signature: str
    slot: int
    reason: str
    base_units: int | None = None


@dataclass(frozen=True, slots=True)
class DepositPage:
    """One poll's worth of work, oldest first."""

    deposits: tuple[ConfirmedDeposit, ...] = ()
    ignored: tuple[IgnoredTransfer, ...] = ()
    #: The newest signature this page fully accounted for — where the cursor goes
    #: *after* the deposits have been acted on. `None` means nothing new.
    cursor_signature: str | None = None
    cursor_slot: int | None = None
    #: True when the page stopped before its end because a transaction was not
    #: available yet. The rest is not lost; it is next poll's work.
    stopped_early: bool = False


class SolanaDepositWatcher:
    """Polls one deposit address. Knows nothing about funding intents.

    Deliberately no database and no ledger: `funding/` composes this with the
    state machine, which keeps the watcher testable against recorded RPC
    responses and keeps `chain/` free of the pipeline (docs/ARCHITECTURE.md §6).
    """

    def __init__(
        self,
        rpc: SolanaRpcClient,
        *,
        deposit_address: str,
        mint: str,
        decimals: int = 6,
        currency: str = "USD",
        page_limit: int = 25,
        chain: str = SOLANA_DEVNET,
    ) -> None:
        if decimals < 2:
            # USDC has six and a USD card has two. A mint with fewer decimals
            # than the currency would make every deposit a rounding decision.
            raise ValueError(f"a mint with {decimals} decimals cannot fund a 2-decimal currency")
        self._rpc = rpc
        self._address = deposit_address
        self._mint = mint
        self._decimals = decimals
        self._currency = currency
        self._page_limit = page_limit
        self._chain = chain
        #: 10**(6-2) = 10_000 base units per cent, for USDC.
        self._base_units_per_minor = 10 ** (decimals - 2)

    @property
    def deposit_address(self) -> str:
        return self._address

    @property
    def chain(self) -> str:
        return self._chain

    async def poll(self, *, until_signature: str | None = None) -> DepositPage:
        """Everything that reached the account since `until_signature`.

        Processed oldest-first, which is the order the chain applied them and
        therefore the order a ledger should record them — the node returns the
        opposite.
        """
        entries = await self._rpc.get_signatures_for_address(
            self._address, limit=self._page_limit, until=until_signature
        )
        deposits: list[ConfirmedDeposit] = []
        ignored: list[IgnoredTransfer] = []
        cursor_signature: str | None = None
        cursor_slot: int | None = None
        stopped_early = False

        for entry in reversed(entries):
            signature = str(entry["signature"])
            slot = int(entry.get("slot") or 0)

            if entry.get("err") is not None:
                ignored.append(
                    IgnoredTransfer(signature, slot, reason="transaction failed on chain")
                )
                cursor_signature, cursor_slot = signature, slot
                continue

            transaction = await self._rpc.get_transaction(signature)
            if transaction is None:
                # Known to the index, not yet readable. Stopping here — rather
                # than skipping — is what keeps the cursor from stepping over a
                # deposit that is about to become visible.
                logger.info("solana transaction %s not available yet; stopping page", signature)
                stopped_early = True
                break

            outcome = self._interpret(entry, transaction)
            if isinstance(outcome, ConfirmedDeposit):
                deposits.append(outcome)
            else:
                ignored.append(outcome)
            cursor_signature, cursor_slot = signature, slot

        return DepositPage(
            deposits=tuple(deposits),
            ignored=tuple(ignored),
            cursor_signature=cursor_signature,
            cursor_slot=cursor_slot,
            stopped_early=stopped_early,
        )

    # ---------------------------------------------------------- internals ----

    def _interpret(
        self, entry: dict[str, Any], transaction: dict[str, Any]
    ) -> ConfirmedDeposit | IgnoredTransfer:
        signature = str(entry["signature"])
        slot = int(transaction.get("slot") or entry.get("slot") or 0)
        meta = transaction.get("meta") or {}

        if meta.get("err") is not None:
            # The signature list said this succeeded and the transaction says it
            # did not. Trust the transaction: it is the record, the list is an index.
            return IgnoredTransfer(signature, slot, reason="transaction failed on chain")

        index = self._account_index(transaction)
        if index is None:
            return IgnoredTransfer(signature, slot, reason="watched account is not in this tx")

        before = self._balance_at(meta.get("preTokenBalances"), index)
        after = self._balance_at(meta.get("postTokenBalances"), index)
        if after is None:
            return IgnoredTransfer(signature, slot, reason="no token balance for the watched mint")

        # A missing `pre` entry means the account did not exist before this
        # transaction — a first deposit, not a zero-value one.
        credited = after - (before or 0)
        if credited <= 0:
            return IgnoredTransfer(
                signature, slot, reason="not a credit to the watched account", base_units=credited
            )

        minor, dust = divmod(credited, self._base_units_per_minor)
        if minor == 0:
            return IgnoredTransfer(
                signature,
                slot,
                reason=f"below one minor unit of {self._currency}",
                base_units=credited,
            )

        return ConfirmedDeposit(
            chain=self._chain,
            deposit_address=self._address,
            mint=self._mint,
            signature=signature,
            slot=slot,
            block_time=_block_time(transaction.get("blockTime") or entry.get("blockTime")),
            base_units=credited,
            amount=Money(minor, self._currency),
            dust_base_units=dust,
            owner=self._owner_at(meta.get("postTokenBalances"), index),
            raw={
                "slot": slot,
                "signature": signature,
                "mint": self._mint,
                "pre_base_units": before,
                "post_base_units": after,
                "confirmation_status": entry.get("confirmationStatus"),
            },
        )

    def _account_index(self, transaction: dict[str, Any]) -> int | None:
        """Where the watched address sits in this transaction's account list.

        Token balances refer to accounts by index, so this is the join. In
        `jsonParsed` encoding the keys are objects; a loaded-address list (address
        lookup tables) is flat strings, so both shapes are read.
        """
        message = (transaction.get("transaction") or {}).get("message") or {}
        keys: Iterable[Any] = message.get("accountKeys") or []
        for index, key in enumerate(keys):
            pubkey = key.get("pubkey") if isinstance(key, dict) else key
            if pubkey == self._address:
                return index
        return None

    def _balance_at(self, balances: Sequence[dict[str, Any]] | None, index: int) -> int | None:
        entry = self._entry_at(balances, index)
        if entry is None:
            return None
        # The integer string, never the `uiAmount` float sitting beside it.
        return int(entry["uiTokenAmount"]["amount"])

    def _owner_at(self, balances: Sequence[dict[str, Any]] | None, index: int) -> str | None:
        entry = self._entry_at(balances, index)
        owner = entry.get("owner") if entry else None
        return str(owner) if owner else None

    def _entry_at(
        self, balances: Sequence[dict[str, Any]] | None, index: int
    ) -> dict[str, Any] | None:
        for balance in balances or []:
            if balance.get("accountIndex") == index and balance.get("mint") == self._mint:
                return balance
        return None


def _block_time(value: Any) -> datetime | None:
    if not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value, tz=UTC)
