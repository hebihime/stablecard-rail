"""Handler retries, backoff, and the dead-letter table (SPEC.md §4).

Failed handling goes to a retry queue with exponential backoff, and what survives
the backoff lands in a dead-letter table rather than vanishing. The dead-letter row
is the whole point: an event that could not be processed after every attempt is an
operational fact someone has to see, and Redis is not where facts live.

Time is passed in, never slept on — a test that waits 512 seconds is a test nobody
runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.money import Money
from app.issuers.base import CardEvent, CardEventType
from app.webhooks import dispatch
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.retry import RetryItem, RetryQueue, delay_for
from tests.support import all_ledger_events, dead_letters

START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
BACKOFF = get_settings().webhook_retry_backoff_seconds


def make_event(event_id: str = "evt_1") -> CardEvent:
    return CardEvent(
        provider_id="gnosis_pay_mock",
        event_id=event_id,
        event_type=CardEventType.SETTLEMENT,
        occurred_at=START,
        card_id="card_000001",
        amount=Money(2500, "USD"),
        funding_ref="intent-abc",
    )


async def fail_once(redis: Redis, *, now: datetime = START, event_id: str = "evt_1") -> None:
    """Dispatch an event to a handler that always throws."""

    async def boom(event: CardEvent) -> None:
        raise RuntimeError("downstream is down")

    if not dispatch.handlers_for(CardEventType.SETTLEMENT):
        dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", boom)
    await dispatch.dispatch(
        make_event(event_id),
        bus=RedisStreamsEventBus(redis),
        retry_queue=RetryQueue(redis),
        now=now,
    )


# ---------------------------------------------------------------- backoff ----


def test_the_backoff_schedule_is_configured_and_increasing() -> None:
    assert len(BACKOFF) >= 3
    assert list(BACKOFF) == sorted(BACKOFF)
    assert BACKOFF[0] > 0


async def test_a_first_failure_is_due_after_the_first_delay(redis_client: Redis) -> None:
    queue = RetryQueue(redis_client)
    await fail_once(redis_client)

    assert await queue.due(now=START + timedelta(seconds=BACKOFF[0] - 1)) == []
    assert len(await queue.due(now=START + timedelta(seconds=BACKOFF[0]))) == 1


async def test_claiming_a_due_item_takes_it_out_of_the_queue(redis_client: Redis) -> None:
    # Two workers draining concurrently must not both run the same handler.
    queue = RetryQueue(redis_client)
    await fail_once(redis_client)
    later = START + timedelta(hours=1)

    assert len(await queue.due(now=later)) == 1
    assert await queue.due(now=later) == []
    assert await queue.size() == 0


async def test_the_queued_item_carries_everything_needed_to_retry(
    redis_client: Redis,
) -> None:
    # No pointer into a table that might be gone: the item is self-contained, so a
    # retry works even if the process that queued it never comes back.
    await fail_once(redis_client)
    (item,) = await RetryQueue(redis_client).due(now=START + timedelta(hours=1))

    assert item.provider_id == "gnosis_pay_mock"
    assert item.handler == "reconcile"
    assert item.attempts == 1
    assert item.event == make_event()
    assert item.event.amount == Money(2500, "USD")


def test_items_round_trip_through_their_wire_form() -> None:
    item = RetryItem(
        provider_id="gnosis_pay_mock",
        handler="reconcile",
        attempts=2,
        last_error="boom",
        event=make_event(),
    )
    assert RetryItem.from_json(item.to_json()) == item


# ----------------------------------------------------------------- draining ----


async def test_draining_reruns_the_handler_and_clears_it_on_success(
    session: AsyncSession, redis_client: Redis
) -> None:
    attempts: list[str] = []

    async def flaky(event: CardEvent) -> None:
        attempts.append(event.event_id)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")

    dispatch.subscribe(CardEventType.SETTLEMENT, "reconcile", flaky)
    await dispatch.dispatch(
        make_event(),
        bus=RedisStreamsEventBus(redis_client),
        retry_queue=RetryQueue(redis_client),
        now=START,
    )

    report = await dispatch.drain_due(
        session, redis_client, now=START + timedelta(seconds=BACKOFF[0])
    )

    assert report.succeeded == ("reconcile",)
    assert attempts == ["evt_1", "evt_1"]
    assert await RetryQueue(redis_client).size() == 0
    assert await dead_letters(session) == []


async def test_a_repeat_failure_is_rescheduled_with_the_next_delay(
    session: AsyncSession, redis_client: Redis
) -> None:
    queue = RetryQueue(redis_client)
    await fail_once(redis_client)

    first_due = START + timedelta(seconds=BACKOFF[0])
    report = await dispatch.drain_due(session, redis_client, now=first_due)

    assert report.rescheduled == ("reconcile",)
    assert await queue.due(now=first_due + timedelta(seconds=BACKOFF[1] - 1)) == []
    (item,) = await queue.due(now=first_due + timedelta(seconds=BACKOFF[1]))
    assert item.attempts == 2


async def test_nothing_due_is_a_cheap_no_op(session: AsyncSession, redis_client: Redis) -> None:
    report = await dispatch.drain_due(session, redis_client, now=START)
    assert report.succeeded == report.rescheduled == report.dead_lettered == ()


# -------------------------------------------------------------- dead letters ----


async def _exhaust(session: AsyncSession, redis_client: Redis) -> None:
    """Fail through every configured attempt, until the queue gives up.

    Drains with a generous clock rather than a hand-computed schedule, so this
    stays correct if the backoff table is retuned.
    """
    await fail_once(redis_client)
    queue = RetryQueue(redis_client)
    now = START
    for _ in range(len(BACKOFF) + 2):
        if await queue.size() == 0:
            return
        now += timedelta(hours=1)
        await dispatch.drain_due(session, redis_client, now=now)
    raise AssertionError("the retry queue never gave up")


async def test_exhausting_the_backoff_dead_letters_the_delivery(
    session: AsyncSession, redis_client: Redis
) -> None:
    await _exhaust(session, redis_client)

    (letter,) = await dead_letters(session)
    assert letter.provider_id == "gnosis_pay_mock"
    assert letter.event_id == "evt_1"
    assert letter.handler == "reconcile"
    assert letter.event_type == CardEventType.SETTLEMENT.value
    # One inline attempt plus one retry per configured backoff step.
    assert letter.attempts == len(BACKOFF) + 1
    assert "downstream is down" in (letter.last_error or "")
    # The event itself is stored, so it can be replayed by hand after the fix.
    assert letter.event["event_id"] == "evt_1"
    assert letter.event["amount"] == {"amount_minor": 2500, "currency": "USD"}


async def test_a_dead_lettered_delivery_leaves_the_queue(
    session: AsyncSession, redis_client: Redis
) -> None:
    await _exhaust(session, redis_client)
    assert await RetryQueue(redis_client).size() == 0


async def test_dead_lettering_is_ledgered(session: AsyncSession, redis_client: Redis) -> None:
    # SPEC.md §7: a delivery we gave up on is exactly the kind of thing the
    # ledger exists to make undeniable.
    await _exhaust(session, redis_client)

    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == ["webhook.dead_lettered"]
    assert events[0].payload["handler"] == "reconcile"
    assert events[0].payload["attempts"] == len(BACKOFF) + 1
    assert events[0].card_id == "card_000001"


async def test_dead_lettering_the_same_delivery_twice_is_idempotent(
    session: AsyncSession, redis_client: Redis
) -> None:
    # A second worker draining the same item, or a replay after a crash, must not
    # produce two rows for one failure.
    await _exhaust(session, redis_client)
    await _exhaust(session, redis_client)
    assert len(await dead_letters(session)) == 1


async def test_a_handler_that_no_longer_exists_is_dead_lettered_immediately(
    session: AsyncSession, redis_client: Redis
) -> None:
    # A deploy that removes a handler must not leave items cycling forever
    # against a name nothing answers to.
    await fail_once(redis_client)
    dispatch.clear_subscriptions()

    report = await dispatch.drain_due(
        session, redis_client, now=START + timedelta(seconds=BACKOFF[0])
    )

    assert report.dead_lettered == ("reconcile",)
    (letter,) = await dead_letters(session)
    assert "no longer subscribed" in (letter.last_error or "")


async def test_two_failing_handlers_are_retried_independently(
    session: AsyncSession, redis_client: Redis
) -> None:
    async def boom(event: CardEvent) -> None:
        raise RuntimeError("down")

    async def recovers(event: CardEvent) -> None:
        if not recovered:
            recovered.append(True)
            raise RuntimeError("down, briefly")

    recovered: list[bool] = []
    dispatch.subscribe(CardEventType.SETTLEMENT, "boom", boom)
    dispatch.subscribe(CardEventType.SETTLEMENT, "recovers", recovers)
    await dispatch.dispatch(
        make_event(),
        bus=RedisStreamsEventBus(redis_client),
        retry_queue=RetryQueue(redis_client),
        now=START,
    )

    report = await dispatch.drain_due(
        session, redis_client, now=START + timedelta(seconds=BACKOFF[0])
    )

    assert sorted(report.succeeded) == ["recovers"]
    assert sorted(report.rescheduled) == ["boom"]


async def test_the_drain_limit_is_respected(session: AsyncSession, redis_client: Redis) -> None:
    # A drain must be bounded: an unbounded one turns a backlog into a stall.
    for index in range(3):
        await fail_once(redis_client, event_id=f"evt_{index}")
    now = START + timedelta(hours=1)

    first = await dispatch.drain_due(session, redis_client, now=now, limit=2)
    # The two it handled are back in the queue, but due later — so a second pass at
    # the same instant sees only the one it did not reach.
    second = await dispatch.drain_due(session, redis_client, now=now, limit=2)

    assert len(first.rescheduled) == 2
    assert len(second.rescheduled) == 1
    assert await RetryQueue(redis_client).size() == 3


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_the_delay_grows_with_the_attempt_count(attempts: int) -> None:
    assert delay_for(attempts) == BACKOFF[attempts - 1]


def test_the_delay_is_capped_rather_than_indexing_off_the_end() -> None:
    # Reachable only if the cap and the table ever disagree; a crash here would
    # strand the item in the queue instead of dead-lettering it.
    assert delay_for(len(BACKOFF) + 5) == BACKOFF[-1]
