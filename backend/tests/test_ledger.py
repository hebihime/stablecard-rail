"""The event ledger (SPEC.md §7): append-only, uniquely keyed, UTC."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.ledger.models import LedgerEvent
from app.ledger.writer import find_by_idempotency_key, record


async def test_record_defaults_occurred_at_to_utc_now(session: AsyncSession) -> None:
    before = datetime.now(UTC)
    event = await record(session, event_type="test.event")
    await session.commit()

    assert event.occurred_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert before <= event.occurred_at <= datetime.now(UTC)


async def test_record_round_trips_every_field(session: AsyncSession) -> None:
    intent_id = uuid.uuid4()
    occurred = datetime(2026, 7, 25, 12, 30, tzinfo=UTC)
    await record(
        session,
        event_type="card.authorization",
        occurred_at=occurred,
        provider_id="lithic",
        cardholder_id="ch_1",
        card_id="card_1",
        intent_id=intent_id,
        state_before="FUNDING",
        state_after="FUNDED",
        amount=Money(-1250, "usd"),
        payload={"nested": {"merchant": "Acme"}, "n": 1},
        idempotency_key="lithic:evt_1",
    )
    await session.commit()

    stored = (await session.execute(select(LedgerEvent))).scalar_one()
    assert stored.event_type == "card.authorization"
    assert stored.occurred_at == occurred
    assert stored.provider_id == "lithic"
    assert stored.cardholder_id == "ch_1"
    assert stored.card_id == "card_1"
    assert stored.intent_id == intent_id
    assert stored.state_before == "FUNDING"
    assert stored.state_after == "FUNDED"
    assert stored.amount_minor == -1250
    assert stored.currency == "USD"
    assert stored.payload == {"nested": {"merchant": "Acme"}, "n": 1}
    assert stored.idempotency_key == "lithic:evt_1"
    assert stored.recorded_at is not None


async def test_payload_defaults_to_empty_object(session: AsyncSession) -> None:
    event = await record(session, event_type="test.event")
    await session.commit()
    assert event.payload == {}


async def test_idempotency_key_is_unique(session: AsyncSession) -> None:
    await record(session, event_type="test.event", idempotency_key="dupe")
    await session.commit()
    # `record()` flushes, so the collision surfaces at the write, not at commit.
    with pytest.raises(IntegrityError):
        await record(session, event_type="test.event", idempotency_key="dupe")
    await session.rollback()


async def test_unkeyed_events_do_not_collide(session: AsyncSession) -> None:
    for _ in range(3):
        await record(session, event_type="test.event")
    await session.commit()
    count = await session.scalar(select(func.count()).select_from(LedgerEvent))
    assert count == 3


async def test_find_by_idempotency_key(session: AsyncSession) -> None:
    await record(session, event_type="test.event", idempotency_key="known")
    await session.commit()
    assert await find_by_idempotency_key(session, "known") is not None
    assert await find_by_idempotency_key(session, "unknown") is None


async def test_ids_are_monotonic_in_insertion_order(session: AsyncSession) -> None:
    first = await record(session, event_type="a")
    second = await record(session, event_type="b")
    await session.commit()
    assert second.id > first.id


async def test_update_is_blocked_at_the_database_level(session: AsyncSession) -> None:
    event = await record(session, event_type="test.event")
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            text("UPDATE ledger_events SET event_type = 'tampered' WHERE id = :id"),
            {"id": event.id},
        )
    await session.rollback()


async def test_delete_is_blocked_at_the_database_level(session: AsyncSession) -> None:
    event = await record(session, event_type="test.event")
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("DELETE FROM ledger_events WHERE id = :id"), {"id": event.id})
    await session.rollback()


async def test_naive_timestamps_are_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="UTC"):
        await record(
            session,
            event_type="test.event",
            occurred_at=datetime(2026, 7, 25, 12, 0),  # naive on purpose
        )
