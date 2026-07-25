"""The `EventBus` (SPEC.md §2, §4).

Redis Streams stands in for Kafka. The interface is the architectural point: a
Kafka implementation would be a drop-in, and the receiver never learns which one
it published to. What matters functionally is that a `CardEvent` survives the
round trip byte-for-byte — a consumer in phase 5 will act on money based on what
comes back out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.money import Money
from app.issuers.base import CardEvent, CardEventType, CardState
from app.webhooks.bus import EventBus, RedisStreamsEventBus


def make_event(**overrides: object) -> CardEvent:
    defaults: dict[str, object] = {
        "provider_id": "gnosis_pay_mock",
        "event_id": "evt_000001",
        "event_type": CardEventType.SETTLEMENT,
        "occurred_at": datetime(2026, 7, 25, 12, 0, 0, 123456, tzinfo=UTC),
        "card_id": "card_000001",
        "amount": Money(2500, "USD"),
        "funding_ref": "intent-abc",
    }
    return CardEvent(**(defaults | overrides))  # type: ignore[arg-type]


def test_the_redis_implementation_satisfies_the_interface(redis_client: Redis) -> None:
    assert isinstance(RedisStreamsEventBus(redis_client), EventBus)


async def test_publish_then_read_returns_an_identical_event(redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(redis_client)
    event = make_event(raw={"nested": {"merchant": "Coffee", "mcc": "5814"}})

    stream_id = await bus.publish(event)
    published = await bus.read()

    assert [entry.event for entry in published] == [event]
    assert published[0].stream_id == stream_id


async def test_money_survives_the_round_trip_as_integer_minor_units(
    redis_client: Redis,
) -> None:
    bus = RedisStreamsEventBus(redis_client)
    await bus.publish(make_event(amount=Money(-1299, "EUR")))
    amount = (await bus.read())[0].event.amount
    assert amount == Money(-1299, "EUR")
    assert isinstance(amount.amount_minor, int)


async def test_microsecond_precision_and_utc_survive_the_round_trip(
    redis_client: Redis,
) -> None:
    bus = RedisStreamsEventBus(redis_client)
    await bus.publish(make_event())
    occurred_at = (await bus.read())[0].event.occurred_at
    assert occurred_at == datetime(2026, 7, 25, 12, 0, 0, 123456, tzinfo=UTC)


async def test_enum_fields_survive_the_round_trip(redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(redis_client)
    await bus.publish(
        make_event(event_type=CardEventType.CARD_LIFECYCLE, card_state=CardState.FROZEN)
    )
    event = (await bus.read())[0].event
    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_state is CardState.FROZEN


async def test_events_are_read_back_in_publication_order(redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(redis_client)
    for index in range(3):
        await bus.publish(make_event(event_id=f"evt_{index}"))
    assert [entry.event.event_id for entry in await bus.read()] == ["evt_0", "evt_1", "evt_2"]


async def test_reading_after_a_stream_id_resumes_where_a_consumer_left_off(
    redis_client: Redis,
) -> None:
    bus = RedisStreamsEventBus(redis_client)
    first = await bus.publish(make_event(event_id="evt_0"))
    await bus.publish(make_event(event_id="evt_1"))

    resumed = await bus.read(after=first)
    assert [entry.event.event_id for entry in resumed] == ["evt_1"]


async def test_reading_an_empty_stream_is_not_an_error(redis_client: Redis) -> None:
    assert await RedisStreamsEventBus(redis_client).read() == []


async def test_the_stream_name_is_stable(redis_client: Redis) -> None:
    # Consumers subscribe by name; changing it silently orphans them.
    bus = RedisStreamsEventBus(redis_client)
    await bus.publish(make_event())
    assert bus.stream == "stablecard:card_events"
    assert await redis_client.xlen("stablecard:card_events") == 1


async def test_the_stream_is_capped_so_it_cannot_grow_forever(redis_client: Redis) -> None:
    # Redis has no TTL per stream entry, so an uncapped stream is an unbounded
    # memory leak. The cap keeps the newest entries, which is what a consumer
    # catching up cares about.
    bus = RedisStreamsEventBus(redis_client, maxlen=2)
    for index in range(6):
        await bus.publish(make_event(event_id=f"evt_{index}"))

    assert await redis_client.xlen(bus.stream) == 2
    assert [entry.event.event_id for entry in await bus.read()] == ["evt_4", "evt_5"]
