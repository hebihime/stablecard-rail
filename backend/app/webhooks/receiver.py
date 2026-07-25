"""The webhook receiver (SPEC.md §4).

    raw body -> verify -> dedup -> parse -> ledger -> dispatch

Four decisions worth stating, because each one is the answer to a way this goes
wrong in production:

**Nothing unauthenticated is recorded.** The ledger is evidence and this endpoint
is open to the internet; a failed signature gets a 401, a log line, and nothing
else. Ledgering rejected traffic would let anyone write to the audit log.

**Dedup runs before parse**, on an id read from the envelope. That is what makes an
authentic-but-unreadable delivery safe: it is recorded once as `unmapped` and never
retried, instead of failing to parse forever. Adapters that cannot supply an
envelope id fall back to a digest of the body.

**The ledger write is the commit point.** Dispatch happens after it, so a handler
never sees an event that is not yet durable, and a handler failure cannot roll back
the record of what arrived.

**An authentic delivery is never dropped.** If we cannot read it, it is stored as
`unmapped` with the raw bytes attached (SPEC.md §3.3).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.issuers.base import CardEvent, CardEventType, CardIssuerAdapter, WebhookParseError
from app.issuers.registry import get_adapter
from app.ledger import event_types
from app.ledger.writer import find_by_idempotency_key, record
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.dedup import DedupGate, ledger_idempotency_key
from app.webhooks.dispatch import dispatch
from app.webhooks.retry import RetryQueue

__all__ = ["DeliveryOutcome", "SignatureRejected", "receive"]

logger = logging.getLogger(__name__)

_LEDGER_IDEMPOTENCY_CONSTRAINT = "uq_ledger_events_idempotency_key"


class SignatureRejected(Exception):
    """The delivery did not authenticate. Answered with 401.

    Carries no detail about *why*: telling a caller which part of their forgery was
    closest is free help for them.
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"webhook signature verification failed for provider {provider_id!r}")
        self.provider_id = provider_id


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What happened to one delivery. The provider sees 200 for all of these."""

    provider_id: str
    event_id: str
    event_type: CardEventType
    duplicate: bool
    ledger_event_id: int | None
    #: `None` for a duplicate: it was published the first time.
    stream_id: str | None
    handlers_ran: tuple[str, ...] = ()
    handlers_failed: tuple[str, ...] = ()


async def receive(
    session: AsyncSession,
    redis: Redis,
    *,
    provider_id: str,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime | None = None,
) -> DeliveryOutcome:
    """Run one delivery through the pipeline. Commits.

    Raises:
        UnknownProviderError: no adapter for `provider_id` (404).
        SignatureRejected: verification failed (401).
    """
    adapter = get_adapter(provider_id)
    now = now or datetime.now(UTC)

    if not await adapter.verify_webhook(headers, body):
        logger.warning("rejected unverified webhook provider=%s bytes=%s", provider_id, len(body))
        raise SignatureRejected(provider_id)

    event_id = _resolve_event_id(adapter, headers, body)
    idempotency_key = ledger_idempotency_key(provider_id, event_id)
    gate = DedupGate(redis)

    if not await gate.claim(provider_id, event_id):
        return await _already_seen(session, adapter, provider_id, event_id, body, idempotency_key)

    event, parse_error = await _normalize(adapter, provider_id, event_id, body, now)

    try:
        entry = await record(
            session,
            event_type=event_types.provider_event(event.event_type.value),
            occurred_at=event.occurred_at,
            provider_id=provider_id,
            cardholder_id=event.cardholder_id,
            card_id=event.card_id,
            state_after=event.card_state.value if event.card_state else None,
            amount=event.amount,
            idempotency_key=idempotency_key,
            payload={
                "raw": event.raw,
                "provider_event_type": event.provider_event_type,
                "funding_ref": event.funding_ref,
                "challenge_id": event.challenge_id,
                "parse_error": parse_error,
            },
        )
        ledger_event_id = entry.id
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _LEDGER_IDEMPOTENCY_CONSTRAINT not in str(exc.orig):
            await gate.release(provider_id, event_id)
            raise
        # Redis had forgotten the claim but the ledger had not: the durable layer
        # catching what the cache missed (SPEC.md §4).
        return await _already_seen(session, adapter, provider_id, event_id, body, idempotency_key)
    except Exception:
        # Claimed, but the work did not land. Give the claim back so the provider's
        # redelivery is treated as new rather than as a duplicate.
        await session.rollback()
        await gate.release(provider_id, event_id)
        raise

    report = await dispatch(
        event,
        bus=RedisStreamsEventBus(redis),
        retry_queue=RetryQueue(redis),
        now=now,
    )
    logger.info(
        "webhook accepted provider=%s event=%s type=%s ledger=%s",
        provider_id,
        event_id,
        event.event_type,
        ledger_event_id,
    )
    return DeliveryOutcome(
        provider_id=provider_id,
        event_id=event_id,
        event_type=event.event_type,
        duplicate=False,
        ledger_event_id=ledger_event_id,
        stream_id=report.stream_id,
        handlers_ran=report.ran,
        handlers_failed=report.failed,
    )


def _resolve_event_id(adapter: CardIssuerAdapter, headers: Mapping[str, str], body: bytes) -> str:
    """The dedup identity of a delivery.

    Prefer the provider's envelope id. Falling back to a body digest is weaker —
    two genuinely distinct events with identical bytes would collapse into one —
    but it is bounded and honest, and providers that omit an id give us nothing
    better to key on.
    """
    return adapter.webhook_event_id(headers, body) or hashlib.sha256(body).hexdigest()


async def _normalize(
    adapter: CardIssuerAdapter,
    provider_id: str,
    event_id: str,
    body: bytes,
    now: datetime,
) -> tuple[CardEvent, str | None]:
    """Parse a verified delivery, or fabricate an `unmapped` event for it.

    A body that will not parse is still authentic, so it is recorded rather than
    rejected — with the bytes base64-encoded, because they may not be text at all
    and the ledger's payload column is JSON.
    """
    try:
        return await adapter.parse_webhook(body), None
    except WebhookParseError as exc:
        logger.warning(
            "unreadable webhook body provider=%s event=%s: %s", provider_id, event_id, exc
        )
        unmapped = CardEvent(
            provider_id=provider_id,
            event_id=event_id,
            event_type=CardEventType.UNMAPPED,
            occurred_at=now,
            raw={"body_base64": base64.b64encode(body).decode()},
        )
        return unmapped, str(exc)


async def _already_seen(
    session: AsyncSession,
    adapter: CardIssuerAdapter,
    provider_id: str,
    event_id: str,
    body: bytes,
    idempotency_key: str,
) -> DeliveryOutcome:
    """Report a duplicate without touching anything.

    The body is parsed only to name the event type in the response — parsing is
    pure, and an operator reading a duplicate needs to know what it was a
    duplicate of.
    """
    original = await find_by_idempotency_key(session, idempotency_key)
    event, _ = await _normalize(adapter, provider_id, event_id, body, datetime.now(UTC))
    logger.info("ignoring duplicate webhook provider=%s event=%s", provider_id, event_id)
    return DeliveryOutcome(
        provider_id=provider_id,
        event_id=event_id,
        event_type=event.event_type,
        duplicate=True,
        ledger_event_id=original.id if original is not None else None,
        stream_id=None,
    )
