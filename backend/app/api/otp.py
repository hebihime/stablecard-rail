"""The OTP surface (SPEC.md §6.3, §6.4) — the polling half.

SPEC.md §6.3 asks for delivery by polling **and** by WebSocket, in that order and
for that reason: "polling is the reliable fallback; push is the demo-quality path".
So this endpoint is the contract. A client that never opens a socket, or whose
socket drops mid-challenge, must still be able to complete the payment — which
means a missed push costs latency and nothing else.

**This is the one endpoint that hands out a code on purpose.** Everywhere else in
the service the code is kept out of durable stores and out of serialized events; a
client asking "what am I being challenged for" is the destination it was kept alive
for. What the demo does not have is a caller identity: there is no auth on this API
at all, so in a real deployment `card_id` would be derived from the session rather
than accepted from the query string, and the reveal-token pattern in SPEC.md §9.2 is
the shape that belongs here. Recorded rather than papered over — see
docs/ARCHITECTURE.md §11.6.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.redis import get_redis
from app.core.time import utcnow
from app.otp.push import listen
from app.otp.store import OtpChallenge, OtpStore

router = APIRouter(tags=["otp"])

logger = logging.getLogger(__name__)

RedisClient = Annotated[Redis, Depends(get_redis)]


class PendingChallengeOut(BaseModel):
    """One open challenge, as the modal in SPEC.md §6.4 needs it."""

    provider_id: str
    challenge_id: str
    card_id: str | None
    cardholder_id: str | None
    #: The code to show, with a copy button.
    code: str
    #: `True` when we minted it rather than reading it from the provider — which is
    #: what happens with a provider whose 3DS flow makes the card program the
    #: challenge issuer (docs/ARCHITECTURE.md §11.4).
    derived: bool
    delivered_at: datetime
    expires_at: datetime
    #: Sent alongside `expires_at` so the countdown does not depend on the client's
    #: own clock, which is the one clock this service cannot vouch for.
    seconds_remaining: int
    amount_minor: int | None
    currency: str | None

    @classmethod
    def of(cls, challenge: OtpChallenge, *, now: datetime) -> PendingChallengeOut:
        return cls(
            **challenge.model_dump(),
            seconds_remaining=challenge.seconds_left(now),
        )


class PendingChallenges(BaseModel):
    count: int
    challenges: list[PendingChallengeOut]


@router.get(
    "/otp/pending",
    response_model=PendingChallenges,
    summary="Open 3DS challenges, soonest deadline first",
)
async def list_pending_challenges(
    redis: RedisClient,
    card_id: Annotated[str | None, Query(description="Filter by provider card id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PendingChallenges:
    """What the cardholder is being asked to confirm right now.

    An empty list rather than a 404 when nothing is open: a client polls on a timer,
    and "no open challenges" is the normal answer.
    """
    now = utcnow()
    store = OtpStore(redis)
    pending = await store.pending(now=now, card_id=card_id, limit=limit)
    challenges = [PendingChallengeOut.of(challenge, now=now) for challenge in pending]
    return PendingChallenges(count=len(challenges), challenges=challenges)


@router.websocket("/ws/otp")
async def push_challenges(
    websocket: WebSocket,
    redis: RedisClient,
    card_id: Annotated[str | None, Query(description="Filter by provider card id")] = None,
) -> None:
    """The push half of SPEC.md §6.3. Sends exactly what the poll endpoint returns.

    One message per challenge, in the same shape `GET /otp/pending` lists — so a
    client handles a pushed challenge and a polled one with the same code, and
    deduplicates on `challenge_id` without caring which arrived first.

    **What is already open is sent on connect.** A socket opened a second after the
    webhook landed would otherwise show nothing until the *next* challenge, and the
    cardholder is looking at a payment they need to confirm now. It also makes the
    two paths interchangeable rather than merely complementary: a client can connect,
    read the snapshot, and never poll.

    Nothing is read from the socket. Approve/decline is an HTTP call
    (`POST /otp/{challenge_id}/respond`) because it has to be answerable by a client
    whose socket has dropped — which is the same reason polling is the contract.
    """
    await websocket.accept()
    store = OtpStore(redis)
    try:
        now = utcnow()
        for challenge in await store.pending(now=now, card_id=card_id):
            await websocket.send_json(
                PendingChallengeOut.of(challenge, now=now).model_dump(mode="json")
            )
        # `listen` never completes — it ends by raising out of `send_json` when the
        # client goes away — so the loop has no normal exit for coverage to see.
        async for pushed in listen(redis, card_id=card_id):  # pragma: no branch
            await websocket.send_json(
                PendingChallengeOut.of(pushed, now=utcnow()).model_dump(mode="json")
            )
    except WebSocketDisconnect:
        # The client went away. Normal: an app backgrounded, a reload, a network
        # blip — and it costs nothing, because the challenge is still in the store
        # for the next poll or the next connect.
        logger.debug("OTP push client disconnected (card_id=%s)", card_id)
