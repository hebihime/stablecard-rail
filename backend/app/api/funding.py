"""Reading funding intents (SPEC.md §9.3).

Phase 5 built the whole state machine and needed no reader: everything that drives
an intent is a chain poll, a webhook or the reconciler, none of which asks over
HTTP. Phase 8 is the first caller — "live-renders the funding intent's state machine
progress (PENDING → … → FUNDED) by polling the intent endpoint".

**Read-only, by construction.** There is no route here that advances anything. An
intent moves through `funding/machine.advance()` and nowhere else, so a client
cannot push money along by asking, and a bug in this file cannot corrupt a state
machine it has no write path into.

**The sequence is served rather than assumed.** A client that hardcodes the state
order holds a second copy of the machine, in another language, updated by hand —
and this machine has already changed twice since it was written (phase 5 added two
self-transitions, phase 6 changed what `BRIDGED` means). Both times a client-side
copy would have gone stale silently. See docs/ARCHITECTURE.md §12.4.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, computed_field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ResourceNotFound
from app.core.db import get_session
from app.funding.models import FundingIntent
from app.funding.states import HAPPY_PATH, FundingState, is_terminal

router = APIRouter(tags=["funding"])

Session = Annotated[AsyncSession, Depends(get_session)]


class IntentProgress(BaseModel):
    """Where an intent is, in terms a client can draw without knowing the machine."""

    #: `PENDING -> ... -> SETTLED`, from the transition table itself.
    sequence: list[FundingState]
    #: Index into `sequence`, or `None` for a failure state. Deliberately not an
    #: index: a failure is not a later stage of the same journey, and giving
    #: `FAILED_BRIDGE` a position would let a client render "step 3 of 7, four to
    #: go" for an intent that is going nowhere.
    position: int | None
    is_terminal: bool
    is_failure: bool

    @classmethod
    def of(cls, state: FundingState) -> IntentProgress:
        return cls(
            sequence=list(HAPPY_PATH),
            position=HAPPY_PATH.index(state) if state in HAPPY_PATH else None,
            is_terminal=is_terminal(state),
            is_failure=state.is_failure,
        )


class FundingIntentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    state: FundingState
    provider_id: str
    card_id: str
    #: What arrived on the source chain. Never adjusted — see `bridged_amount_minor`.
    amount_minor: int
    currency: str
    #: What the bridge delivered, net of its fee (SPEC.md §11). `None` until the
    #: transfer completes. Both numbers are returned rather than one corrected one,
    #: so the fee is a subtraction a client can show.
    bridged_amount_minor: int | None
    deposit_tx_ref: str | None
    bridge_ref: str | None
    issuer_funding_ref: str | None
    retry_count: int
    #: The reason attached to the most recent retry or failure. The only
    #: human-readable account of a failure outside the ledger.
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    state_changed_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> IntentProgress:
        """Derived on the way out, never stored.

        A computed field rather than a column: it is a rendering of `state` against
        the transition table, and a stored copy is a copy that can disagree with the
        state it describes.
        """
        return IntentProgress.of(self.state)


class FundingIntentPage(BaseModel):
    count: int
    intents: list[FundingIntentOut]


@router.get(
    "/funding/intents",
    response_model=FundingIntentPage,
    summary="List funding intents, newest first",
)
async def list_intents(
    session: Session,
    card_id: Annotated[str | None, Query(description="Filter by provider card id")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> FundingIntentPage:
    """Newest first — the opposite of `GET /ledger`, and for the opposite reason.

    The ledger is ascending because it reads as a story. A client asking "what is
    happening with my card" wants the attempt in flight, and that is the most recent
    one. An empty list rather than a 404 when there is nothing: a fund screen polls
    before any deposit exists, which is most of that screen's life.
    """
    statement = select(FundingIntent)
    if card_id is not None:
        statement = statement.where(FundingIntent.card_id == card_id)
    statement = statement.order_by(FundingIntent.created_at.desc()).limit(limit)

    result = await session.execute(statement)
    intents = [FundingIntentOut.model_validate(intent) for intent in result.scalars().all()]
    return FundingIntentPage(count=len(intents), intents=intents)


@router.get(
    "/funding/intents/{intent_id}",
    response_model=FundingIntentOut,
    summary="Read one funding intent",
)
async def get_intent(
    session: Session,
    intent_id: Annotated[uuid.UUID, Path(description="The intent id, which is its funding_ref")],
) -> FundingIntentOut:
    intent = await session.get(FundingIntent, intent_id)
    if intent is None:
        # `ResourceNotFound`, not `CardNotFoundError`: an intent is a row of ours,
        # and raising an issuer error would attribute the miss to a provider that
        # was never asked. Same 404 and same `code` through the shared handler.
        raise ResourceNotFound(f"no funding intent {intent_id}")
    return FundingIntentOut.model_validate(intent)
