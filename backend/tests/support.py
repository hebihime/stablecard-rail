"""Shared test helpers.

Deliberately thin: the suite arranges state by writing rows directly (so a test
for `advance()` never depends on `advance()` to reach its starting state) and
asserts by reading the ledger back.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.funding.models import FundingIntent
from app.funding.states import FundingState
from app.ledger.models import LedgerEvent


class SeedIntent(Protocol):
    """Inserts a `FundingIntent` at an arbitrary state, bypassing the machine."""

    async def __call__(
        self,
        *,
        state: FundingState = FundingState.PENDING,
        amount_minor: int = 2500,
        currency: str = "USD",
        provider_id: str = "evm_deposit_mock",
        card_id: str = "card_test_1",
        deposit_tx_ref: str | None = None,
        retry_count: int = 0,
    ) -> FundingIntent: ...


async def ledger_for_intent(session: AsyncSession, intent_id: uuid.UUID) -> list[LedgerEvent]:
    result = await session.execute(
        select(LedgerEvent).where(LedgerEvent.intent_id == intent_id).order_by(LedgerEvent.id)
    )
    return list(result.scalars().all())


async def reload_intent(session: AsyncSession, intent_id: uuid.UUID) -> FundingIntent:
    """Re-read an intent on a fresh identity map, so we assert committed state."""
    session.expire_all()
    result = await session.execute(select(FundingIntent).where(FundingIntent.id == intent_id))
    return result.scalar_one()
