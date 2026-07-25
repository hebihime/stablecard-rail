"""Read access to the event ledger (SPEC.md §7).

Read-only by construction: there is no write route, and the table itself rejects
UPDATE and DELETE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.ledger.models import LedgerEvent

router = APIRouter(tags=["ledger"])


class LedgerEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    recorded_at: datetime
    event_type: str
    provider_id: str | None
    cardholder_id: str | None
    card_id: str | None
    intent_id: uuid.UUID | None
    state_before: str | None
    state_after: str | None
    amount_minor: int | None
    currency: str | None
    idempotency_key: str | None
    payload: dict[str, Any]


class LedgerPage(BaseModel):
    count: int
    events: list[LedgerEventOut]


@router.get("/ledger", response_model=LedgerPage, summary="List ledger events, oldest first")
async def list_ledger_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    card_id: Annotated[str | None, Query(description="Filter by provider card id")] = None,
    intent_id: Annotated[uuid.UUID | None, Query(description="Filter by funding intent")] = None,
    event_type: Annotated[str | None, Query(description="Exact event type match")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> LedgerPage:
    statement = select(LedgerEvent)
    if card_id is not None:
        statement = statement.where(LedgerEvent.card_id == card_id)
    if intent_id is not None:
        statement = statement.where(LedgerEvent.intent_id == intent_id)
    if event_type is not None:
        statement = statement.where(LedgerEvent.event_type == event_type)

    # Ascending id: the ledger reads as a story, which is the point of §7.
    result = await session.execute(statement.order_by(LedgerEvent.id).limit(limit))
    events = [LedgerEventOut.model_validate(row) for row in result.scalars().all()]
    return LedgerPage(count=len(events), events=events)
