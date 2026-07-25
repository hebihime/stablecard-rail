"""Card lifecycle endpoints (SPEC.md §12 phase 2).

Every route is `/providers/{provider_id}/...`. The provider is in the path rather
than looked up from a local card table because there is no local card table: the
provider owns card state, and a second copy of it here would be a cache that can
silently disagree with the thing it caches. See docs/ARCHITECTURE.md §3.4.

These routes are thin on purpose — resolve the adapter, call it, ledger what we
did (SPEC.md §7: "every card action"), return the provider's own view. No route
touches `funding/`: moving money onto a card goes through the funding state
machine, never through an HTTP call to an issuer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.money import Money
from app.issuers.base import (
    Card,
    Cardholder,
    CardIssuerAdapter,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
)
from app.issuers.registry import describe, get_adapter
from app.ledger import event_types
from app.ledger.writer import record

router = APIRouter(tags=["cards"])

ProviderId = Annotated[str, Path(description="Issuer registry key, e.g. `evm_deposit_mock`")]
CardId = Annotated[str, Path(description="The provider's opaque card identifier")]
Session = Annotated[AsyncSession, Depends(get_session)]


class ProviderOut(BaseModel):
    """What a client needs to know about a provider without importing an adapter."""

    provider_id: str
    funding_model: FundingModel


class BalanceOut(BaseModel):
    card_id: str
    amount_minor: int
    currency: str


@router.get("/providers", response_model=list[ProviderOut], summary="Registered issuers")
async def list_providers() -> list[ProviderOut]:
    return [
        ProviderOut(provider_id=provider_id, funding_model=funding_model)
        for provider_id, funding_model in describe()
    ]


@router.post(
    "/providers/{provider_id}/cardholders",
    response_model=Cardholder,
    status_code=201,
    summary="Create a cardholder",
)
async def create_cardholder(
    provider_id: ProviderId, req: CreateCardholderRequest, session: Session
) -> Cardholder:
    adapter = get_adapter(provider_id)
    holder = await adapter.create_cardholder(req)
    await record(
        session,
        event_type=event_types.CARDHOLDER_CREATED,
        provider_id=provider_id,
        cardholder_id=holder.cardholder_id,
        payload={"email_domain": holder.email.rpartition("@")[2]},
    )
    await session.commit()
    return holder


@router.post(
    "/providers/{provider_id}/cardholders/{cardholder_id}/cards",
    response_model=Card,
    status_code=201,
    summary="Create a virtual card",
)
async def create_card(
    provider_id: ProviderId, cardholder_id: str, req: CreateCardRequest, session: Session
) -> Card:
    adapter = get_adapter(provider_id)
    card = await adapter.create_card(cardholder_id, req)
    await record(
        session,
        event_type=event_types.CARD_CREATED,
        provider_id=provider_id,
        cardholder_id=card.cardholder_id,
        card_id=card.card_id,
        state_after=card.state.value,
        payload={
            "currency": card.currency,
            "spend_limit_minor": card.spend_limit_minor,
            # For a CRYPTO_DEPOSIT provider this is where funds must be sent;
            # phase 5's watcher needs it, so it is recorded when first seen.
            "deposit_address": card.deposit_address,
            "funding_model": adapter.funding_model.value,
        },
    )
    await session.commit()
    return card


@router.get(
    "/providers/{provider_id}/cards/{card_id}",
    response_model=Card,
    summary="Read a card",
)
async def get_card(provider_id: ProviderId, card_id: CardId) -> Card:
    # A read, so nothing is ledgered: the ledger records what happened, not what
    # was looked at.
    return await get_adapter(provider_id).get_card(card_id)


@router.get(
    "/providers/{provider_id}/cards/{card_id}/balance",
    response_model=BalanceOut,
    summary="Read a card balance",
)
async def get_balance(provider_id: ProviderId, card_id: CardId) -> BalanceOut:
    balance: Money = await get_adapter(provider_id).get_balance(card_id)
    return BalanceOut(card_id=card_id, amount_minor=balance.amount_minor, currency=balance.currency)


@router.post(
    "/providers/{provider_id}/cards/{card_id}/activate",
    response_model=Card,
    summary="Activate a card (also the unfreeze path)",
)
async def activate_card(provider_id: ProviderId, card_id: CardId, session: Session) -> Card:
    return await _lifecycle(
        provider_id, card_id, session, event_types.CARD_ACTIVATED, lambda a: a.activate_card
    )


@router.post(
    "/providers/{provider_id}/cards/{card_id}/freeze",
    response_model=Card,
    summary="Freeze a card",
)
async def freeze_card(provider_id: ProviderId, card_id: CardId, session: Session) -> Card:
    return await _lifecycle(
        provider_id, card_id, session, event_types.CARD_FROZEN, lambda a: a.freeze_card
    )


@router.post(
    "/providers/{provider_id}/cards/{card_id}/cancel",
    response_model=Card,
    summary="Cancel a card, permanently",
)
async def cancel_card(provider_id: ProviderId, card_id: CardId, session: Session) -> Card:
    return await _lifecycle(
        provider_id, card_id, session, event_types.CARD_CANCELED, lambda a: a.cancel_card
    )


async def _lifecycle(
    provider_id: str,
    card_id: str,
    session: AsyncSession,
    event_type: str,
    pick: Callable[[CardIssuerAdapter], Callable[[str], Awaitable[Card]]],
) -> Card:
    """Read the card, change it at the provider, ledger the before/after.

    The extra read is what makes the ledger row useful: without `state_before`, a
    lifecycle entry says only where the card ended up. A rejected change raises out
    of here and is ledgered nowhere — nothing happened, and the provider's own
    refusal is the record.
    """
    adapter = get_adapter(provider_id)
    before = await adapter.get_card(card_id)
    card = await pick(adapter)(card_id)
    await record(
        session,
        event_type=event_type,
        provider_id=provider_id,
        cardholder_id=card.cardholder_id,
        card_id=card.card_id,
        state_before=before.state.value,
        state_after=card.state.value,
    )
    await session.commit()
    return card
