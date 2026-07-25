"""`POST /webhooks/{provider_id}` — one endpoint per provider (SPEC.md §4).

The route does almost nothing, which is the point: raw bytes and headers go
straight to `webhooks.receiver`, and the only HTTP-specific decisions here are the
status codes. Verification failure is a 401; **everything after that is a 200**,
including a duplicate, an event we cannot parse, and a handler that threw
(SPEC.md §4: handler exceptions never cause a non-2xx once verification passed).

`await request.body()` rather than a parsed model: every provider signs the raw
bytes, and re-serializing JSON before verifying is the standard way to break a
signature check.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.redis import get_redis
from app.issuers.base import CardEventType
from app.webhooks.receiver import receive

router = APIRouter(tags=["webhooks"])


class WebhookAck(BaseModel):
    """What the provider gets back. Informative, but never load-bearing for them."""

    received: bool
    duplicate: bool
    provider_id: str
    event_id: str
    event_type: CardEventType
    ledger_event_id: int | None
    handlers_failed: list[str]


@router.post(
    "/webhooks/{provider_id}",
    response_model=WebhookAck,
    summary="Receive a provider webhook",
    responses={
        401: {"description": "Signature verification failed"},
        404: {"description": "No adapter registered for this provider_id"},
    },
)
async def receive_webhook(
    provider_id: Annotated[str, Path(description="Issuer registry key")],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> WebhookAck:
    outcome = await receive(
        session,
        redis,
        provider_id=provider_id,
        headers=dict(request.headers),
        body=await request.body(),
    )
    return WebhookAck(
        received=True,
        duplicate=outcome.duplicate,
        provider_id=outcome.provider_id,
        event_id=outcome.event_id,
        event_type=outcome.event_type,
        ledger_event_id=outcome.ledger_event_id,
        handlers_failed=list(outcome.handlers_failed),
    )
