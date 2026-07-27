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
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, computed_field
from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ResourceNotFound
from app.chain.config import get_solana_settings
from app.chain.signer import LocalKeypairSigner, SignerError
from app.chain.tokens import associated_token_address
from app.core.config import get_settings
from app.core.db import get_session
from app.funding.models import FundingIntent
from app.funding.routes import DepositRoute, register_route, routes_for_card
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


class DepositRouteOut(BaseModel):
    """Where to send money so that it reaches one card (SPEC.md §9.3)."""

    chain: str
    #: **The source address — ours, not the card's.** The account this service
    #: watches, which is the SPL associated token account of the deposit keypair.
    #: The card's own Safe is the *destination* and is on the card, not here;
    #: conflating the two is the mistake docs/ARCHITECTURE.md §9.8 records.
    deposit_address: str
    #: The wallet that owns the token account above. Shown because a block explorer
    #: is more useful for the owner, and because a bare token account looks wrong
    #: to anyone who checks it.
    owner_address: str
    mint: str
    decimals: int
    provider_id: str
    card_id: str


class DepositRouteIn(BaseModel):
    provider_id: str
    card_id: str


def _deposit_route_out(route: DepositRoute, deposit: _DepositAccount) -> DepositRouteOut:
    return DepositRouteOut(
        chain=route.chain,
        deposit_address=route.deposit_address,
        owner_address=deposit.owner,
        mint=deposit.mint,
        decimals=deposit.decimals,
        provider_id=route.provider_id,
        card_id=route.card_id,
    )


@router.post(
    "/funding/deposit-routes",
    response_model=DepositRouteOut,
    status_code=201,
    summary="Point this service's deposit address at a card",
)
async def create_deposit_route(session: Session, body: DepositRouteIn) -> DepositRouteOut:
    """Claim the watched address for this card, and say where to send money.

    Idempotent, and `funding/routes.py` was written that way on purpose — its
    docstring says "the fund screen (SPEC.md §9.3) will do it every time it is
    opened", which is exactly what happens now that the fund screen exists.

    **The address is not a parameter.** It is derived here from this service's own
    deposit keypair, because a caller that could name an address could point the
    watcher at somebody else's account and claim their deposits.
    """
    deposit = _deposit_account()
    route = await register_route(
        session,
        chain=deposit.chain,
        deposit_address=deposit.address,
        provider_id=body.provider_id,
        card_id=body.card_id,
    )
    await session.commit()
    return _deposit_route_out(route, deposit)


@router.get(
    "/funding/deposit-routes",
    response_model=list[DepositRouteOut],
    summary="Addresses already funding a card",
)
async def list_deposit_routes(
    session: Session,
    provider_id: Annotated[str, Query(description="Issuer registry key")],
    card_id: Annotated[str, Query(description="The provider's opaque card identifier")],
) -> list[DepositRouteOut]:
    deposit = _deposit_account()
    routes = await routes_for_card(session, provider_id=provider_id, card_id=card_id)
    return [_deposit_route_out(route, deposit) for route in routes]


@dataclass(frozen=True)
class _DepositAccount:
    chain: str
    owner: str
    address: str
    mint: str
    decimals: int


def _deposit_account() -> _DepositAccount:
    """This service's Solana deposit account, derived rather than configured.

    Two addresses come out of one keypair and only one of them can receive USDC:
    the wallet holds SOL and the *associated token account* holds the token. A fund
    screen showing the wallet would collect deposits nobody can see, which is the
    §9.8 trap in its other direction.
    """
    settings = get_solana_settings()
    if not settings.deposit_keypair:
        # 503 rather than 500: the service is fine and one setting is missing, and
        # naming it is the difference between a two-minute fix and an investigation.
        raise HTTPException(
            status_code=503,
            detail=(
                "SOLANA_DEPOSIT_KEYPAIR is not configured, so this service has no "
                "address to receive deposits at"
            ),
        )
    try:
        signer = LocalKeypairSigner.from_env_value(settings.deposit_keypair)
    except SignerError as exc:
        raise HTTPException(
            status_code=503, detail=f"SOLANA_DEPOSIT_KEYPAIR is set but unusable: {exc}"
        ) from exc
    address = associated_token_address(signer.pubkey, Pubkey.from_string(settings.usdc_mint))
    return _DepositAccount(
        chain=get_settings().funding_source_chain,
        owner=signer.public_key,
        address=str(address),
        mint=settings.usdc_mint,
        decimals=settings.usdc_decimals,
    )
