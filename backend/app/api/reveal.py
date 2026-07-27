"""The card-reveal surface (SPEC.md §9.2).

Two calls, deliberately not one:

    POST /providers/{provider_id}/cards/{card_id}/reveal-token   -> a 60-second token
    POST /reveal                                                 -> the card, once

Collapsing them into a single `GET /cards/{id}/details` would be simpler and would
lose the property the pattern exists for. Gnosis Pay's PSE splits the same way for
the same reason: minting is privileged (mTLS, partner certificate) and rendering is
not, so the privileged half stays on the backend and the client is handed something
that expires, works once, and is worthless anywhere else.

**There is no auth on this API**, which phase 7 already recorded as the honest gap
(docs/ARCHITECTURE.md §11.6). It matters more here than anywhere else in the service,
because the mint endpoint is the one that decides who may see a card — in a real
deployment `card_id` would be checked against the caller's session, not accepted from
the path. Recorded rather than papered over; see §12.3.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.core.time import utcnow
from app.issuers.base import RevealedCard, RevealUnsupported
from app.issuers.registry import get_adapter
from app.ledger import event_types
from app.ledger.writer import record
from app.reveal.capability import supports_reveal
from app.reveal.store import RevealGrant, RevealRejection, RevealTokenStore

router = APIRouter(tags=["reveal"])

logger = logging.getLogger(__name__)

RedisClient = Annotated[Redis, Depends(get_redis)]
Session = Annotated[AsyncSession, Depends(get_session)]

#: What a caller is told when a token does not work, whatever the reason. One string
#: for three cases — never minted, expired, already spent — because the differences
#: are ours to know and an attacker's to exploit.
REJECTION_DETAIL = "reveal token is not valid; request a new one"


def _store(redis: Redis) -> RevealTokenStore:
    settings = get_settings()
    return RevealTokenStore(
        redis,
        ttl_seconds=settings.reveal_token_ttl_seconds,
        replay_memory_seconds=settings.reveal_replay_memory_seconds,
    )


class RevealTokenOut(BaseModel):
    """The client's half of the exchange."""

    token: str
    provider_id: str
    card_id: str
    expires_at: datetime
    #: Seconds, from the server. The reveal screen's auto-hide (SPEC.md §9.2) counts
    #: this down rather than the client's own clock.
    expires_in: int


@router.post(
    "/providers/{provider_id}/cards/{card_id}/reveal-token",
    response_model=RevealTokenOut,
    status_code=201,
    summary="Mint a short-lived, single-use token for revealing a card",
)
async def mint_reveal_token(
    session: Session,
    redis: RedisClient,
    provider_id: Annotated[str, Path(description="Issuer registry key")],
    card_id: Annotated[str, Path(description="The provider's opaque card identifier")],
) -> RevealTokenOut:
    """Check the provider can do this and the card is real, then issue a token.

    Both checks happen *before* minting, and the order is the point. Handing out a
    token and discovering at the exchange that the provider has no reveal path, or
    that the card does not exist, would put the failure after the client has started
    a countdown — and after a credential exists that nothing will ever spend.
    """
    adapter = get_adapter(provider_id)
    if not supports_reveal(adapter):
        # Raised rather than returned, so `api/errors.py` maps it — 501, and not the
        # 502 an `IssuerError` would otherwise get. The card is fine and the provider
        # is up; it is the capability that is absent, and saying that is more useful
        # than pretending the card is missing or blaming an upstream for being asked.
        raise RevealUnsupported(provider_id)
    # Raises `CardNotFoundError` -> 404 through the installed handler.
    card = await adapter.get_card(card_id)

    minted = await _store(redis).mint(provider_id, card.card_id, now=utcnow())
    await record(
        session,
        event_type=event_types.REVEAL_TOKEN_MINTED,
        provider_id=provider_id,
        cardholder_id=card.cardholder_id,
        card_id=card.card_id,
        # No token, not even a digest: a ledger row proves a reveal was requested,
        # and a digest would let anyone holding the ledger confirm a guessed token.
        payload={"expires_in": minted.expires_in},
    )
    await session.commit()
    return RevealTokenOut(
        token=minted.token,
        provider_id=minted.provider_id,
        card_id=minted.card_id,
        expires_at=minted.expires_at,
        expires_in=minted.expires_in,
    )


class RevealIn(BaseModel):
    #: `min_length` so an empty string is a 422 from the model rather than a lookup
    #: that was never going to find anything.
    token: str = Field(min_length=1, repr=False)


@router.post(
    "/reveal",
    response_model=RevealedCard,
    summary="Exchange a reveal token for the card's details",
)
async def redeem_reveal_token(
    session: Session,
    redis: RedisClient,
    body: RevealIn,
) -> RevealedCard:
    """Spend the token, then ask the provider to render the card.

    The card is not named in the request: it is what the token was minted for. A
    client that could pass a `card_id` here could pair someone else's token with its
    own card, and the token would have stopped meaning anything.
    """
    outcome = await _store(redis).redeem(body.token)
    if isinstance(outcome, RevealRejection):
        await _ledger_rejection(session, outcome)
        raise HTTPException(status_code=404, detail=REJECTION_DETAIL)
    return await _reveal(session, outcome)


async def _reveal(session: AsyncSession, grant: RevealGrant) -> RevealedCard:
    adapter = get_adapter(grant.provider_id)
    try:
        revealed = await adapter.reveal(grant.card_id)
    except RevealUnsupported:
        # Only reachable if a provider's capability changed between mint and
        # exchange — a redeploy inside the token's minute. Rare, and a 502 is right:
        # the token was good and we could not honour it.
        logger.warning("provider %s lost its reveal path mid-exchange", grant.provider_id)
        raise
    await record(
        session,
        event_type=event_types.REVEAL_GRANTED,
        provider_id=grant.provider_id,
        card_id=revealed.card_id,
        payload={"rendered_in": revealed.rendered_in, "last_four": revealed.last_four},
    )
    await session.commit()
    return revealed


async def _ledger_rejection(session: AsyncSession, rejection: RevealRejection) -> None:
    """Record the refusal, with as much attribution as honestly exists.

    A replay names a token we minted, so the row can name its card; an unrecognised
    token names nothing, and inventing an attribution would make the ledger read as
    though somebody had attacked a specific card. `provider_id` is absent for the
    same reason.
    """
    await record(
        session,
        event_type=event_types.REVEAL_REJECTED,
        provider_id=rejection.provider_id,
        card_id=rejection.card_id,
        payload={"reason": rejection.reason.value},
    )
    await session.commit()
