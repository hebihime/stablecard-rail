"""The only way to write the ledger.

`record()` flushes but never commits: a ledger entry must land in the same
transaction as the change it describes, so the caller owns the commit. A state
transition and its audit record are therefore atomic — you cannot end up with one
without the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.ledger.models import LedgerEvent

__all__ = ["find_by_idempotency_key", "record"]


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware UTC, got a naive datetime")
    return moment.astimezone(UTC)


async def record(
    session: AsyncSession,
    *,
    event_type: str,
    occurred_at: datetime | None = None,
    provider_id: str | None = None,
    cardholder_id: str | None = None,
    card_id: str | None = None,
    intent_id: uuid.UUID | None = None,
    state_before: str | None = None,
    state_after: str | None = None,
    amount: Money | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> LedgerEvent:
    """Append one event. Flushes so the caller sees its `id`; does not commit."""
    event = LedgerEvent(
        event_type=event_type,
        occurred_at=_require_utc(occurred_at) if occurred_at else datetime.now(UTC),
        provider_id=provider_id,
        cardholder_id=cardholder_id,
        card_id=card_id,
        intent_id=intent_id,
        state_before=str(state_before) if state_before is not None else None,
        state_after=str(state_after) if state_after is not None else None,
        amount_minor=amount.amount_minor if amount is not None else None,
        currency=amount.currency if amount is not None else None,
        idempotency_key=idempotency_key,
        payload=dict(payload) if payload is not None else {},
    )
    session.add(event)
    await session.flush()
    return event


async def find_by_idempotency_key(session: AsyncSession, key: str) -> LedgerEvent | None:
    """Look up a previously recorded event. The durable half of webhook dedup."""
    result = await session.execute(
        select(LedgerEvent).where(LedgerEvent.idempotency_key == key).limit(1)
    )
    return result.scalar_one_or_none()
