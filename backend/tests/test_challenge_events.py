"""What a `THREE_DS_CHALLENGE` event may and may not carry (SPEC.md §6).

Phase 7's first finding is about *durability*, not about OTP mechanics. SPEC.md
§6.2 says the code lives in Redis "with a short TTL" — so the code is the first
value in this system that must **stop existing**. Everything else the pipeline
handles wants to be permanent.

A `CardEvent` reaches four stores on its way through the pipeline, and three of
them outlive a five-minute code:

1. the ledger (`ledger_events`, append-only — it cannot even be redacted later),
2. the `EventBus` stream (Redis, capped at 10k entries and no TTL),
3. the handler retry queue (Redis, no TTL — a retry sits until it is drained),
4. the dead-letter table (`webhook_dead_letters.event`, JSONB, durable).

Sinks 2, 3 and 4 serialize the whole event with `model_dump`, so the only place
that can be fixed once is the model. `CardEvent.otp_code` is declared
`Field(exclude=True)`: it exists in memory, from `parse_webhook` to the handler,
and no serializer anywhere can emit it. These tests are what makes that
structural rather than a convention every future sink has to remember.

The consequence is deliberate and is asserted below: an event read back off the
bus, or out of a retry, has `otp_code is None`. A code that has been through a
retry delay is expired anyway, and re-showing a dead code is worse than showing
nothing — see `app/otp/service.py` for what the handler does instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.issuers import registry
from app.issuers.base import CardEvent, CardEventType
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.receiver import receive
from app.webhooks.retry import RetryItem, dead_letter
from tests.support import all_ledger_events, dead_letters, make_mock_card

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
#: Distinctive enough that `in json.dumps(...)` cannot pass by coincidence.
CODE = "918273"


def challenge_event(**overrides: object) -> CardEvent:
    fields: dict[str, object] = {
        "provider_id": "gnosis_pay_mock",
        "event_id": "evt-challenge-1",
        "event_type": CardEventType.THREE_DS_CHALLENGE,
        "occurred_at": NOW,
        "card_id": "card_1",
        "challenge_id": "3ds_000001",
        "challenge_expires_at": NOW + timedelta(minutes=5),
        "otp_code": CODE,
    }
    fields.update(overrides)
    return CardEvent(**fields)  # type: ignore[arg-type]


def test_the_code_is_readable_in_memory() -> None:
    # Excluding it from serialization must not make it unusable: the handler that
    # stores it in Redis reads exactly this attribute.
    assert CODE == challenge_event().otp_code


def test_the_code_is_absent_from_every_serialization() -> None:
    event = challenge_event()
    assert "otp_code" not in event.model_dump()
    assert "otp_code" not in event.model_dump(mode="json")
    assert CODE not in event.model_dump_json()


def test_the_expiry_is_not_a_secret_and_does_serialize() -> None:
    # The counterpart assertion. Only the code is withheld; when the challenge
    # dies is exactly what a consumer needs to know, and nothing is lost by
    # keeping it.
    dumped = challenge_event().model_dump(mode="json")
    assert "challenge_expires_at" in dumped
    assert dumped["challenge_expires_at"].startswith("2026-07-25T12:05:00")


def test_an_event_round_trips_without_its_code() -> None:
    # `extra="forbid"` on the model means a dump that omits the field must still
    # validate, or every re-read of a serialized event would raise.
    restored = CardEvent.model_validate(challenge_event().model_dump(mode="json"))
    assert restored.otp_code is None
    assert restored.challenge_id == "3ds_000001"


def test_a_queued_retry_does_not_carry_the_code() -> None:
    item = RetryItem(
        provider_id="gnosis_pay_mock",
        handler="otp.deliver",
        attempts=1,
        last_error="RuntimeError: redis unavailable",
        event=challenge_event(),
    )
    serialized = item.to_json()
    assert CODE not in serialized
    assert RetryItem.from_json(serialized).event.otp_code is None


async def test_the_bus_does_not_carry_the_code(redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(redis_client)
    await bus.publish(challenge_event())

    stored = await redis_client.xrange(bus.stream, min="-", max="+")
    assert CODE not in json.dumps(stored)

    published = await bus.read()
    assert 1 == len(published)
    assert published[0].event.otp_code is None
    assert published[0].event.challenge_id == "3ds_000001"


async def test_a_dead_letter_row_does_not_carry_the_code(session: AsyncSession) -> None:
    await dead_letter(
        session,
        RetryItem(
            provider_id="gnosis_pay_mock",
            handler="otp.deliver",
            attempts=6,
            last_error="RuntimeError: redis unavailable",
            event=challenge_event(),
        ),
        reason="gave up",
    )

    rows = await dead_letters(session)
    assert 1 == len(rows)
    # The whole row, not just the event column: a code anywhere in a durable
    # table is the thing being ruled out.
    assert CODE not in json.dumps(rows[0].event)
    assert rows[0].event["challenge_id"] == "3ds_000001"


# ------------------------------------------------- and through the pipeline ----


@pytest.fixture
def mock_provider() -> GnosisPayMockAdapter:
    adapter = registry.get_adapter("gnosis_pay_mock")
    assert isinstance(adapter, GnosisPayMockAdapter)
    return adapter


async def test_a_real_challenge_delivery_leaves_no_code_in_the_ledger(
    session: AsyncSession, redis_client: Redis, mock_provider: GnosisPayMockAdapter
) -> None:
    """The assertion that matters, made end to end rather than per component.

    The unit tests above each hold one sink. This one drives the whole receiver —
    verify, dedup, parse, ledger, dispatch — with a genuine signed delivery whose
    body really does contain a code, and then reads the durable record back.
    """
    card_id = await make_mock_card(mock_provider)
    delivery = mock_provider.simulator.emit_three_ds_challenge(card_id, code=CODE)
    assert CODE in delivery.body.decode(), "the delivery must really carry a code"

    outcome = await receive(
        session,
        redis_client,
        provider_id="gnosis_pay_mock",
        headers=delivery.headers,
        body=delivery.body,
    )
    assert CardEventType.THREE_DS_CHALLENGE is outcome.event_type

    entries = await all_ledger_events(session)
    assert 1 == len(entries)
    row = entries[0]
    assert CODE not in json.dumps(row.payload)
    # The auditable facts survive: which challenge, and that a code was in it.
    assert row.payload["challenge_id"] is not None
    assert "[redacted]" == row.payload["raw"]["data"]["otpCode"]
