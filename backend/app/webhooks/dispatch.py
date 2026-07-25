"""Dispatch: publish an event, then run its handlers (SPEC.md §4).

The rule everything here serves: **once verification succeeds, the provider gets a
2xx.** Retrying the whole delivery would re-run handlers that already succeeded,
and a 5xx to a provider with exponential backoff eventually means it gives up on us
entirely. So handler failure is our problem: it goes to the retry queue, keyed to
the individual handler, and the delivery itself is still a success.

**Handlers must be idempotent.** They can be re-run by a retry, by a drain in
another process, or by hand from a dead-letter row. Every handler must document its
idempotency key in a comment at the subscription site, as consumers land:

    # idempotency key: (intent_id, "settled") — `advance()` dedups on it
    subscribe(CardEventType.SETTLEMENT, "funding.settle", settle_intent)

Phase 2 registers none: the funding engine subscribes in phase 5 and the OTP
service in phase 7. The pipe is built and tested; the consumers arrive with their
phases.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.issuers.base import CardEvent, CardEventType
from app.webhooks.bus import EventBus
from app.webhooks.retry import RetryItem, RetryQueue, dead_letter

__all__ = [
    "DispatchReport",
    "DrainReport",
    "Handler",
    "clear_subscriptions",
    "dispatch",
    "drain_due",
    "handlers_for",
    "subscribe",
    "subscriptions",
]

logger = logging.getLogger(__name__)

Handler = Callable[[CardEvent], Awaitable[None]]

#: event type -> {handler name: handler}. Insertion-ordered, so dispatch order is
#: stable — not a guarantee handlers may depend on, but it makes failures
#: reproducible.
_SUBSCRIPTIONS: dict[CardEventType, dict[str, Handler]] = {}


@dataclass(frozen=True, slots=True)
class DispatchReport:
    stream_id: str
    ran: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DrainReport:
    succeeded: tuple[str, ...]
    rescheduled: tuple[str, ...]
    dead_lettered: tuple[str, ...]


def subscribe(
    event_type: CardEventType, name: str, handler: Handler, *, replace: bool = False
) -> None:
    """Register a handler for one event type.

    `name` is how the retry queue refers to the handler, so it must be unique per
    event type and stable across deploys — renaming one strands its queued retries.
    """
    registered = _SUBSCRIPTIONS.setdefault(event_type, {})
    if name in registered and not replace:
        raise ValueError(f"{name!r} is already subscribed to {event_type}")
    registered[name] = handler


def handlers_for(event_type: CardEventType) -> tuple[tuple[str, Handler], ...]:
    return tuple(_SUBSCRIPTIONS.get(event_type, {}).items())


def subscriptions() -> tuple[tuple[CardEventType, str], ...]:
    return tuple(
        (event_type, name)
        for event_type, registered in _SUBSCRIPTIONS.items()
        for name in registered
    )


def clear_subscriptions() -> None:
    """Drop every subscription. For tests; nothing in the app calls this."""
    _SUBSCRIPTIONS.clear()


async def dispatch(
    event: CardEvent,
    *,
    bus: EventBus,
    retry_queue: RetryQueue,
    now: datetime,
) -> DispatchReport:
    """Publish `event`, then run its handlers. Never raises on handler failure.

    Publishing happens first and unconditionally: the stream is the durable record
    of what was dispatched, and a consumer added in a later phase can replay it
    from the beginning.
    """
    stream_id = await bus.publish(event)

    ran: list[str] = []
    failed: list[str] = []
    for name, handler in handlers_for(event.event_type):
        try:
            await handler(event)
        except Exception as exc:
            failed.append(name)
            logger.exception(
                "webhook handler failed provider=%s event=%s handler=%s",
                event.provider_id,
                event.event_id,
                name,
            )
            await retry_queue.schedule(
                RetryItem(
                    provider_id=event.provider_id,
                    handler=name,
                    attempts=1,
                    last_error=f"{type(exc).__name__}: {exc}",
                    event=event,
                ),
                now=now,
            )
        else:
            ran.append(name)

    return DispatchReport(stream_id=stream_id, ran=tuple(ran), failed=tuple(failed))


async def drain_due(
    session: AsyncSession,
    redis: Redis,
    *,
    now: datetime,
    limit: int = 100,
) -> DrainReport:
    """Re-run every retry that has come due.

    Called by `scripts/drain_webhook_retries.py`. Deliberately a plain function
    rather than a background task inside the app: a retry worker that lives in the
    web process is one that dies with it, and one nobody can run by hand.
    """
    queue = RetryQueue(redis)
    succeeded: list[str] = []
    rescheduled: list[str] = []
    dead_lettered: list[str] = []

    for item in await queue.due(now=now, limit=limit):
        handler = dict(handlers_for(item.event.event_type)).get(item.handler)
        if handler is None:
            # A deploy removed the handler. Nothing will ever answer to this name,
            # so cycling it forever would be a slow leak with no upside.
            dead_lettered.append(item.handler)
            await dead_letter(
                session, item, reason=f"handler {item.handler!r} is no longer subscribed"
            )
            continue

        try:
            await handler(item.event)
        except Exception as exc:
            attempted = RetryItem(
                provider_id=item.provider_id,
                handler=item.handler,
                attempts=item.attempts + 1,
                last_error=f"{type(exc).__name__}: {exc}",
                event=item.event,
            )
            if attempted.attempts >= queue.max_attempts:
                dead_lettered.append(item.handler)
                await dead_letter(session, attempted, reason=attempted.last_error)
            else:
                rescheduled.append(item.handler)
                await queue.schedule(attempted, now=now)
        else:
            succeeded.append(item.handler)
            logger.info(
                "webhook retry succeeded provider=%s event=%s handler=%s attempt=%s",
                item.provider_id,
                item.event.event_id,
                item.handler,
                item.attempts + 1,
            )

    return DrainReport(
        succeeded=tuple(succeeded),
        rescheduled=tuple(rescheduled),
        dead_lettered=tuple(dead_lettered),
    )
