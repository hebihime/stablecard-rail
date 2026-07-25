"""The handler retry queue and dead-letter writer (SPEC.md §4).

A Redis sorted set scored by "when this is next due". That gives the two
operations a retry queue needs — "what is due now" and "claim it exactly once" —
without a poller scanning a table or a second piece of infrastructure.

Items are **self-contained**: the whole normalized event travels with the retry, so
a handler can be re-run by a different process, after a restart, or from a
dead-letter row copied out by hand. Nothing here points at state that might be gone.

Time is always passed in. Retry code that reads the clock itself can only be
tested by waiting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.issuers.base import CardEvent
from app.ledger import event_types
from app.ledger.writer import record
from app.webhooks.models import WebhookDeadLetter

__all__ = ["DEFAULT_QUEUE_KEY", "RetryItem", "RetryQueue", "dead_letter", "delay_for"]

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_KEY = "webhook:retries"


def backoff_seconds() -> tuple[int, ...]:
    return get_settings().webhook_retry_backoff_seconds


def delay_for(attempts: int, backoff: tuple[int, ...] | None = None) -> int:
    """Seconds to wait before retry number `attempts`.

    Clamped rather than indexed blindly: an out-of-range attempt count should mean
    "wait the longest delay", never an `IndexError` that strands the item in the
    queue instead of dead-lettering it.
    """
    schedule = backoff or backoff_seconds()
    return schedule[min(max(attempts, 1), len(schedule)) - 1]


@dataclass(frozen=True, slots=True)
class RetryItem:
    """One handler's failed attempt at one event."""

    provider_id: str
    handler: str
    #: Failures so far, counting the inline attempt made during dispatch.
    attempts: int
    last_error: str
    event: CardEvent

    def to_json(self) -> str:
        # sort_keys: the JSON string *is* the sorted-set member, so it has to be
        # byte-identical to what we later remove.
        return json.dumps(
            {
                "provider_id": self.provider_id,
                "handler": self.handler,
                "attempts": self.attempts,
                "last_error": self.last_error,
                "event": self.event.model_dump(mode="json"),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> RetryItem:
        payload = json.loads(raw)
        return cls(
            provider_id=payload["provider_id"],
            handler=payload["handler"],
            attempts=payload["attempts"],
            last_error=payload["last_error"],
            event=CardEvent.model_validate(payload["event"]),
        )


class RetryQueue:
    """Due-time ordered queue of failed handler runs."""

    def __init__(
        self,
        redis: Redis,
        *,
        key: str = DEFAULT_QUEUE_KEY,
        backoff: tuple[int, ...] | None = None,
    ) -> None:
        self._redis = redis
        self._key = key
        self._backoff = backoff or backoff_seconds()

    @property
    def max_attempts(self) -> int:
        """The inline attempt, plus one retry per configured backoff step."""
        return len(self._backoff) + 1

    def delay_for(self, attempts: int) -> int:
        return delay_for(attempts, self._backoff)

    async def size(self) -> int:
        return int(await self._redis.zcard(self._key))

    async def schedule(self, item: RetryItem, *, now: datetime) -> datetime:
        """Queue a retry, returning when it becomes due."""
        due_at = now.timestamp() + self.delay_for(item.attempts)
        await self._redis.zadd(self._key, {item.to_json(): due_at})
        logger.info(
            "queued webhook retry provider=%s event=%s handler=%s attempt=%s in %ss",
            item.provider_id,
            item.event.event_id,
            item.handler,
            item.attempts,
            self.delay_for(item.attempts),
        )
        return datetime.fromtimestamp(due_at, tz=now.tzinfo)

    async def due(self, *, now: datetime, limit: int = 100) -> list[RetryItem]:
        """Claim up to `limit` due items, removing them from the queue.

        Claiming on read is what keeps two workers from running the same handler
        twice; a worker that dies mid-retry loses the item, which is why the
        handler contract is idempotence rather than exactly-once delivery.
        """
        # The client this queue is given always decodes responses (app/core/redis.py),
        # which redis-py's own annotations cannot express.
        members = cast(
            "list[str]",
            await self._redis.zrangebyscore(
                self._key, min="-inf", max=now.timestamp(), start=0, num=limit
            ),
        )
        if not members:
            return []
        await self._redis.zrem(self._key, *members)
        return [RetryItem.from_json(member) for member in members]


async def dead_letter(session: AsyncSession, item: RetryItem, *, reason: str) -> None:
    """Record a delivery we have given up on. Commits.

    `ON CONFLICT DO NOTHING`: two workers reaching the same conclusion, or a
    replay after a crash, must not produce two rows for one failure.
    """
    statement = (
        pg_insert(WebhookDeadLetter)
        .values(
            provider_id=item.provider_id,
            event_id=item.event.event_id,
            handler=item.handler,
            event_type=item.event.event_type.value,
            attempts=item.attempts,
            last_error=reason,
            event=item.event.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(constraint="uq_webhook_dead_letters_delivery")
    )
    # `rowcount` is 0 when the conflict clause suppressed the insert, which is how
    # we know whether this is the first time we have given up on this delivery.
    result = cast("CursorResult[Any]", await session.execute(statement))
    if result.rowcount:
        # Only ledger a first arrival, so the ledger does not gain a row every
        # time a duplicate is suppressed.
        await record(
            session,
            event_type=event_types.WEBHOOK_DEAD_LETTERED,
            occurred_at=item.event.occurred_at,
            provider_id=item.provider_id,
            card_id=item.event.card_id,
            cardholder_id=item.event.cardholder_id,
            amount=item.event.amount,
            payload={
                "handler": item.handler,
                "attempts": item.attempts,
                "event_id": item.event.event_id,
                "event_type": item.event.event_type.value,
                "reason": reason,
            },
        )
    await session.commit()
    logger.error(
        "dead-lettered webhook provider=%s event=%s handler=%s after %s attempts: %s",
        item.provider_id,
        item.event.event_id,
        item.handler,
        item.attempts,
        reason,
    )
