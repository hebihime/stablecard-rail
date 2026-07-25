"""Contract tests for the `evm_deposit_mock` adapter (SPEC.md §3.2, §10).

This adapter models the crypto-funded issuer pattern: the provider assigns an
EVM deposit address per card and funding means a confirmed token deposit there.
It is the `CRYPTO_DEPOSIT` half of the funding-model taxonomy, and the reason the
abstraction can be trusted to cover more than one shape of provider.

Everything here runs in-process against the bundled simulator — no network, no
sandbox account, no fixtures to re-record.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.money import Money
from app.issuers.base import (
    CardEventType,
    CardholderNotFoundError,
    CardNotFoundError,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingRejectedError,
    FundingStatus,
    IllegalCardTransitionError,
    IssuerError,
    WebhookParseError,
)
from app.issuers.evm_deposit_mock import Delivery, EvmDepositMockAdapter

SECRET = "test-mock-secret"
FIXED_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def build_adapter(
    *, secret: str = SECRET, now: datetime = FIXED_NOW, tolerance_seconds: int = 300
) -> EvmDepositMockAdapter:
    def clock() -> datetime:
        return now

    return EvmDepositMockAdapter(
        webhook_secret=secret, signature_tolerance_seconds=tolerance_seconds, clock=clock
    )


@pytest.fixture
def adapter() -> EvmDepositMockAdapter:
    return build_adapter()


async def make_card(
    adapter: EvmDepositMockAdapter, *, currency: str = "USD", activate: bool = True
) -> str:
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(
        holder.cardholder_id, CreateCardRequest(currency=currency, spend_limit_minor=100_000)
    )
    if activate:
        await adapter.activate_card(card.card_id)
    return card.card_id


# --------------------------------------------------------------- identity ----


def test_the_adapter_declares_its_provider_id_and_funding_model(
    adapter: EvmDepositMockAdapter,
) -> None:
    assert adapter.provider_id == "evm_deposit_mock"
    assert adapter.funding_model is FundingModel.CRYPTO_DEPOSIT


# -------------------------------------------------------------- lifecycle ----


async def test_a_new_card_is_unactivated_and_carries_a_deposit_address(
    adapter: EvmDepositMockAdapter,
) -> None:
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest(currency="usd"))

    assert card.state is CardState.UNACTIVATED
    assert card.cardholder_id == holder.cardholder_id
    assert card.currency == "USD"
    assert len(card.last_four) == 4 and card.last_four.isdigit()
    # The defining feature of a CRYPTO_DEPOSIT issuer (SPEC.md §3.2).
    assert card.deposit_address.startswith("0x")  # type: ignore[union-attr]
    assert len(card.deposit_address) == 42  # type: ignore[arg-type]


async def test_deposit_addresses_are_stable_per_card_and_distinct_between_cards(
    adapter: EvmDepositMockAdapter,
) -> None:
    first = await adapter.get_card(await make_card(adapter))
    second = await adapter.get_card(await make_card(adapter))

    assert first.deposit_address == (await adapter.get_card(first.card_id)).deposit_address
    assert first.deposit_address != second.deposit_address


async def test_the_full_lifecycle_including_unfreeze(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter, activate=False)

    assert (await adapter.activate_card(card_id)).state is CardState.ACTIVE
    assert (await adapter.freeze_card(card_id)).state is CardState.FROZEN
    # Unfreeze is `activate_card` again — the mobile toggle in SPEC.md §9.1.
    assert (await adapter.activate_card(card_id)).state is CardState.ACTIVE
    assert (await adapter.cancel_card(card_id)).state is CardState.CANCELED


async def test_cancellation_is_terminal_at_the_provider(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    await adapter.cancel_card(card_id)

    for action in (adapter.activate_card, adapter.freeze_card, adapter.cancel_card):
        with pytest.raises(IllegalCardTransitionError):
            await action(card_id)


async def test_freezing_an_unactivated_card_is_refused(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter, activate=False)
    with pytest.raises(IllegalCardTransitionError) as caught:
        await adapter.freeze_card(card_id)
    assert caught.value.card_id == card_id
    assert caught.value.from_state is CardState.UNACTIVATED
    assert caught.value.to_state is CardState.FROZEN


async def test_unknown_entities_raise_typed_not_found_errors(
    adapter: EvmDepositMockAdapter,
) -> None:
    with pytest.raises(CardholderNotFoundError):
        await adapter.create_card("chr_nope", CreateCardRequest())
    for coro in (
        adapter.get_card("card_nope"),
        adapter.activate_card("card_nope"),
        adapter.get_balance("card_nope"),
    ):
        with pytest.raises(CardNotFoundError):
            await coro


# ----------------------------------------------------------------- funding ----


async def test_funding_credits_the_balance_and_reports_both_references(
    adapter: EvmDepositMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    assert await adapter.get_balance(card_id) == Money(0, "USD")

    result = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")

    assert result.status is FundingStatus.SUCCEEDED
    assert result.funding_ref == "intent-abc"
    assert result.issuer_funding_ref and result.issuer_funding_ref != "intent-abc"
    assert result.amount == Money(2500, "USD")
    assert await adapter.get_balance(card_id) == Money(2500, "USD")


async def test_the_same_funding_ref_twice_funds_once(adapter: EvmDepositMockAdapter) -> None:
    """SPEC.md §10: idempotent `fund_card` — same `funding_ref` twice, one funding.

    This is the property the whole retry story rests on: the engine may call
    `fund_card` again after a timeout without knowing whether the first call
    landed, so a replay must be indistinguishable from the original.
    """
    card_id = await make_card(adapter)

    first = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")
    second = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")

    assert second == first, "a replay must return the original result verbatim"
    assert await adapter.get_balance(card_id) == Money(2500, "USD")
    settlements = [d for d in adapter.simulator.deliveries if "settlement" in d.event_type]
    assert len(settlements) == 1, "a replay must not emit a second confirmation"


async def test_distinct_funding_refs_accumulate(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    await adapter.fund_card(card_id, Money(1500, "USD"), "intent-2")
    assert await adapter.get_balance(card_id) == Money(4000, "USD")


async def test_a_replay_with_a_different_amount_is_refused(
    adapter: EvmDepositMockAdapter,
) -> None:
    # Silently honouring the first amount would hide a caller bug; silently
    # honouring the second would double-fund. Refuse and let the caller see it.
    card_id = await make_card(adapter)
    await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")
    with pytest.raises(FundingRejectedError):
        await adapter.fund_card(card_id, Money(9900, "USD"), "intent-abc")
    assert await adapter.get_balance(card_id) == Money(2500, "USD")


async def test_funding_emits_a_provider_confirmation_carrying_the_funding_ref(
    adapter: EvmDepositMockAdapter,
) -> None:
    # SPEC.md §5.2 step 4 reconciles the intent from this event.
    card_id = await make_card(adapter)
    await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")

    event = await adapter.parse_webhook(adapter.simulator.deliveries[-1].body)
    assert event.event_type is CardEventType.SETTLEMENT
    assert event.funding_ref == "intent-abc"
    assert event.amount == Money(2500, "USD")
    assert event.card_id == card_id


@pytest.mark.parametrize(
    ("amount", "note"),
    [(Money(2500, "EUR"), "currency mismatch"), (Money(0, "USD"), "non-positive")],
)
async def test_unfundable_amounts_are_refused(
    adapter: EvmDepositMockAdapter, amount: Money, note: str
) -> None:
    card_id = await make_card(adapter)
    with pytest.raises(FundingRejectedError):
        await adapter.fund_card(card_id, amount, f"intent-{note}")
    assert await adapter.get_balance(card_id) == Money(0, "USD")


async def test_a_canceled_card_cannot_be_funded(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    await adapter.cancel_card(card_id)
    with pytest.raises(FundingRejectedError):
        await adapter.fund_card(card_id, Money(2500, "USD"), "intent-abc")


async def test_funding_an_unknown_card_is_not_found(adapter: EvmDepositMockAdapter) -> None:
    with pytest.raises(CardNotFoundError):
        await adapter.fund_card("card_nope", Money(2500, "USD"), "intent-abc")


# --------------------------------------------------- webhook verification ----


async def test_a_genuine_delivery_verifies(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    assert await adapter.verify_webhook(delivery.headers, delivery.body) is True


async def test_header_lookup_is_case_insensitive(adapter: EvmDepositMockAdapter) -> None:
    # ASGI hands headers over lower-cased; a curl user will not.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    shouty = {name.upper(): value for name, value in delivery.headers.items()}
    assert await adapter.verify_webhook(shouty, delivery.body) is True


async def test_a_tampered_body_fails_verification(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    tampered = delivery.body.replace(b"1299", b"999999")
    assert tampered != delivery.body
    assert await adapter.verify_webhook(delivery.headers, tampered) is False


async def test_a_tampered_event_id_fails_verification(adapter: EvmDepositMockAdapter) -> None:
    # The event id is the dedup key, so the signature must cover it: otherwise an
    # attacker could re-send a legitimate body under a fresh id and be processed
    # twice (SPEC.md §4).
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    headers = dict(delivery.headers) | {"x-mock-event-id": "evt_forged"}
    assert await adapter.verify_webhook(headers, delivery.body) is False


async def test_another_secret_fails_verification(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    impostor = build_adapter(secret="not-the-secret")
    assert await impostor.verify_webhook(delivery.headers, delivery.body) is False


@pytest.mark.parametrize(
    "drop", ["x-mock-signature", "x-mock-timestamp", "x-mock-event-id"], ids=lambda h: f"no-{h}"
)
async def test_a_missing_signature_header_fails_verification(
    adapter: EvmDepositMockAdapter, drop: str
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    headers = {k: v for k, v in delivery.headers.items() if k != drop}
    assert await adapter.verify_webhook(headers, delivery.body) is False


@pytest.mark.parametrize("signature", ["", "deadbeef", "v2=deadbeef", "v1=", "v1=not-hex"])
async def test_malformed_signatures_fail_verification(
    adapter: EvmDepositMockAdapter, signature: str
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    headers = dict(delivery.headers) | {"x-mock-signature": signature}
    assert await adapter.verify_webhook(headers, delivery.body) is False


@pytest.mark.parametrize("skew", [timedelta(minutes=10), timedelta(minutes=-10)])
async def test_a_stale_or_future_timestamp_fails_verification(skew: timedelta) -> None:
    # A captured delivery must not be replayable forever, in either direction.
    signer = build_adapter()
    card_id = await make_card(signer)
    delivery = signer.simulator.emit_authorization(card_id, Money(1299, "USD"))

    verifier = build_adapter(now=FIXED_NOW + skew)
    assert await verifier.verify_webhook(delivery.headers, delivery.body) is False


async def test_a_timestamp_inside_the_tolerance_still_verifies() -> None:
    signer = build_adapter()
    card_id = await make_card(signer)
    delivery = signer.simulator.emit_authorization(card_id, Money(1299, "USD"))

    verifier = build_adapter(now=FIXED_NOW + timedelta(seconds=299))
    assert await verifier.verify_webhook(delivery.headers, delivery.body) is True


async def test_a_non_numeric_timestamp_fails_verification(adapter: EvmDepositMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    headers = dict(delivery.headers) | {"x-mock-timestamp": "yesterday"}
    assert await adapter.verify_webhook(headers, delivery.body) is False


# ---------------------------------------------------------- event parsing ----


async def _emit_each_kind(adapter: EvmDepositMockAdapter) -> dict[CardEventType, Delivery]:
    sim = adapter.simulator
    card_id = await make_card(adapter)
    authorization = sim.emit_authorization(card_id, Money(1299, "USD"))
    authorization_id = json.loads(authorization.body)["data"]["authorization_id"]
    return {
        CardEventType.AUTHORIZATION: authorization,
        CardEventType.AUTHORIZATION_REVERSAL: sim.emit_authorization_reversal(authorization_id),
        CardEventType.SETTLEMENT: sim.emit_settlement(card_id, Money(1299, "USD")),
        CardEventType.REFUND: sim.emit_refund(card_id, Money(500, "USD")),
        CardEventType.CHARGEBACK: sim.emit_chargeback(card_id, Money(1299, "USD")),
        CardEventType.THREE_DS_CHALLENGE: sim.emit_three_ds_challenge(card_id),
        CardEventType.CARD_LIFECYCLE: sim.emit_card_lifecycle(card_id),
    }


async def test_every_normalized_event_type_is_produced_and_parsed(
    adapter: EvmDepositMockAdapter,
) -> None:
    # SPEC.md §3.3: the simulator exercises the whole normalized vocabulary, so
    # downstream consumers can be written against all of it before a real
    # provider exists.
    for expected, delivery in (await _emit_each_kind(adapter)).items():
        event = await adapter.parse_webhook(delivery.body)
        assert event.event_type is expected, delivery.event_type
        assert event.provider_id == "evm_deposit_mock"
        assert event.event_id == delivery.event_id
        assert event.occurred_at.tzinfo is UTC
        assert event.raw == json.loads(delivery.body)


async def test_the_provider_vocabulary_is_translated_not_passed_through(
    adapter: EvmDepositMockAdapter,
) -> None:
    # The provider says `card.state_changed`; we say `card_lifecycle`. Proving
    # these differ is proving normalization actually happens.
    card_id = await make_card(adapter)
    await adapter.freeze_card(card_id)
    delivery = adapter.simulator.emit_card_lifecycle(card_id)

    event = await adapter.parse_webhook(delivery.body)
    assert delivery.event_type == "card.state_changed"
    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.provider_event_type == "card.state_changed"
    assert event.card_state is CardState.FROZEN


async def test_amounts_survive_parsing_as_integer_minor_units(
    adapter: EvmDepositMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    event = await adapter.parse_webhook(delivery.body)
    assert event.amount == Money(1299, "USD")
    assert isinstance(event.amount.amount_minor, int)


async def test_an_unknown_provider_event_normalizes_to_unmapped(
    adapter: EvmDepositMockAdapter,
) -> None:
    # SPEC.md §3.3: unknown provider events are never dropped silently.
    delivery = adapter.simulator.emit_unknown(
        "card.quantum_entangled", {"card_id": "card_000001", "surprise": True}
    )
    event = await adapter.parse_webhook(delivery.body)

    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == "card.quantum_entangled"
    assert event.raw["data"]["surprise"] is True
    assert event.event_id == delivery.event_id


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"[1, 2, 3]",
        b'"a string"',
        b"{}",
        b'{"id": "evt_1"}',
        b'{"id": "evt_1", "type": "card.authorization"}',
        b'{"id": "evt_1", "type": "card.authorization", "created": "yesterday"}',
        b'{"id": "evt_1", "type": "card.authorization", "created": "2026-07-25T12:00:00"}',
        b'{"id": "", "type": "card.authorization", "created": "2026-07-25T12:00:00Z"}',
    ],
    ids=[
        "empty",
        "garbage",
        "array",
        "scalar",
        "no-envelope",
        "no-type",
        "no-created",
        "unparseable-created",
        "naive-created",
        "blank-id",
    ],
)
async def test_an_unreadable_body_raises_a_parse_error(
    adapter: EvmDepositMockAdapter, body: bytes
) -> None:
    # The receiver turns this into an `unmapped` ledger entry rather than a retry
    # loop: the delivery is authentic, so re-sending it will not help.
    with pytest.raises(WebhookParseError):
        await adapter.parse_webhook(body)


@pytest.mark.parametrize(
    "data",
    [
        {"amount_minor": 12.99, "currency": "USD"},
        {"amount_minor": True, "currency": "USD"},
        {"amount_minor": "1299", "currency": "USD"},
        {"amount_minor": 1299, "currency": 840},
    ],
    ids=["float", "bool", "string", "numeric-currency"],
)
async def test_an_amount_that_is_not_integer_minor_units_is_refused(
    adapter: EvmDepositMockAdapter, data: dict[str, object]
) -> None:
    # A provider sending a decimal amount is a reconciliation incident waiting to
    # happen. Refuse it loudly; the receiver records it as unmapped with the raw
    # bytes attached, which is a bug report rather than a silent rounding.
    delivery = adapter.simulator.emit_unknown("card.note", data)
    with pytest.raises(WebhookParseError):
        await adapter.parse_webhook(delivery.body)


async def test_a_card_state_we_do_not_model_parses_to_no_state(
    adapter: EvmDepositMockAdapter,
) -> None:
    # Providers add states. An unrecognized one must not fail the delivery — the
    # event is still real, and `raw` keeps whatever they actually said.
    delivery = adapter.simulator.emit_unknown(
        "card.state_changed", {"card_id": "card_000001", "state": "pending_review"}
    )
    event = await adapter.parse_webhook(delivery.body)
    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_state is None
    assert event.raw["data"]["state"] == "pending_review"


async def test_a_missing_amount_parses_to_no_amount_rather_than_zero(
    adapter: EvmDepositMockAdapter,
) -> None:
    delivery = adapter.simulator.emit_unknown("card.note", {"card_id": "card_000001"})
    event = await adapter.parse_webhook(delivery.body)
    assert event.amount is None, "absent is not the same as zero"


# ------------------------------------------------------------- dedup keys ----


async def test_the_event_id_is_readable_from_the_envelope_before_parsing(
    adapter: EvmDepositMockAdapter,
) -> None:
    # Dedup runs before parse (SPEC.md §4), so the id must be available without
    # trusting the body to be well-formed.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    assert adapter.webhook_event_id(delivery.headers, delivery.body) == delivery.event_id


async def test_event_id_extraction_returns_none_when_the_envelope_lacks_one(
    adapter: EvmDepositMockAdapter,
) -> None:
    assert adapter.webhook_event_id({}, b"{}") is None


# ------------------------------------------------------------- simulator ----


async def test_identifiers_are_deterministic_so_demos_reproduce() -> None:
    def ids(adapter: EvmDepositMockAdapter) -> list[str]:
        return [adapter.provider_id, *sorted(adapter.simulator.card_ids)]

    first, second = build_adapter(), build_adapter()
    for adapter in (first, second):
        await make_card(adapter)
        await make_card(adapter)
    assert ids(first) == ids(second)


async def test_draining_deliveries_hands_them_over_once(
    adapter: EvmDepositMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))
    assert len(adapter.simulator.drain_deliveries()) == 1
    assert adapter.simulator.drain_deliveries() == ()


async def test_reversing_an_unknown_authorization_is_an_issuer_error(
    adapter: EvmDepositMockAdapter,
) -> None:
    with pytest.raises(IssuerError):
        adapter.simulator.emit_authorization_reversal("auth_nope")


async def test_emitting_for_an_unknown_card_is_not_found(
    adapter: EvmDepositMockAdapter,
) -> None:
    with pytest.raises(CardNotFoundError):
        adapter.simulator.emit_authorization("card_nope", Money(1299, "USD"))


def test_the_bundled_simulator_holds_no_state_between_adapters() -> None:
    # Two adapters must not share a class-level dict; the registry keeps one
    # instance per process precisely because the state is per-instance.
    first, second = build_adapter(), build_adapter()
    assert first.simulator is not second.simulator
    assert first.simulator.card_ids == () == second.simulator.card_ids
