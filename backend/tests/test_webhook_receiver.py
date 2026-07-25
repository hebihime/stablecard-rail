"""The webhook receiver pipeline (SPEC.md §4, §10).

    raw body -> verify -> dedup -> parse -> ledger -> dispatch

Every step is a place where money can be double-counted or lost, so these tests
are written against invariants rather than implementation:

* an unverified delivery leaves **no trace at all** — not even a ledger row;
* a verified delivery is recorded **exactly once**, whatever the provider does;
* an authentic delivery is **never dropped**, even when we cannot read it;
* nothing a handler does changes the answer given back to the provider.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.money import Money
from app.issuers import registry
from app.issuers.base import CardEvent, CardEventType, CardState
from app.issuers.evm_deposit_mock import Delivery, EvmDepositMockAdapter
from app.issuers.evm_deposit_mock.signing import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from app.webhooks import dispatch
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.dedup import dedup_key, ledger_idempotency_key
from app.webhooks.receiver import DeliveryOutcome, SignatureRejected, receive
from tests.support import StubIssuerAdapter, all_ledger_events, make_mock_card

PROVIDER = "evm_deposit_mock"


class Clock:
    """A clock the test moves by hand, shared by the signer and the verifier."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 7, 25, 12, 0, tzinfo=UTC))


@pytest.fixture
def adapter(clock: Clock) -> Iterator[EvmDepositMockAdapter]:
    """The mock provider on a controlled clock, registered under its real id."""
    built = EvmDepositMockAdapter(
        webhook_secret=get_settings().evm_deposit_mock_webhook_secret, clock=clock
    )
    registry.register(PROVIDER, lambda: built, replace=True)
    yield built


@pytest.fixture
def stub_provider() -> Iterator[StubIssuerAdapter]:
    built = StubIssuerAdapter()
    registry.register(built.provider_id, lambda: built, replace=True)
    yield built


async def deliver(
    session: AsyncSession, redis: Redis, delivery: Delivery, *, provider_id: str = PROVIDER
) -> DeliveryOutcome:
    return await receive(
        session,
        redis,
        provider_id=provider_id,
        headers=delivery.headers,
        body=delivery.body,
    )


# -------------------------------------------------------------- happy path ----


async def test_a_verified_delivery_is_recorded_with_its_provider_payload(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"), merchant="Coffee")

    outcome = await deliver(session, redis_client, delivery)

    assert outcome.duplicate is False
    assert outcome.event_type is CardEventType.AUTHORIZATION
    assert outcome.event_id == delivery.event_id

    events = await all_ledger_events(session)
    assert len(events) == 1
    recorded = events[0]
    assert recorded.id == outcome.ledger_event_id
    assert recorded.event_type == "provider.authorization"
    assert recorded.provider_id == PROVIDER
    assert recorded.card_id == card_id
    assert recorded.amount_minor == 1299
    assert recorded.currency == "USD"
    assert recorded.idempotency_key == ledger_idempotency_key(PROVIDER, delivery.event_id)
    # The raw payload is kept verbatim (SPEC.md §7) — normalizing loses nothing.
    assert recorded.payload["raw"] == json.loads(delivery.body)
    assert recorded.payload["provider_event_type"] == "card.authorization"


async def test_the_provider_timestamp_is_preserved_not_the_arrival_time(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter, clock: Clock
) -> None:
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    await deliver(session, redis_client, delivery)

    recorded = (await all_ledger_events(session))[0]
    # The provider said 12:00; the wall clock here says something else entirely.
    assert recorded.occurred_at == clock.now
    assert recorded.recorded_at != recorded.occurred_at, "recorded_at is ours, not theirs"


async def test_a_lifecycle_event_records_the_new_card_state(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    card_id = await make_mock_card(adapter)
    await adapter.freeze_card(card_id)
    delivery = adapter.simulator.emit_card_lifecycle(card_id)

    outcome = await deliver(session, redis_client, delivery)

    assert outcome.event_type is CardEventType.CARD_LIFECYCLE
    recorded = (await all_ledger_events(session))[0]
    assert recorded.event_type == "provider.card_lifecycle"
    assert recorded.state_after == CardState.FROZEN.value


async def test_the_event_is_published_on_the_bus(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    outcome = await deliver(session, redis_client, delivery)

    published = await RedisStreamsEventBus(redis_client).read()
    assert [entry.event.event_id for entry in published] == [delivery.event_id]
    assert published[0].stream_id == outcome.stream_id


# ------------------------------------------------------------ verification ----


async def test_an_unsigned_delivery_is_rejected_and_leaves_no_trace(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    # No ledger row for unauthenticated traffic: anyone can POST here, and an
    # attacker must not be able to write to the audit log or fill its keyspace.
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    with pytest.raises(SignatureRejected):
        await receive(session, redis_client, provider_id=PROVIDER, headers={}, body=delivery.body)

    assert await all_ledger_events(session) == []
    assert await redis_client.keys("*") == []


async def test_a_tampered_body_is_rejected(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    with pytest.raises(SignatureRejected):
        await receive(
            session,
            redis_client,
            provider_id=PROVIDER,
            headers=delivery.headers,
            body=delivery.body.replace(b"1299", b"999999"),
        )
    assert await all_ledger_events(session) == []


async def test_a_captured_delivery_stops_verifying_once_it_goes_stale(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter, clock: Clock
) -> None:
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    clock.now += timedelta(hours=1)

    with pytest.raises(SignatureRejected):
        await deliver(session, redis_client, delivery)


async def test_an_unknown_provider_is_a_lookup_error_not_a_crash(
    session: AsyncSession, redis_client: Redis
) -> None:
    with pytest.raises(registry.UnknownProviderError):
        await receive(session, redis_client, provider_id="wells_fargo", headers={}, body=b"{}")
    assert await all_ledger_events(session) == []


# ------------------------------------------------------------------- dedup ----


async def test_a_duplicate_delivery_is_a_no_op(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    """SPEC.md §4: duplicate deliveries return 200 with no side effects."""
    seen: list[str] = []

    async def recorder(event: CardEvent) -> None:
        seen.append(event.event_id)

    dispatch.subscribe(CardEventType.AUTHORIZATION, "recorder", recorder)

    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    first = await deliver(session, redis_client, delivery)
    second = await deliver(session, redis_client, delivery)

    assert first.duplicate is False
    assert second.duplicate is True
    assert len(await all_ledger_events(session)) == 1
    assert seen == [delivery.event_id], "a duplicate must not re-run handlers"
    assert await redis_client.xlen("stablecard:card_events") == 1


async def test_a_duplicate_points_back_at_the_row_it_matched(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    # The provider gets a 200 either way, but our operators need to tell
    # "already had it, here it is" from "never seen it".
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    first = await deliver(session, redis_client, delivery)

    second = await deliver(session, redis_client, delivery)
    assert second.event_id == delivery.event_id
    assert second.event_type is CardEventType.AUTHORIZATION
    assert second.ledger_event_id == first.ledger_event_id
    assert second.stream_id is None, "a duplicate is not republished"


async def test_dedup_survives_redis_losing_the_key(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    """The durable layer is the ledger's unique index, not Redis (SPEC.md §4).

    Eviction, TTL expiry or a Redis restart must not turn a redelivery into a
    second funding. Flushing the database is what all three look like from here.
    """
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    await deliver(session, redis_client, delivery)

    await redis_client.flushdb()

    second = await deliver(session, redis_client, delivery)
    assert second.duplicate is True
    assert len(await all_ledger_events(session)) == 1


async def test_a_failure_before_the_ledger_write_gives_the_dedup_claim_back(
    session: AsyncSession,
    redis_client: Redis,
    adapter: EvmDepositMockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SETNX-first opens a crash window; releasing the claim closes it.

    Claim, then die before recording: without the release, the provider's
    redelivery would be answered "duplicate" and the event would be lost for the
    life of the TTL — a day, by default.
    """
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr("app.webhooks.receiver.record", explode)
    with pytest.raises(RuntimeError):
        await deliver(session, redis_client, delivery)

    assert await redis_client.exists(dedup_key(PROVIDER, delivery.event_id)) == 0
    assert await all_ledger_events(session) == []

    monkeypatch.undo()
    retried = await deliver(session, redis_client, delivery)
    assert retried.duplicate is False
    assert len(await all_ledger_events(session)) == 1


async def test_an_unrelated_integrity_error_is_not_mistaken_for_a_duplicate(
    session: AsyncSession,
    redis_client: Redis,
    adapter: EvmDepositMockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The duplicate path is entered by matching the constraint *name*. Treating
    # every IntegrityError as a duplicate would silently swallow real schema
    # violations and answer 200 to an event we never recorded.
    card_id = await make_mock_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT ...", {}, Exception('violates check constraint "ck_other"'))

    monkeypatch.setattr("app.webhooks.receiver.record", explode)

    with pytest.raises(IntegrityError):
        await deliver(session, redis_client, delivery)

    assert await all_ledger_events(session) == []
    assert await redis_client.exists(dedup_key(PROVIDER, delivery.event_id)) == 0


async def test_an_adapter_without_envelope_ids_dedups_on_the_body(
    session: AsyncSession, redis_client: Redis, stub_provider: StubIssuerAdapter
) -> None:
    # `webhook_event_id` is optional; the fallback must still be a working dedup
    # key, or a provider with no envelope id would be processed twice.
    body = b'{"provider":"stub","n":1}'
    first = await receive(
        session, redis_client, provider_id=stub_provider.provider_id, headers={}, body=body
    )
    second = await receive(
        session, redis_client, provider_id=stub_provider.provider_id, headers={}, body=body
    )

    assert first.event_id == hashlib.sha256(body).hexdigest()
    assert second.duplicate is True
    assert len(await all_ledger_events(session)) == 1


async def test_a_different_body_from_such_an_adapter_is_a_different_event(
    session: AsyncSession, redis_client: Redis, stub_provider: StubIssuerAdapter
) -> None:
    provider = stub_provider.provider_id
    await receive(session, redis_client, provider_id=provider, headers={}, body=b'{"n":1}')
    await receive(session, redis_client, provider_id=provider, headers={}, body=b'{"n":2}')
    assert len(await all_ledger_events(session)) == 2


# --------------------------------------------------------- unmapped events ----


async def test_an_unknown_provider_event_is_ledgered_as_unmapped(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter
) -> None:
    """SPEC.md §3.3: unknown provider events are never dropped silently."""
    delivery = adapter.simulator.emit_unknown(
        "card.quantum_entangled", {"card_id": "card_000001", "surprise": True}
    )

    outcome = await deliver(session, redis_client, delivery)

    assert outcome.event_type is CardEventType.UNMAPPED
    recorded = (await all_ledger_events(session))[0]
    assert recorded.event_type == "provider.unmapped"
    assert recorded.payload["provider_event_type"] == "card.quantum_entangled"
    assert recorded.payload["raw"]["data"]["surprise"] is True


async def test_an_unreadable_body_is_ledgered_rather_than_retried_forever(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter, clock: Clock
) -> None:
    # The signature proves the delivery is genuine, so redelivery cannot fix it.
    # Recording it as unmapped preserves the evidence *and* stops the loop.
    event_id = "evt_malformed"
    body = b"{this is not json"
    timestamp = str(int(clock.now.timestamp()))
    headers = {
        TIMESTAMP_HEADER: timestamp,
        EVENT_ID_HEADER: event_id,
        SIGNATURE_HEADER: sign(
            get_settings().evm_deposit_mock_webhook_secret,
            timestamp=timestamp,
            event_id=event_id,
            body=body,
        ),
    }

    outcome = await receive(session, redis_client, provider_id=PROVIDER, headers=headers, body=body)

    assert outcome.event_type is CardEventType.UNMAPPED
    recorded = (await all_ledger_events(session))[0]
    assert recorded.event_type == "provider.unmapped"
    assert "not JSON" in recorded.payload["parse_error"]
    assert recorded.payload["raw"]["body_base64"], "the bytes are kept, readable or not"


async def test_an_unreadable_body_is_still_deduplicated(
    session: AsyncSession, redis_client: Redis, stub_provider: StubIssuerAdapter
) -> None:
    stub_provider.parse_fails = True
    body = b"\xff\xfe not even utf-8"
    for _ in range(2):
        await receive(
            session, redis_client, provider_id=stub_provider.provider_id, headers={}, body=body
        )
    assert len(await all_ledger_events(session)) == 1


# ---------------------------------------------------------------- ordering ----


async def test_out_of_order_deliveries_keep_both_orders_readable(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter, clock: Clock
) -> None:
    """SPEC.md §10: out-of-order events.

    Providers do not guarantee order. We record arrival order in `id` and the
    provider's own order in `occurred_at`, and never reorder or reject on that
    basis — a settlement arriving before its authorization is normal.
    """
    card_id = await make_mock_card(adapter)
    earlier = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    clock.now += timedelta(seconds=30)
    later = adapter.simulator.emit_settlement(card_id, Money(1299, "USD"))

    # Delivered newest-first, as a retrying provider might.
    await deliver(session, redis_client, later)
    await deliver(session, redis_client, earlier)

    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == [
        "provider.settlement",
        "provider.authorization",
    ], "arrival order is preserved: the ledger is append-only"
    assert events[0].occurred_at > events[1].occurred_at
    assert [event.payload["raw"]["id"] for event in events] == [later.event_id, earlier.event_id]


async def test_events_for_one_card_read_back_as_a_history(
    session: AsyncSession, redis_client: Redis, adapter: EvmDepositMockAdapter, clock: Clock
) -> None:
    card_id = await make_mock_card(adapter)
    authorization = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    authorization_id = json.loads(authorization.body)["data"]["authorization_id"]
    clock.now += timedelta(seconds=1)
    reversal = adapter.simulator.emit_authorization_reversal(authorization_id)

    for delivery in (authorization, reversal):
        await deliver(session, redis_client, delivery)

    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == [
        "provider.authorization",
        "provider.authorization_reversal",
    ]
    assert {event.card_id for event in events} == {card_id}
