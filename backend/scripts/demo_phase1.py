"""Phase 1 demo: walk a funding intent through the state machine and print the ledger.

    python scripts/demo_phase1.py

Shows, against a real database: a created intent, the full happy path, a retry
self-transition, a rejected illegal transition that is still ledgered, and the
resulting append-only event stream.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.money import Money
from app.funding.machine import IllegalTransitionError, advance, create_intent
from app.funding.states import FundingState
from app.ledger.models import LedgerEvent

HAPPY_PATH = [
    FundingState.DEPOSIT_CONFIRMED,
    FundingState.BRIDGING,
    FundingState.BRIDGED,
    FundingState.FUNDING,
    FundingState.FUNDED,
    FundingState.SETTLED,
]


async def main() -> None:
    # Keep the narrative on stdout readable; the ledger dump below shows the same
    # events the log lines would.
    logging.basicConfig(level=logging.ERROR)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        intent = await create_intent(
            session,
            provider_id="gnosis_pay_mock",
            card_id="card_demo_1",
            amount=Money(2500, "USD"),
            deposit_tx_ref=f"demo-deposit-{asyncio.get_running_loop().time():.6f}",
        )
        print(f"created intent {intent.id} in {intent.state} for {intent.money}")

        for target in HAPPY_PATH:
            if target is FundingState.BRIDGED:
                # One retry first: the bridge has not confirmed yet.
                await advance(
                    session,
                    intent.id,
                    FundingState.BRIDGING,
                    reason="destination not confirmed yet",
                )
                print("  retried BRIDGING (retry_count now 1)")
            is_bridging = target is FundingState.BRIDGING
            updated = await advance(
                session,
                intent.id,
                target,
                reason=f"demo step -> {target}",
                updates={"bridge_ref": "demo-bridge-order"} if is_bridging else None,
            )
            print(f"  -> {updated.state}")

        try:
            await advance(session, intent.id, FundingState.FUNDING)
        except IllegalTransitionError as exc:
            print(f"rejected as expected: {exc}")

        print("\nledger:")
        events = (
            (
                await session.execute(
                    select(LedgerEvent)
                    .where(LedgerEvent.intent_id == intent.id)
                    .order_by(LedgerEvent.id)
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            before = event.state_before or "-"
            print(
                f"  #{event.id:<3} {event.occurred_at:%H:%M:%S} {event.event_type:<38}"
                f" {before:>17} -> {event.state_after}"
            )


if __name__ == "__main__":
    asyncio.run(main())
