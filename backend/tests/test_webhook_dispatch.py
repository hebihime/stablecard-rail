"""Dispatch: publish, then run handlers (SPEC.md §4).

The rule that shapes all of this: **after verification succeeds, the provider gets
a 2xx.** A handler that throws is our problem, not theirs — retrying the whole
delivery would re-run the handlers that already succeeded, and answering 5xx would
make a provider with exponential backoff eventually give up on us entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.issuers.base import CardEvent, CardEventType
from app.main import create_app
from app.webhooks import dispatch
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.retry import RetryQueue


def make_event(
    event_type: CardEventType = CardEventType.AUTHORIZATION, event_id: str = "evt_1"
) -> CardEvent:
    return CardEvent(
        provider_id="gnosis_pay_mock",
        event_id=event_id,
        event_type=event_type,
        occurred_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        card_id="card_000001",
        amount=Money(1299, "USD"),
    )


async def run(redis: Redis, event: CardEvent) -> dispatch.DispatchReport:
    return await dispatch.dispatch(
        event,
        bus=RedisStreamsEventBus(redis),
        retry_queue=RetryQueue(redis),
        now=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )


# ------------------------------------------------------------ subscriptions ----


def test_starting_the_app_registers_exactly_the_consumers_that_exist() -> None:
    # Phase 2 built the pipe and no consumers, and this test asserted an empty
    # tuple on the stated grounds that "the funding engine subscribes in phase 5
    # and the OTP service in phase 7". Both have arrived, so the guard names both —
    # and still fails if a consumer is wired ahead of its phase, or if one is
    # written and then never registered, which is what it is for.
    create_app()

    assert dispatch.subscriptions() == (
        (CardEventType.SETTLEMENT, "funding.settle"),
        (CardEventType.THREE_DS_CHALLENGE, "otp.deliver"),
    )


def test_a_handler_is_registered_against_one_event_type() -> None:
    async def handler(event: CardEvent) -> None: ...

    dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", handler)
    assert [name for _, name in dispatch.subscriptions()] == ["reconcile"]
    assert dispatch.handlers_for(CardEventType.SETTLEMENT) == (("reconcile", handler),)
    assert dispatch.handlers_for(CardEventType.AUTHORIZATION) == ()


def test_two_handlers_may_not_share_a_name_for_one_event_type() -> None:
    # The name is the retry queue's handle on a handler, so it has to be unique.
    async def handler(event: CardEvent) -> None: ...

    dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", handler)
    with pytest.raises(ValueError, match="already subscribed"):
        dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", handler)


def test_the_same_name_on_a_different_event_type_is_fine() -> None:
    async def handler(event: CardEvent) -> None: ...

    dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", handler)
    dispatch.subscribe(CardEventType.REFUND, "reconcile", handler)
    assert len(dispatch.subscriptions()) == 2


# ---------------------------------------------------------------- dispatch ----


async def test_handlers_receive_the_event(redis_client: Redis) -> None:
    seen: list[CardEvent] = []

    async def handler(event: CardEvent) -> None:
        seen.append(event)

    dispatch.subscribe(CardEventType.AUTHORIZATION, "watcher", handler)
    event = make_event()

    report = await run(redis_client, event)

    assert seen == [event]
    assert report.ran == ("watcher",)
    assert report.failed == ()


async def test_handlers_for_other_types_stay_out_of_it(redis_client: Redis) -> None:
    async def handler(event: CardEvent) -> None:
        raise AssertionError("must not be called")

    dispatch.subscribe(CardEventType.CHARGEBACK, "chargebacks", handler)
    report = await run(redis_client, make_event(CardEventType.AUTHORIZATION))
    assert report.ran == ()


async def test_the_event_reaches_the_bus_even_with_no_handlers(redis_client: Redis) -> None:
    # Publishing is not conditional on anyone listening yet: a consumer added in
    # phase 5 can replay the stream from the beginning.
    report = await run(redis_client, make_event())
    assert report.stream_id
    assert await redis_client.xlen("stablecard:card_events") == 1


async def test_a_failing_handler_is_reported_not_raised(redis_client: Redis) -> None:
    async def boom(event: CardEvent) -> None:
        raise RuntimeError("downstream is down")

    dispatch.subscribe(CardEventType.AUTHORIZATION, "boom", boom)

    report = await run(redis_client, make_event())

    assert report.failed == ("boom",)
    assert report.ran == ()


async def test_one_failing_handler_does_not_block_another(redis_client: Redis) -> None:
    survived: list[str] = []

    async def boom(event: CardEvent) -> None:
        raise RuntimeError("downstream is down")

    async def fine(event: CardEvent) -> None:
        survived.append(event.event_id)

    dispatch.subscribe(CardEventType.AUTHORIZATION, "boom", boom)
    dispatch.subscribe(CardEventType.AUTHORIZATION, "fine", fine)

    report = await run(redis_client, make_event())

    assert survived == ["evt_1"]
    assert report.ran == ("fine",)
    assert report.failed == ("boom",)


async def test_a_failing_handler_is_queued_for_retry(redis_client: Redis) -> None:
    async def boom(event: CardEvent) -> None:
        raise RuntimeError("downstream is down")

    dispatch.subscribe(CardEventType.AUTHORIZATION, "boom", boom)

    await run(redis_client, make_event())

    queue = RetryQueue(redis_client)
    assert await queue.size() == 1
    (item,) = await queue.due(now=datetime(2026, 7, 25, 12, 1, tzinfo=UTC))
    assert item.handler == "boom"
    assert item.attempts == 1
    assert "downstream is down" in item.last_error
    assert item.event == make_event()


async def test_only_the_failing_handler_is_queued(redis_client: Redis) -> None:
    async def boom(event: CardEvent) -> None:
        raise RuntimeError("nope")

    async def fine(event: CardEvent) -> None: ...

    dispatch.subscribe(CardEventType.AUTHORIZATION, "boom", boom)
    dispatch.subscribe(CardEventType.AUTHORIZATION, "fine", fine)

    await run(redis_client, make_event())

    items = await RetryQueue(redis_client).due(now=datetime(2026, 7, 25, 13, 0, tzinfo=UTC))
    assert [item.handler for item in items] == ["boom"]


async def test_handlers_run_in_subscription_order(redis_client: Redis) -> None:
    # Not a guarantee handlers may rely on — they must be independent — but a
    # stable order makes failures reproducible.
    order: list[str] = []

    async def first(event: CardEvent) -> None:
        order.append("first")

    async def second(event: CardEvent) -> None:
        order.append("second")

    dispatch.subscribe(CardEventType.AUTHORIZATION, "first", first)
    dispatch.subscribe(CardEventType.AUTHORIZATION, "second", second)

    await run(redis_client, make_event())
    assert order == ["first", "second"]


async def test_clearing_subscriptions_removes_everything(
    redis_client: Redis, session: AsyncSession
) -> None:
    async def handler(event: CardEvent) -> None:
        raise AssertionError("must not be called")

    dispatch.subscribe(CardEventType.AUTHORIZATION, "gone", handler)
    dispatch.clear_subscriptions()

    assert dispatch.subscriptions() == ()
    assert (await run(redis_client, make_event())).ran == ()
