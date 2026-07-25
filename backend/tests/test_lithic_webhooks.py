"""Lithic deliveries, normalized (SPEC.md §3.3, §4).

The bodies here are the `payload` objects out of a recorded `GET /v1/events` — which
*is* what a webhook delivers. Lithic sends the payload alone; the identity and the
delivery time are in headers, and there is no envelope in the body at all (their own
verification example signs `{"acquirer_fee":0,"amount":2000,...}`). That is the reason
`parse_webhook` takes headers, and it is asserted here rather than assumed.

Three mapping decisions carry weight:

**A transaction event is keyed on its newest entry in `events[]`.** Lithic re-sends
the whole transaction on every change, so `card_transaction.updated` is an
authorization the first time and a settlement the second. Keying on the transaction's
`status` instead would call a voided authorization "voided" and lose the reversal.

**`amount` is a magnitude; the event type carries the direction.** Lithic signs its
event amounts (a reversal is `-500`, a refund `-250`); `CardEventType` already
distinguishes `refund` from `settlement`, so a sign would only invite double
negation. `effective_polarity` stays in `raw`.

**Nothing is ever dropped.** An event type we do not model becomes `unmapped` with a
compound `provider_event_type` (`card_transaction.updated:BALANCE_INQUIRY`) so the
ledger says what actually arrived (SPEC.md §3.3).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.money import Money
from app.issuers.base import CardEventType, CardState, WebhookParseError
from app.issuers.lithic import LithicAdapter
from app.issuers.lithic.client import LithicClient
from app.issuers.lithic.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WEBHOOK_ID_HEADER,
    sign,
)

BASE_URL = "https://sandbox.lithic.test/v1"
API_KEY = "test-sandbox-key-not-a-real-credential"
WEBHOOK_SECRET = "whsec_cGhhc2UtMy10ZXN0LWtleS1tYXRlcmlhbC0zMmJ5dGU="

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
EVENT_ID = "msg_3Gz2jl6jYfdoFh0uMVTiOBE36hL"

FIXTURES = Path(__file__).parent / "fixtures" / "lithic"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def recorded_payloads() -> list[dict[str, Any]]:
    """Every recorded event payload — i.e. every recorded webhook body."""
    return [event["payload"] for event in fixture("events_all")["data"]]


def transaction_payload(*, status: str, event_types: list[str], amount: int) -> dict[str, Any]:
    """One recorded `card_transaction.updated` body, chosen by what it reports.

    `amount` is the newest event's own signed amount, which is what identifies a
    delivery here: the recording holds several authorizations, and the sign is the
    detail the mapping has to get right.
    """
    for payload in recorded_payloads():
        if payload.get("event_type") != "card_transaction.updated":
            continue
        events = payload["events"]
        if payload["status"] != status or [e["type"] for e in events] != event_types:
            continue
        if events[-1]["amount"] == amount:
            return payload
    raise AssertionError(f"no recorded transaction: {status} {event_types} {amount}")


def lifecycle_payload() -> dict[str, Any]:
    for payload in recorded_payloads():
        if payload.get("event_type") == "card.created":
            return payload
    raise AssertionError("no recorded card.created")


def holder_payload() -> dict[str, Any]:
    for payload in recorded_payloads():
        if payload.get("event_type") == "account_holder.created":
            return payload
    raise AssertionError("no recorded account_holder.created")


@pytest.fixture
def adapter() -> LithicAdapter:
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0)
    return LithicAdapter(client=client, webhook_secret=WEBHOOK_SECRET, clock=lambda: NOW)


@pytest.fixture
def unsecured() -> LithicAdapter:
    """An adapter on a program with no event subscription yet."""
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0)
    return LithicAdapter(client=client, webhook_secret="", clock=lambda: NOW)


def body_of(payload: Any) -> bytes:
    # Compact, and then never touched again: the signature covers these bytes.
    return json.dumps(payload, separators=(",", ":")).encode()


def headers_for(body: bytes, *, event_id: str = EVENT_ID, now: datetime = NOW) -> dict[str, str]:
    timestamp = str(int(now.timestamp()))
    return {
        WEBHOOK_ID_HEADER: event_id,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(WEBHOOK_SECRET, webhook_id=event_id, timestamp=timestamp, body=body),
    }


# ------------------------------------------------------------ verification ----


async def test_a_genuine_delivery_verifies(adapter: LithicAdapter) -> None:
    body = body_of(
        transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    )
    assert await adapter.verify_webhook(headers_for(body), body)


async def test_a_delivery_signed_with_another_key_does_not(adapter: LithicAdapter) -> None:
    body = body_of(lifecycle_payload())
    headers = headers_for(body)
    other = "whsec_YW5vdGhlci1wcm9ncmFtcy13ZWJob29rLWtleS0xMg=="
    headers[SIGNATURE_HEADER] = sign(
        other, webhook_id=EVENT_ID, timestamp=headers[TIMESTAMP_HEADER], body=body
    )

    assert not await adapter.verify_webhook(headers, body)


async def test_an_adapter_with_no_secret_configured_verifies_nothing(
    unsecured: LithicAdapter,
) -> None:
    # Fails closed. An unconfigured program must reject deliveries, not accept them —
    # and must not raise either, or the endpoint answers 500 where it means 401.
    body = body_of(lifecycle_payload())
    assert not await unsecured.verify_webhook(headers_for(body), body)


async def test_the_refusal_says_which_variable_to_set(
    unsecured: LithicAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    # `verify_webhook` returns a bare False, so the log line is the *only* thing that
    # tells an operator the difference between "this delivery was forged" and "you never
    # set the secret". It has existed since phase 3 and had never been observed by a
    # test: `alembic/env.py` ran `fileConfig` with `disable_existing_loggers=True`, so
    # migrations switched off every app logger for the whole session and `caplog` came
    # back empty (docs/ARCHITECTURE.md §8.9). Now that logging survives, this is the
    # test that phase 3 should have had.
    body = body_of(lifecycle_payload())

    with caplog.at_level(logging.WARNING):
        assert not await unsecured.verify_webhook(headers_for(body), body)

    assert "LITHIC_WEBHOOK_SECRET" in caplog.text


async def test_the_dedup_id_is_the_webhook_id_header(adapter: LithicAdapter) -> None:
    # It is inside the signed content, so it cannot be rewritten to make a replay
    # look like a new event (SPEC.md §4).
    body = body_of(lifecycle_payload())
    assert EVENT_ID == adapter.webhook_event_id(headers_for(body), body)


async def test_no_dedup_id_without_the_header(adapter: LithicAdapter) -> None:
    # The receiver then falls back to a digest of the body; verification would have
    # rejected such a delivery first.
    assert adapter.webhook_event_id({}, b"{}") is None


# --------------------------------------------------- transaction lifecycle ----


async def test_an_authorization(adapter: LithicAdapter) -> None:
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.AUTHORIZATION is event.event_type
    assert "lithic" == event.provider_id
    assert EVENT_ID == event.event_id
    assert payload["card_token"] == event.card_id
    assert payload["account_token"] == event.cardholder_id
    assert Money(1_234, "USD") == event.amount
    assert "card_transaction.updated:AUTHORIZATION" == event.provider_event_type
    # The moment the provider says the event happened, not when we read it.
    assert datetime.fromisoformat(payload["events"][0]["created"]) == event.occurred_at
    assert event.occurred_at.tzinfo is UTC
    # Nothing is lost by normalizing (SPEC.md §7).
    assert payload == event.raw


async def test_a_settlement_is_keyed_on_the_clearing_not_the_authorization(
    adapter: LithicAdapter,
) -> None:
    # The same transaction, re-sent after it cleared. Keying on the first event would
    # ledger a second authorization and never a settlement.
    payload = transaction_payload(
        status="SETTLED", event_types=["AUTHORIZATION", "CLEARING"], amount=1_234
    )
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.SETTLEMENT is event.event_type
    assert "card_transaction.updated:CLEARING" == event.provider_event_type
    assert Money(1_234, "USD") == event.amount


async def test_a_reversal_reports_a_positive_amount(adapter: LithicAdapter) -> None:
    # Lithic sends -500 with `effective_polarity: CREDIT`. The type says which way
    # the money went, so the amount is a magnitude.
    payload = transaction_payload(
        status="VOIDED", event_types=["AUTHORIZATION", "AUTHORIZATION_REVERSAL"], amount=-500
    )
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.AUTHORIZATION_REVERSAL is event.event_type
    assert Money(500, "USD") == event.amount
    assert -500 == event.raw["events"][-1]["amount"], "the provider's sign is kept in raw"


async def test_a_refund(adapter: LithicAdapter) -> None:
    payload = transaction_payload(status="SETTLED", event_types=["RETURN"], amount=-250)
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.REFUND is event.event_type
    assert Money(250, "USD") == event.amount


async def test_a_decline_is_still_an_authorization_and_keeps_its_reason(
    adapter: LithicAdapter,
) -> None:
    # There is no normalized field for "declined" (SPEC.md §3.3), and inventing one
    # for a provider detail is what `raw` exists to avoid.
    payload = transaction_payload(
        status="DECLINED", event_types=["AUTHORIZATION"], amount=10_000_000
    )
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.AUTHORIZATION is event.event_type
    assert "USER_TRANSACTION_LIMIT" == event.raw["result"]


async def test_the_newest_event_wins_even_if_the_array_is_out_of_order(
    adapter: LithicAdapter,
) -> None:
    # Recorded arrays are chronological. Nothing documents that they must be.
    payload = transaction_payload(
        status="SETTLED", event_types=["AUTHORIZATION", "CLEARING"], amount=1_234
    )
    shuffled = {**payload, "events": list(reversed(payload["events"]))}
    body = body_of(shuffled)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.SETTLEMENT is event.event_type


async def test_a_transaction_event_we_do_not_model_is_unmapped_with_its_own_label(
    adapter: LithicAdapter,
) -> None:
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    inquiry = {
        **payload,
        "events": [{**payload["events"][0], "type": "BALANCE_INQUIRY", "amount": 0}],
    }
    body = body_of(inquiry)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.UNMAPPED is event.event_type
    assert "card_transaction.updated:BALANCE_INQUIRY" == event.provider_event_type
    assert event.card_id == payload["card_token"], "still attributed to its card"


async def test_a_transaction_with_no_events_falls_back_to_the_transaction_itself(
    adapter: LithicAdapter,
) -> None:
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    empty = {**payload, "events": []}
    body = body_of(empty)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.UNMAPPED is event.event_type
    assert "card_transaction.updated" == event.provider_event_type
    assert event.amount is None, "there is no event to take an amount from"
    assert datetime.fromisoformat(payload["created"]) == event.occurred_at


async def test_an_event_amount_that_is_not_an_integer_is_refused(
    adapter: LithicAdapter,
) -> None:
    # A decimal amount from a provider is a reconciliation incident waiting to happen.
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    decimal = {**payload, "events": [{**payload["events"][0], "amount": 12.34}]}
    body = body_of(decimal)

    with pytest.raises(WebhookParseError, match="minor units"):
        await adapter.parse_webhook(headers_for(body), body)


# -------------------------------------------------------- card lifecycle ----


async def test_a_card_created_event_has_no_timestamp_of_its_own(
    adapter: LithicAdapter,
) -> None:
    # `{card_token, event_type, replacement_for}` — that is the whole payload. The
    # only time available is the `webhook-timestamp` header, which is why
    # `parse_webhook` takes headers at all (docs/ARCHITECTURE.md §4.1).
    payload = lifecycle_payload()
    assert not {"created", "updated"} & set(payload), "the recording must show this"
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body, now=NOW), body)

    assert CardEventType.CARD_LIFECYCLE is event.event_type
    assert payload["card_token"] == event.card_id
    assert NOW == event.occurred_at
    # Sandbox `card.created` says nothing about state, and guessing would be a claim
    # about whether the card can spend.
    assert event.card_state is None


async def test_a_card_updated_event_carries_the_new_state(adapter: LithicAdapter) -> None:
    # Unavailable in Sandbox, so this is the published shape:
    # `{previous_fields, state, card_token, event_type}`.
    payload = {
        "event_type": "card.updated",
        "card_token": lifecycle_payload()["card_token"],
        "state": "PAUSED",
        "previous_fields": {"state": "OPEN"},
    }
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.CARD_LIFECYCLE is event.event_type
    assert CardState.FROZEN is event.card_state


async def test_a_card_state_we_do_not_model_leaves_the_state_unset(
    adapter: LithicAdapter,
) -> None:
    payload = {
        "event_type": "card.updated",
        "card_token": lifecycle_payload()["card_token"],
        "state": "PENDING_REVIEW",
    }
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    # Unlike `get_card`, a delivery is not refused over this: the event is real and
    # the raw payload keeps whatever they said.
    assert CardEventType.CARD_LIFECYCLE is event.event_type
    assert event.card_state is None
    assert "PENDING_REVIEW" == event.raw["state"]


# ------------------------------------------------------- other event types ----


async def test_a_cardholder_event_is_unmapped_but_attributed(
    adapter: LithicAdapter,
) -> None:
    # `CardEventType` has no cardholder vocabulary (SPEC.md §3.3), so this is
    # `unmapped` — recorded, attributed to its account, and not dropped.
    payload = holder_payload()
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.UNMAPPED is event.event_type
    assert "account_holder.created" == event.provider_event_type
    assert payload["account_token"] == event.cardholder_id
    assert event.card_id is None


async def test_an_event_type_lithic_has_not_invented_yet_is_unmapped(
    adapter: LithicAdapter,
) -> None:
    body = body_of({"event_type": "card.quantum_entangled", "card_token": "c0ffee"})

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.UNMAPPED is event.event_type
    assert "card.quantum_entangled" == event.provider_event_type
    assert "c0ffee" == event.card_id


async def test_a_three_ds_challenge(adapter: LithicAdapter) -> None:
    payload = json.loads((FIXTURES / "from_schema" / "event_three_ds_challenge.json").read_text())
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.THREE_DS_CHALLENGE is event.event_type
    # Phase 7's OTP service keys on this.
    assert payload["authentication_object"]["token"] == event.challenge_id
    assert payload["authentication_object"]["card_token"] == event.card_id
    assert datetime(2026, 7, 25, 12, 34, 56, tzinfo=UTC) == event.occurred_at
    # Lithic states this amount as a decimal plus a `currency_exponent`. Money here is
    # integer minor units only, so it is left unnormalized rather than converted on a
    # payload we have never seen live; the provider's own numbers stay in `raw`.
    assert event.amount is None
    assert 12.34 == event.raw["authentication_object"]["transaction"]["amount"]


async def test_a_three_ds_challenge_we_cannot_read_is_unmapped_not_dropped(
    adapter: LithicAdapter,
) -> None:
    body = body_of({"event_type": "three_ds_authentication.challenge", "challenge": {}})

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.UNMAPPED is event.event_type
    assert "three_ds_authentication.challenge" == event.provider_event_type


async def test_a_dispute_with_no_readable_amount_is_still_a_chargeback(
    adapter: LithicAdapter,
) -> None:
    payload = json.loads((FIXTURES / "from_schema" / "event_dispute_updated.json").read_text())
    body = body_of({**payload, "amount": "1234"})

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.CHARGEBACK is event.event_type
    assert event.amount is None, "a string amount is not silently coerced"


async def test_an_event_with_no_amount_at_all_has_none(adapter: LithicAdapter) -> None:
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    without = {
        **payload,
        "events": [{k: v for k, v in payload["events"][0].items() if k != "amount"}],
    }
    body = body_of(without)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert event.amount is None, "absent is not zero"


@pytest.mark.parametrize("amounts", [{}, None, {"cardholder": None}, {"cardholder": {}}])
async def test_an_event_with_no_currency_anywhere_falls_back_to_usd(
    adapter: LithicAdapter, amounts: object
) -> None:
    # Every recorded event carries one; this is the program's own currency, and USD is
    # the only one this sandbox program issues in.
    payload = transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    stripped = {**payload, "events": [{**payload["events"][0], "amounts": amounts}]}
    body = body_of(stripped)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert Money(1_234, "USD") == event.amount


async def test_a_delivery_with_an_unreadable_timestamp_header_uses_our_clock(
    adapter: LithicAdapter,
) -> None:
    payload = lifecycle_payload()
    body = body_of(payload)
    headers = headers_for(body)
    headers[TIMESTAMP_HEADER] = "the day before yesterday"

    event = await adapter.parse_webhook(headers, body)

    assert NOW == event.occurred_at


async def test_a_delivery_with_no_timestamp_header_uses_our_clock(
    adapter: LithicAdapter,
) -> None:
    payload = lifecycle_payload()
    body = body_of(payload)
    headers = headers_for(body)
    del headers[TIMESTAMP_HEADER]

    event = await adapter.parse_webhook(headers, body)

    assert NOW == event.occurred_at


async def test_a_dispute_is_a_chargeback_without_a_card(adapter: LithicAdapter) -> None:
    payload = json.loads((FIXTURES / "from_schema" / "event_dispute_updated.json").read_text())
    body = body_of(payload)

    event = await adapter.parse_webhook(headers_for(body), body)

    assert CardEventType.CHARGEBACK is event.event_type
    assert Money(1_234, "USD") == event.amount
    # The payload names a transaction, not a card, and resolving it would need an API
    # call — `parse_webhook` is pure, and the receiver calls it again for duplicates.
    assert event.card_id is None
    assert payload["transaction_token"] == event.raw["transaction_token"]


# ------------------------------------------------------- unreadable bodies ----


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[1, 2, 3]", b'"a string"', b"\xff\xfe", b"null"],
    ids=["empty", "text", "array", "string", "not-utf8", "null"],
)
async def test_a_body_that_is_not_a_json_object_is_a_parse_error(
    adapter: LithicAdapter, body: bytes
) -> None:
    # The receiver ledgers these as `unmapped` with the bytes attached rather than
    # retrying: the signature already proved the delivery is genuine, so re-sending
    # it changes nothing (SPEC.md §4).
    with pytest.raises(WebhookParseError):
        await adapter.parse_webhook(headers_for(body), body)


async def test_a_body_with_no_event_type_is_a_parse_error(adapter: LithicAdapter) -> None:
    body = body_of({"card_token": "c0ffee"})
    with pytest.raises(WebhookParseError, match="event_type"):
        await adapter.parse_webhook(headers_for(body), body)


async def test_a_delivery_with_no_id_header_is_a_parse_error(adapter: LithicAdapter) -> None:
    # A verified delivery always has one — the id is part of the signed content — so
    # this means something upstream stripped it.
    body = body_of(lifecycle_payload())
    headers = headers_for(body)
    del headers[WEBHOOK_ID_HEADER]

    with pytest.raises(WebhookParseError, match=WEBHOOK_ID_HEADER):
        await adapter.parse_webhook(headers, body)


async def test_the_parsed_event_id_is_the_key_the_receiver_deduped_on(
    adapter: LithicAdapter,
) -> None:
    # If these two ever disagreed, the ledger would record one id and the dedup gate
    # would hold another — and the second delivery would be recorded again.
    body = body_of(
        transaction_payload(status="PENDING", event_types=["AUTHORIZATION"], amount=1_234)
    )
    headers = headers_for(body)

    event = await adapter.parse_webhook(headers, body)

    assert adapter.webhook_event_id(headers, body) == event.event_id
