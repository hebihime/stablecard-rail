"""Shared test helpers.

Deliberately thin: the suite arranges state by writing rows directly (so a test
for `advance()` never depends on `advance()` to reach its starting state) and
asserts by reading the ledger back.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.funding.models import FundingIntent
from app.funding.states import FundingState
from app.issuers.base import (
    Card,
    CardEvent,
    CardEventType,
    Cardholder,
    CardIssuerAdapter,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingResult,
    WebhookParseError,
)
from app.issuers.evm_deposit_mock import EvmDepositMockAdapter
from app.ledger.models import LedgerEvent
from app.webhooks.models import WebhookDeadLetter


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


async def all_ledger_events(session: AsyncSession) -> list[LedgerEvent]:
    session.expire_all()
    result = await session.execute(select(LedgerEvent).order_by(LedgerEvent.id))
    return list(result.scalars().all())


async def dead_letters(session: AsyncSession) -> list[WebhookDeadLetter]:
    session.expire_all()
    result = await session.execute(select(WebhookDeadLetter).order_by(WebhookDeadLetter.id))
    return list(result.scalars().all())


async def reload_intent(session: AsyncSession, intent_id: uuid.UUID) -> FundingIntent:
    """Re-read an intent on a fresh identity map, so we assert committed state."""
    session.expire_all()
    result = await session.execute(select(FundingIntent).where(FundingIntent.id == intent_id))
    return result.scalar_one()


async def make_mock_card(
    adapter: EvmDepositMockAdapter, *, currency: str = "USD", activate: bool = True
) -> str:
    """A cardholder plus a card at the mock provider, returning the card id."""
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(
        holder.cardholder_id, CreateCardRequest(currency=currency, spend_limit_minor=100_000)
    )
    if activate:
        await adapter.activate_card(card.card_id)
    return card.card_id


class StubIssuerAdapter(CardIssuerAdapter):
    """A second provider for tests that must not be mock-shaped.

    Two things it proves that the mock cannot: that the registry and the webhook
    receiver work for a `FIAT_RAIL` adapter, and that an adapter which does *not*
    implement `webhook_event_id` still deduplicates (the receiver falls back to a
    body digest).
    """

    provider_id = "stub_provider"
    funding_model = FundingModel.FIAT_RAIL

    def __init__(self, *, verifies: bool = True, parse_fails: bool = False) -> None:
        self.verifies = verifies
        self.parse_fails = parse_fails

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        raise NotImplementedError

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        raise NotImplementedError

    async def get_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def activate_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def freeze_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def cancel_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        raise NotImplementedError

    async def get_balance(self, card_id: str) -> Money:
        raise NotImplementedError

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return self.verifies

    async def parse_webhook(self, body: bytes) -> CardEvent:
        if self.parse_fails:
            raise WebhookParseError("stub adapter cannot read this body")
        return CardEvent(
            provider_id=self.provider_id,
            event_id="stub-event",
            event_type=CardEventType.AUTHORIZATION,
            occurred_at=datetime.now(UTC),
            card_id="stub-card",
            amount=Money(500, "USD"),
            raw={"body": body.decode(errors="replace")},
        )
