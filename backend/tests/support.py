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
from app.issuers.gnosis_pay_mock import Delivery, GnosisPayMockAdapter
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
        provider_id: str = "gnosis_pay_mock",
        card_id: str = "card_test_1",
        deposit_tx_ref: str | None = None,
        retry_count: int = 0,
    ) -> FundingIntent: ...


def routed_paths(node: object) -> set[str]:
    """Every path the app serves, WebSockets included.

    Recursive, and it follows `original_router`, because Starlette 1.3 wraps an
    included router in an `_IncludedRouter` that exposes neither `path` nor
    `routes` — so the obvious `{route.path for route in app.routes}` reports only
    the four docs endpoints and would make "is this route wired up?" pass
    vacuously. `app.openapi()` is not an alternative: WebSocket routes are absent
    from an OpenAPI schema by definition, and the socket is the thing being checked.
    """
    routes = getattr(node, "routes", None)
    if not routes:
        included = getattr(node, "original_router", None)
        routes = getattr(included, "routes", []) if included is not None else []
    found: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            found.add(path)
        found |= routed_paths(route)
    return found


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
    adapter: GnosisPayMockAdapter,
    *,
    activate: bool = True,
    deposit: Money | None = Money(100_000, "USD"),
) -> str:
    """A user, their Safe, and a card at the mock provider. Returns the card id.

    `deposit` lands on-chain rather than through `fund_card`, because at this
    provider that is the only way money arrives (SPEC.md §3.2). Without it every
    authorization comes back `InsufficientFunds`, which is correct but is rarely
    what a test about something else wants.
    """
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(
        holder.cardholder_id, CreateCardRequest(spend_limit_minor=100_000)
    )
    if activate:
        await adapter.activate_card(card.card_id)
    if deposit is not None:
        assert card.deposit_address is not None
        adapter.simulator.receive_onchain_deposit(card.deposit_address, deposit)
    return card.card_id


def mock_authorization(
    adapter: GnosisPayMockAdapter, card_id: str, amount: Money, *, merchant: str = "Test Merchant"
) -> tuple[str, Delivery]:
    """An authorization at the mock provider: its thread id, and its delivery.

    `authorize` returns the transaction, because clearing and reversing it need the
    thread id; the delivery it queued is what a webhook test wants. Both, then.
    """
    transaction = adapter.simulator.authorize(card_id, amount, merchant=merchant)
    return transaction.thread_id, adapter.simulator.deliveries[-1]


class StubIssuerAdapter(CardIssuerAdapter):
    """A second provider for tests that must not be mock-shaped.

    Two things it proves that the mock cannot: that the registry and the webhook
    receiver work for a `FIAT_RAIL` adapter, and that an adapter which does *not*
    implement `webhook_event_id` still deduplicates (the receiver falls back to a
    body digest).
    """

    provider_id = "stub_provider"
    funding_model = FundingModel.FIAT_RAIL

    #: Deliveries whose id lives in a header, the way Lithic's does.
    EVENT_ID_HEADER = "x-stub-event-id"

    def __init__(self, *, verifies: bool = True, parse_fails: bool = False) -> None:
        self.verifies = verifies
        self.parse_fails = parse_fails
        #: What `parse_webhook` was last given, so a test can assert the receiver
        #: passes the headers through rather than dropping them.
        self.parsed_headers: dict[str, str] | None = None

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

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        self.parsed_headers = dict(headers)
        if self.parse_fails:
            raise WebhookParseError("stub adapter cannot read this body")
        return CardEvent(
            provider_id=self.provider_id,
            event_id=headers.get(self.EVENT_ID_HEADER, "stub-event"),
            event_type=CardEventType.AUTHORIZATION,
            occurred_at=datetime.now(UTC),
            card_id="stub-card",
            amount=Money(500, "USD"),
            raw={"body": body.decode(errors="replace")},
        )
