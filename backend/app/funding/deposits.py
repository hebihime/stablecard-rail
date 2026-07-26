"""Turning what the chain did into funding intents (SPEC.md §5.2 step 1).

This is the seam between `chain/` and `funding/`. The watcher observes and knows
nothing about intents; the state machine knows nothing about chains; this module
is the only place that knows both, which is what keeps either of them replaceable.

**Order of operations, and why it is not the obvious one.** Intents are created
first and the cursor moves last. The reverse would be tidier — one transaction,
one commit — but `create_intent()` commits, so there is no single transaction to
be had, and of the two possible crash windows only this one is safe:

* cursor first, then intents: a crash loses the deposit permanently, because the
  cursor is passed back to the node as `until` and the signature is never listed
  again;
* intents first, then cursor: a crash re-observes the same deposits, and the
  unique index on `deposit_tx_ref` refuses the second intent.

At-least-once plus a unique key is exactly-once effect — the bargain the webhook
receiver already makes (docs/ARCHITECTURE.md §2.4).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.cursors import advance_cursor, cursor_key, load_cursor
from app.chain.solana_watcher import (
    ConfirmedDeposit,
    DepositPage,
    IgnoredTransfer,
    SolanaDepositWatcher,
)
from app.funding.machine import advance, create_intent, intent_for_deposit
from app.funding.routes import route_for
from app.funding.states import FundingState
from app.ledger import event_types
from app.ledger.writer import find_by_idempotency_key, record

__all__ = ["DepositReport", "collect_deposits", "record_deposits"]

logger = logging.getLogger(__name__)

_DEPOSIT_CONSTRAINT = "uq_funding_intents_deposit_tx_ref"


@dataclass(frozen=True, slots=True)
class DepositReport:
    """What one poll turned into. Every observed transfer appears in exactly one
    of these lists, so a caller can reconcile the page against the ledger."""

    opened: tuple[uuid.UUID, ...] = ()
    #: Deposits already recorded — the watcher re-observed them after a restart.
    duplicates: tuple[str, ...] = ()
    #: Creditable money at an address no card claims.
    unroutable: tuple[str, ...] = ()
    #: Transfers the watcher itself declined to credit, with its reasons.
    ignored: tuple[IgnoredTransfer, ...] = field(default=())

    @property
    def observed(self) -> int:
        return len(self.opened) + len(self.duplicates) + len(self.unroutable) + len(self.ignored)


async def collect_deposits(session: AsyncSession, watcher: SolanaDepositWatcher) -> DepositReport:
    """Poll once, record what arrived, then move the cursor. Commits."""
    key = cursor_key(watcher.chain, watcher.deposit_address)
    cursor = await load_cursor(session, key)

    page = await watcher.poll(until_signature=cursor.last_signature if cursor else None)
    report = await record_deposits(session, page, chain=watcher.chain)

    if page.cursor_signature is not None:
        # Last, and only for the part of the page that was fully accounted for.
        await advance_cursor(
            session, key, signature=page.cursor_signature, slot=page.cursor_slot or 0
        )
        await session.commit()

    if report.observed:
        logger.info(
            "chain poll on %s: %s opened, %s duplicate, %s unroutable, %s ignored",
            watcher.deposit_address,
            len(report.opened),
            len(report.duplicates),
            len(report.unroutable),
            len(report.ignored),
        )
    return report


async def record_deposits(session: AsyncSession, page: DepositPage, *, chain: str) -> DepositReport:
    """Open one intent per creditable deposit; ledger everything else. Commits."""
    opened: list[uuid.UUID] = []
    duplicates: list[str] = []
    unroutable: list[str] = []

    for ignored in page.ignored:
        await _ledger_ignored(session, ignored, chain=chain)

    for deposit in page.deposits:
        route = await route_for(session, chain=chain, deposit_address=deposit.deposit_address)
        if route is None:
            # Not an exception: one unroutable deposit must not stop the rest of
            # the page from being credited.
            await _ledger_unroutable(session, deposit)
            unroutable.append(deposit.signature)
            continue

        intent_id = await _open_intent(
            session, deposit, provider_id=route.provider_id, card_id=route.card_id
        )
        if intent_id is None:
            duplicates.append(deposit.signature)
            continue
        opened.append(intent_id)

    return DepositReport(
        opened=tuple(opened),
        duplicates=tuple(duplicates),
        unroutable=tuple(unroutable),
        ignored=page.ignored,
    )


async def _open_intent(
    session: AsyncSession, deposit: ConfirmedDeposit, *, provider_id: str, card_id: str
) -> uuid.UUID | None:
    """A new intent in `DEPOSIT_CONFIRMED`, or `None` if we have seen it before."""
    context = {
        "chain": deposit.chain,
        "deposit_address": deposit.deposit_address,
        "mint": deposit.mint,
        "slot": deposit.slot,
        "base_units": deposit.base_units,
        # Non-zero means the deposit was not a whole number of minor units and
        # this much could not be credited (docs/ARCHITECTURE.md §9.7).
        "dust_base_units": deposit.dust_base_units,
        "owner": deposit.owner,
        "block_time": deposit.block_time.isoformat() if deposit.block_time else None,
    }

    if await intent_for_deposit(session, deposit.signature) is not None:
        # The ordinary path after a restart, not an exceptional one: the watcher
        # re-observes deposits by design, so it is asked rather than provoked.
        await _note_duplicate(session, deposit)
        return None

    try:
        intent = await create_intent(
            session,
            provider_id=provider_id,
            card_id=card_id,
            amount=deposit.amount,
            deposit_tx_ref=deposit.signature,
            payload=context,
            idempotency_key=f"deposit:{deposit.signature}",
        )
    except IntegrityError as exc:
        if _DEPOSIT_CONSTRAINT not in str(exc.orig):
            raise
        # The backstop for the race the question above cannot close: two workers
        # asking at the same time both get "no intent yet". The unique index is
        # what makes only one of them right.
        await _note_duplicate(session, deposit)
        return None

    # The deposit is finalized by construction — the watcher only reports
    # `finalized` commitment — so the intent does not linger in PENDING.
    await advance(
        session,
        intent.id,
        FundingState.DEPOSIT_CONFIRMED,
        reason=f"{deposit.chain} transfer {deposit.signature} finalized in slot {deposit.slot}",
        payload=context,
        idempotency_key=f"deposit-confirmed:{deposit.signature}",
    )
    return intent.id


async def _ledger_ignored(session: AsyncSession, ignored: IgnoredTransfer, *, chain: str) -> None:
    await _record_once(
        session,
        event_type=event_types.TRANSFER_IGNORED,
        payload={
            "chain": chain,
            "signature": ignored.signature,
            "slot": ignored.slot,
            "reason": ignored.reason,
            "base_units": ignored.base_units,
        },
        idempotency_key=f"transfer-ignored:{ignored.signature}",
    )


async def _ledger_unroutable(session: AsyncSession, deposit: ConfirmedDeposit) -> None:
    logger.warning(
        "deposit %s to %s has no route to a card", deposit.signature, deposit.deposit_address
    )
    await _record_once(
        session,
        event_type=event_types.DEPOSIT_UNROUTABLE,
        amount=deposit.amount,
        payload={
            "chain": deposit.chain,
            "signature": deposit.signature,
            "deposit_address": deposit.deposit_address,
            "slot": deposit.slot,
            "base_units": deposit.base_units,
        },
        idempotency_key=f"deposit-unroutable:{deposit.signature}",
    )


async def _note_duplicate(session: AsyncSession, deposit: ConfirmedDeposit) -> None:
    """A deposit that already has an intent. Not an error, and not news."""
    await session.rollback()
    logger.info("deposit %s already has an intent", deposit.signature)
    await _record_once(
        session,
        event_type=event_types.DEPOSIT_DUPLICATE,
        amount=deposit.amount,
        payload={
            "chain": deposit.chain,
            "signature": deposit.signature,
            "deposit_address": deposit.deposit_address,
        },
        idempotency_key=f"deposit-duplicate:{deposit.signature}",
    )


async def _record_once(session: AsyncSession, **entry: Any) -> None:
    """One ledger row per keyed event, however often it is re-observed.

    Asked first and enforced second, the same two layers as everywhere else: the
    watcher re-observes the same ignored transfer on every restart, so the
    ordinary path should not be a provoked constraint violation.

    The write and the commit share one `try` because `record()` flushes, so the
    unique violation can surface at either — a trap phase 2 paid for once already.
    """
    if await find_by_idempotency_key(session, entry["idempotency_key"]) is not None:
        return

    try:
        await record(session, **entry)
        await session.commit()
    except IntegrityError:
        # Two workers both found nothing a moment ago. The index decides.
        await session.rollback()
