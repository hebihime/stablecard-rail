"""Stripe deliveries, normalized onto `CardEvent` (SPEC.md §3.3, §4, phase 4).

This is where phase 3's one interface change pays off. `parse_webhook(headers,
body)` was widened because Lithic puts the event id in a `webhook-id` header and
sends payloads with no timestamp at all (docs/ARCHITECTURE.md §4.1). Stripe does
the opposite: the id and the timestamp are both in the body, inside a proper Event
envelope. The widened signature costs Stripe nothing, and a body-only signature
would have cost Lithic everything — which is the argument for having changed it
rather than smuggling headers past the interface.

Three mapping decisions carry most of the risk, and each has a test whose name
says what would go wrong:

**A captured authorization must not settle twice.** Stripe sends
`issuing_transaction.created` *and* an `issuing_authorization.updated` moving the
authorization to `closed`. Only the first is a settlement.

**A reversal's amount is zero by the time we see it.** Stripe zeroes `amount` on a
void, so the released magnitude survives only in `data.previous_attributes`.

**`issuing_authorization.request` is not an authorization event.** It is a
two-second request for a decision, and this pipeline verifies, dedups and queues
rather than deciding inline.

`raw` diverges from Lithic's adapter, deliberately: Lithic's webhook payloads are
flat, so it keeps them untouched. Stripe's embed expanded objects — a card event
carries the whole cardholder, name and postal address included — and `raw` reaches
the ledger. So expansions are collapsed back to the ids they came from, which
keeps everything except the personal data.
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
from app.issuers.stripe_issuing import adapter as stripe_adapter
from app.issuers.stripe_issuing.adapter import PROVIDER_ID, StripeIssuingAdapter
from app.issuers.stripe_issuing.client import StripeClient
from app.issuers.stripe_issuing.signing import signature_header

SECRET = "whsec_c3RhYmxlY2FyZA=="
CARD_ID = "ic_1SbCardStablecard0001"
HOLDER_ID = "ich_1SbHolderStablecard01"
NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_issuing"


def fixture_bytes(name: str) -> bytes:
    """A delivery body exactly as it sits on disk — raw bytes, never re-serialized."""
    return (FIXTURES / f"{name}.json").read_bytes()


def fixture(name: str) -> Any:
    return json.loads(fixture_bytes(name))


async def no_sleep(seconds: float) -> None:
    return None


def make_adapter(*, secret: str = SECRET) -> StripeIssuingAdapter:
    return StripeIssuingAdapter(
        client=StripeClient(
            base_url="https://api.stripe.test/v1",
            api_key="sk_test_webhooks",
            timeout=5.0,
            sleep=no_sleep,
        ),
        webhook_secret=secret,
        clock=lambda: NOW,
    )


@pytest.fixture
def adapter() -> StripeIssuingAdapter:
    return make_adapter()


def signed(body: bytes, *, at: datetime = NOW, secret: str = SECRET) -> dict[str, str]:
    timestamp = str(int(at.timestamp()))
    return {"stripe-signature": signature_header(secret, timestamp=timestamp, body=body)}


# ------------------------------------------------------------------ verify ----


async def test_a_genuine_delivery_is_authenticated(adapter: StripeIssuingAdapter) -> None:
    body = fixture_bytes("event_transaction_capture")

    assert await adapter.verify_webhook(signed(body), body)


async def test_a_tampered_body_is_refused(adapter: StripeIssuingAdapter) -> None:
    body = fixture_bytes("event_transaction_capture")
    headers = signed(body)

    assert not await adapter.verify_webhook(headers, body.replace(b"-1234", b"-9999"))


async def test_an_unconfigured_endpoint_fails_closed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An account with no endpoint secret yet has nothing to verify against, and
    # accepting unverifiable deliveries would let anyone write to the ledger. The
    # warning names the variable, because that is the actual fix.
    #
    # The logger has to be switched back on first: `alembic/env.py` calls
    # `fileConfig` with its default `disable_existing_loggers=True`, so running
    # migrations — which the session-scoped `migrated_database` fixture always does
    # — disables every logger the app created at import time, both adapters'
    # included. Fixing that is a one-line change in `alembic/env.py` and therefore
    # not this phase's to make; it is reported instead.
    monkeypatch.setattr(stripe_adapter.logger, "disabled", False)
    body = fixture_bytes("event_transaction_capture")
    unconfigured = make_adapter(secret="")

    with caplog.at_level(logging.WARNING):
        assert not await unconfigured.verify_webhook(signed(body), body)

    assert "STRIPE_ISSUING_WEBHOOK_SECRET" in caplog.text


async def test_a_stale_delivery_is_refused(adapter: StripeIssuingAdapter) -> None:
    body = fixture_bytes("event_transaction_capture")
    headers = signed(body, at=NOW.replace(hour=12))

    assert not await adapter.verify_webhook(headers, body)


# ---------------------------------------------------------------- event id ----


async def test_the_dedup_id_is_the_event_id_from_the_body(
    adapter: StripeIssuingAdapter,
) -> None:
    # Lithic's arrives in a header; Stripe's is in the body. Both are inside the
    # signed content, which is what stops a captured delivery being relabelled as a
    # new event and processed twice (SPEC.md §4).
    body = fixture_bytes("event_transaction_capture")

    assert adapter.webhook_event_id(signed(body), body) == "evt_1SbEvTxnCapture001"


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"object":"event"}', b'{"id":"","object":"event"}'],
    ids=["empty", "not-json", "not-an-object", "no-id", "blank-id"],
)
async def test_an_unusable_body_yields_no_dedup_id(
    adapter: StripeIssuingAdapter, body: bytes
) -> None:
    # The receiver falls back to a digest of the body rather than failing, so this
    # must answer `None` rather than raise (SPEC.md §4).
    assert adapter.webhook_event_id({}, body) is None


# ------------------------------------------------------------------- parse ----


async def test_an_authorization_is_normalized_with_its_held_amount(
    adapter: StripeIssuingAdapter,
) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_authorization_created"))

    assert event.provider_id == PROVIDER_ID
    assert event.event_id == "evt_1SbEvAuthCreated001"
    assert event.event_type is CardEventType.AUTHORIZATION
    assert event.provider_event_type == "issuing_authorization.created"
    assert event.card_id == CARD_ID
    assert event.cardholder_id == HOLDER_ID
    assert event.amount == Money(3000, "USD")
    # The Event's own timestamp: for an update, the object's `created` is when the
    # object was made rather than when it changed.
    assert event.occurred_at == datetime(2026, 7, 25, 18, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "name", ["event_authorization_reversed", "event_authorization_expired"], ids=["void", "lapse"]
)
async def test_a_reversal_reports_the_amount_that_was_released(
    adapter: StripeIssuingAdapter, name: str
) -> None:
    # Stripe zeroes `amount` on a void, so `previous_attributes` is the only place
    # the released magnitude survives. An event that said 0 would be useless to a
    # ledger, and `expired` and `reversed` differ only by API version.
    event = await adapter.parse_webhook({}, fixture_bytes(name))

    assert event.event_type is CardEventType.AUTHORIZATION_REVERSAL
    assert event.amount == Money(3000, "USD")


async def test_a_reversal_without_previous_attributes_falls_back_honestly(
    adapter: StripeIssuingAdapter,
) -> None:
    # No invented number: if the old amount is not in the envelope, report what the
    # object says even when that is zero.
    payload = fixture("event_authorization_reversed")
    del payload["data"]["previous_attributes"]

    event = await adapter.parse_webhook({}, json.dumps(payload).encode())

    assert event.event_type is CardEventType.AUTHORIZATION_REVERSAL
    assert event.amount == Money(0, "USD")


async def test_a_reversal_whose_previous_attributes_omit_the_amount_falls_back(
    adapter: StripeIssuingAdapter,
) -> None:
    # `previous_attributes` carries only the fields that changed, so a reversal that
    # did not change `amount` has none to give back.
    payload = fixture("event_authorization_reversed")
    payload["data"]["previous_attributes"] = {"status": "pending"}

    event = await adapter.parse_webhook({}, json.dumps(payload).encode())

    assert event.event_type is CardEventType.AUTHORIZATION_REVERSAL
    assert event.amount == Money(0, "USD")


async def test_an_event_with_no_amount_reports_none_rather_than_zero(
    adapter: StripeIssuingAdapter,
) -> None:
    # `Money(0)` would be a claim that nothing was authorized; `None` says the
    # delivery did not carry a figure.
    payload = fixture("event_authorization_created")
    del payload["data"]["object"]["amount"]

    event = await adapter.parse_webhook({}, json.dumps(payload).encode())

    assert event.event_type is CardEventType.AUTHORIZATION
    assert event.amount is None


@pytest.mark.parametrize("amount", ["1234", 12.34, True], ids=["string", "float", "boolean"])
async def test_an_amount_that_is_not_integer_minor_units_is_a_parse_error(
    adapter: StripeIssuingAdapter, amount: Any
) -> None:
    # Money is integer minor units everywhere (SPEC.md §1), and a float that parsed
    # would put a rounding error into the ledger. `True` is an int subclass, which is
    # exactly why it is checked separately.
    payload = fixture("event_transaction_capture")
    payload["data"]["object"]["amount"] = amount

    with pytest.raises(WebhookParseError, match="minor units"):
        await adapter.parse_webhook({}, json.dumps(payload).encode())


async def test_the_close_that_follows_a_capture_is_not_a_second_settlement(
    adapter: StripeIssuingAdapter,
) -> None:
    # The load-bearing one. Stripe sends `issuing_transaction.created` *and* an
    # authorization update to `closed` for one purchase. Mapping both to SETTLEMENT
    # would double-count every card payment in the ledger.
    event = await adapter.parse_webhook({}, fixture_bytes("event_authorization_closed"))

    assert event.event_type is CardEventType.UNMAPPED
    # Recorded under its own label, so the ledger shows what it actually was.
    assert event.provider_event_type == "issuing_authorization.updated:closed"


async def test_a_real_time_authorization_request_is_not_an_authorization(
    adapter: StripeIssuingAdapter,
) -> None:
    # `issuing_authorization.request` is a two-second request for a decision, not a
    # record that money moved. This pipeline verifies, dedups and queues (SPEC.md
    # §4), so it is never the thing that answers one.
    event = await adapter.parse_webhook({}, fixture_bytes("event_authorization_request"))

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "issuing_authorization.request"
    assert event.card_id == CARD_ID


async def test_a_capture_is_a_settlement(adapter: StripeIssuingAdapter) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_transaction_capture"))

    assert event.event_type is CardEventType.SETTLEMENT
    assert event.card_id == CARD_ID
    # Stripe signs a capture negative; `CardEventType` already says which way the
    # money went, so a sign here would only invite double negation downstream.
    assert event.amount == Money(1234, "USD")


async def test_a_refund_is_a_refund(adapter: StripeIssuingAdapter) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_transaction_refund"))

    assert event.event_type is CardEventType.REFUND
    assert event.amount == Money(250, "USD")


async def test_a_transaction_type_we_do_not_model_keeps_its_own_label(
    adapter: StripeIssuingAdapter,
) -> None:
    # `refund_reversal` has no normalized equivalent, and calling it a refund would
    # move money the wrong way in the ledger.
    event = await adapter.parse_webhook({}, fixture_bytes("event_transaction_unknown_type"))

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "issuing_transaction.created:refund_reversal"


async def test_a_created_card_is_a_lifecycle_event_reporting_its_state(
    adapter: StripeIssuingAdapter,
) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_card_created"))

    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_id == CARD_ID
    assert event.cardholder_id == HOLDER_ID
    # No activation marker in the payload, so `inactive` means never activated.
    assert event.card_state is CardState.UNACTIVATED
    assert event.amount is None


async def test_an_activated_card_reports_active(adapter: StripeIssuingAdapter) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_card_activated"))

    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_state is CardState.ACTIVE
    # The delivery is the result of a call we made, and our own idempotency key came
    # back with it — which is what lets a handler tie an event to its cause.
    assert event.raw["request"]["idempotency_key"] is not None


async def test_a_frozen_card_reports_frozen_from_the_marker_in_the_payload(
    adapter: StripeIssuingAdapter,
) -> None:
    # Same ambiguity as on the REST path, resolved the same way: `inactive` with an
    # activation marker is a freeze, without one it is a card never used.
    payload = fixture("event_card_activated")
    payload["data"]["object"]["status"] = "inactive"

    event = await adapter.parse_webhook({}, json.dumps(payload).encode())

    assert event.card_state is CardState.FROZEN


async def test_an_unmappable_card_status_leaves_the_state_unset(
    adapter: StripeIssuingAdapter,
) -> None:
    # Still a lifecycle event, and still ledgered — but a guessed state is a claim
    # about whether the card can spend.
    event = await adapter.parse_webhook({}, fixture_bytes("event_card_unknown_status"))

    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_state is None


async def test_a_dispute_is_a_chargeback(adapter: StripeIssuingAdapter) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_dispute_created"))

    assert event.event_type is CardEventType.CHARGEBACK
    assert event.amount == Money(1234, "USD")
    # A dispute names a transaction, not a card, so there is no card id to report
    # rather than one derived from a guess.
    assert event.card_id is None


async def test_a_cardholder_event_is_not_a_card_lifecycle_event(
    adapter: StripeIssuingAdapter,
) -> None:
    # Nothing in `CardEventType` fits, and CARD_LIFECYCLE would be a lie about what
    # changed. Never dropped, though (SPEC.md §3.3).
    event = await adapter.parse_webhook({}, fixture_bytes("event_cardholder_created"))

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "issuing_cardholder.created"
    assert event.cardholder_id == HOLDER_ID
    assert event.card_id is None


async def test_an_event_family_we_have_no_opinion_about_is_recorded_anyway(
    adapter: StripeIssuingAdapter,
) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_token_created"))

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "issuing_token.created"
    assert event.card_id == CARD_ID


async def test_an_event_with_no_object_is_unmapped_rather_than_rejected(
    adapter: StripeIssuingAdapter,
) -> None:
    # Authentic and readable, so re-sending it changes nothing: better in the ledger
    # as unmapped than in the dead-letter table.
    event = await adapter.parse_webhook({}, fixture_bytes("event_without_an_object"))

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "issuing_transaction.created"
    assert event.card_id is None


async def test_stripe_has_no_three_ds_challenge_to_normalize(
    adapter: StripeIssuingAdapter,
) -> None:
    # Recorded as a finding rather than a gap: Stripe delivers the cardholder's
    # verification code itself and publishes no issuer-facing challenge webhook, so
    # this adapter never produces THREE_DS_CHALLENGE. Phase 7 gets that path from
    # the mock adapter's simulator, which SPEC.md §6 already allows for. If Stripe
    # ever adds one, `CardEventType` already has the member.
    assert CardEventType.THREE_DS_CHALLENGE in set(CardEventType)


# ---------------------------------------------------------------- raw data ----


async def test_expanded_objects_are_collapsed_so_no_personal_data_is_ledgered(
    adapter: StripeIssuingAdapter,
) -> None:
    # The divergence from Lithic's adapter, which keeps its payloads untouched.
    # Stripe's card event embeds the whole cardholder — name, phone, postal address
    # — and `raw` reaches the ledger's payload column.
    event = await adapter.parse_webhook({}, fixture_bytes("event_card_created"))

    flattened = json.dumps(event.raw)
    for leaked in ("Ada", "Lovelace", "+15555550123", "Analytical Engine", "10128"):
        assert leaked not in flattened, f"{leaked!r} reached the ledger payload"
    # Collapsed to the id it came from, so nothing is lost that a reconciler needs.
    assert event.raw["object"]["cardholder"] == HOLDER_ID


async def test_everything_that_is_not_an_expansion_survives(
    adapter: StripeIssuingAdapter,
) -> None:
    # Collapsing is narrow on purpose: only a nested Stripe object goes, because
    # only a nested Stripe object can carry somebody's name.
    event = await adapter.parse_webhook({}, fixture_bytes("event_authorization_created"))

    obj = event.raw["object"]
    assert obj["merchant_data"]["name"] == "THE ANALYTICAL HOTEL"
    assert obj["verification_data"]["cvc_check"] == "match"
    assert obj["status"] == "pending"
    # The card was expanded on the authorization; it is an id again.
    assert obj["card"] == CARD_ID


async def test_the_previous_attributes_are_kept_for_debugging(
    adapter: StripeIssuingAdapter,
) -> None:
    event = await adapter.parse_webhook({}, fixture_bytes("event_card_activated"))

    assert event.raw["previous_attributes"] == {"status": "inactive"}


# --------------------------------------------------------- unreadable body ----


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b"null", b'{"id":"evt_1","object":"event"}'],
    ids=["empty", "not-json", "not-an-object", "null", "no-type"],
)
async def test_an_unreadable_body_raises_a_parse_error(
    adapter: StripeIssuingAdapter, body: bytes
) -> None:
    # The receiver ledgers these as unmapped rather than retrying: the signature
    # already proved the delivery is genuine, so re-sending it changes nothing.
    with pytest.raises(WebhookParseError):
        await adapter.parse_webhook({}, body)


async def test_a_body_with_no_event_id_raises_rather_than_inventing_one(
    adapter: StripeIssuingAdapter,
) -> None:
    # `event_id` is the dedup key. A generated one would make every redelivery a new
    # event.
    payload = fixture("event_transaction_capture")
    del payload["id"]

    with pytest.raises(WebhookParseError, match="id"):
        await adapter.parse_webhook({}, json.dumps(payload).encode())


async def test_an_unreadable_event_timestamp_falls_back_to_our_clock(
    adapter: StripeIssuingAdapter,
) -> None:
    payload = fixture("event_transaction_capture") | {"created": "yesterday"}

    event = await adapter.parse_webhook({}, json.dumps(payload).encode())

    assert event.occurred_at == NOW
