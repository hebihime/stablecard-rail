"""The event bus (SPEC.md §2, §4).

Kafka is deliberately out of scope for this demo, so an `EventBus` interface with
a Redis Streams implementation stands in. The interface is the architectural
point: consumers (the funding engine in phase 5, the OTP service in phase 7)
subscribe to a stream of normalized `CardEvent`s and never learn what carries
them, so a Kafka implementation would be a drop-in.

Redis Streams is a genuine fit rather than a fudge: entries are ordered, ids are
monotonic, and a consumer can resume from the last id it processed — the three
properties a replayable log needs. What it lacks against Kafka is partitioning,
retention policy and multi-broker durability, which is exactly the difference this
demo is not trying to hide.

Events ship as JSON. A stream entry's fields must be flat strings, so the whole
event goes in one field rather than being spread across many — which also means
adding a field to `CardEvent` needs no change here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

from app.issuers.base import CardEvent

__all__ = ["EventBus", "PublishedEvent", "RedisStreamsEventBus"]

#: Consumers subscribe by name, so this is a compatibility surface.
DEFAULT_STREAM = "stablecard:card_events"

#: Redis has no per-entry TTL, so an uncapped stream is an unbounded memory leak.
DEFAULT_MAXLEN = 10_000

_EVENT_FIELD = "event"


@dataclass(frozen=True, slots=True)
class PublishedEvent:
    """An event as read back off the bus, with the id a consumer resumes from."""

    stream_id: str
    event: CardEvent


class EventBus(ABC):
    @abstractmethod
    async def publish(self, event: CardEvent) -> str:
        """Append an event, returning its id on the stream."""

    @abstractmethod
    async def read(self, *, after: str = "0", count: int = 100) -> list[PublishedEvent]:
        """Read up to `count` events published after `after` (exclusive)."""


class RedisStreamsEventBus(EventBus):
    def __init__(
        self, redis: Redis, *, stream: str = DEFAULT_STREAM, maxlen: int = DEFAULT_MAXLEN
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    @property
    def stream(self) -> str:
        return self._stream

    async def publish(self, event: CardEvent) -> str:
        # Exact trimming rather than approximate: at demo scale the cost is
        # irrelevant, and an exact bound is one that can be asserted.
        stream_id = await self._redis.xadd(
            self._stream,
            {_EVENT_FIELD: event.model_dump_json()},
            maxlen=self._maxlen,
            approximate=False,
        )
        return str(stream_id)

    async def read(self, *, after: str = "0", count: int = 100) -> list[PublishedEvent]:
        # `(` is Redis' exclusive-range prefix: resume *after* what we have seen.
        start = f"({after}" if after != "0" else "-"
        # redis-py types its return around `decode_responses`, which it cannot see;
        # the client this class is given always decodes (see app/core/redis.py).
        entries = cast(
            "list[tuple[str, dict[str, str]]]",
            await self._redis.xrange(self._stream, min=start, max="+", count=count),
        )
        return [
            PublishedEvent(
                stream_id=str(stream_id),
                event=CardEvent.model_validate_json(fields[_EVENT_FIELD]),
            )
            for stream_id, fields in entries
        ]
