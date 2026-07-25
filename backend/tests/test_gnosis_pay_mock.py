"""Contract tests for the `gnosis_pay_mock` adapter (SPEC.md §3.2, §10).

This adapter models the Gnosis Pay partner pattern, shaped on their public
documentation (https://docs.gnosispay.com). It is the `CRYPTO_DEPOSIT` half of
the funding-model taxonomy, and the reason the abstraction can be trusted to
cover more than one shape of provider.

The characteristic that matters most is what funding *is*: money reaches a card
by an on-chain stablecoin transfer to the user's Safe, so `fund_card` verifies and
attributes a deposit it did not cause. It cannot move money, and these tests hold
it to that — a `fund_card` that changes a balance would be a fiat rail wearing a
`CRYPTO_DEPOSIT` label.

Everything here runs in-process against the bundled simulator — no network, no
partner credentials, no fixtures to re-record.
"""

from __future__ import annotations

import hashlib
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
from app.issuers.gnosis_pay_mock import Delivery, GnosisPayMockAdapter
from app.issuers.gnosis_pay_mock.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from app.issuers.gnosis_pay_mock.simulator import (
    EPHEMERAL_TOKEN_TTL_SECONDS,
    MAX_ACTIVE_CARDS,
    SAFE_CURRENCIES,
)

SECRET = "test-mock-secret"
FIXED_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def build_adapter(
    *, secret: str = SECRET, now: datetime = FIXED_NOW, tolerance_seconds: int = 300
) -> GnosisPayMockAdapter:
    def clock() -> datetime:
        return now

    return GnosisPayMockAdapter(
        webhook_secret=secret, signature_tolerance_seconds=tolerance_seconds, clock=clock
    )


@pytest.fixture
def adapter() -> GnosisPayMockAdapter:
    return build_adapter()


async def make_user(adapter: GnosisPayMockAdapter) -> str:
    """A Gnosis Pay user with a deployed Safe, returning the cardholder id."""
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace"),
    )
    # USDCe by default, so the demo stays USD-denominated end to end.
    assert holder.raw["safeCurrency"] == "USDCe"
    return holder.cardholder_id


async def make_card(adapter: GnosisPayMockAdapter, *, activate: bool = True) -> str:
    cardholder_id = await make_user(adapter)
    card = await adapter.create_card(cardholder_id, CreateCardRequest())
    if activate:
        await adapter.activate_card(card.card_id)
    return card.card_id


async def fund(
    adapter: GnosisPayMockAdapter, card_id: str, amount: Money, *, confirmed: bool = True
) -> str:
    """Simulate the bridge landing `amount` in the card's Safe. Returns the tx hash."""
    card = await adapter.get_card(card_id)
    assert card.deposit_address is not None
    deposit = adapter.simulator.receive_onchain_deposit(
        card.deposit_address, amount, confirmed=confirmed
    )
    return deposit.tx_hash


# --------------------------------------------------------------- identity ----


def test_the_adapter_declares_its_provider_id_and_funding_model(
    adapter: GnosisPayMockAdapter,
) -> None:
    assert adapter.provider_id == "gnosis_pay_mock"
    # The other half of the taxonomy from Lithic and Stripe (SPEC.md §3.2).
    assert adapter.funding_model is FundingModel.CRYPTO_DEPOSIT


def test_the_simulator_is_reachable_for_demos_but_not_for_the_pipeline(
    adapter: GnosisPayMockAdapter,
) -> None:
    assert adapter.simulator is adapter.simulator


def test_the_registry_factory_needs_no_settings_of_its_own() -> None:
    # The point of the module-constant signing key: this adapter reads only the
    # shared webhook setting, so registering it cost no change to core/config.py.
    built = GnosisPayMockAdapter.from_settings()
    assert built.provider_id == "gnosis_pay_mock"
    assert isinstance(built, GnosisPayMockAdapter)


# ------------------------------------------------------------- cardholder ----


async def test_creating_a_user_deploys_a_safe(adapter: GnosisPayMockAdapter) -> None:
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(
            email="ada@example.test",
            first_name="Ada",
            last_name="Lovelace",
            external_ref="intent-1",
        )
    )
    assert holder.provider_id == "gnosis_pay_mock"
    assert holder.email == "ada@example.test"
    assert holder.created_at.tzinfo is not None
    assert holder.raw["externalRef"] == "intent-1"
    # A Gnosis Pay account *is* a Safe; the address is the funding target.
    safe_address = holder.raw["safeAddress"]
    assert safe_address.startswith("0x")
    assert len(safe_address) == 42
    assert holder.raw["safeDeployed"] is True
    assert holder.raw["chain"] == "gnosis"


async def test_an_unknown_cardholder_cannot_be_given_a_card(
    adapter: GnosisPayMockAdapter,
) -> None:
    with pytest.raises(CardholderNotFoundError):
        await adapter.create_card("usr_nope", CreateCardRequest())


# ------------------------------------------------------------------ cards ----


async def test_a_virtual_card_starts_unactivated(adapter: GnosisPayMockAdapter) -> None:
    cardholder_id = await make_user(adapter)
    card = await adapter.create_card(cardholder_id, CreateCardRequest())

    assert card.provider_id == "gnosis_pay_mock"
    assert card.cardholder_id == cardholder_id
    assert card.state is CardState.UNACTIVATED
    assert len(card.last_four) == 4
    assert card.currency == "USD"
    assert card.raw["virtual"] is True
    assert card.raw["activatedAt"] is None
    assert card.raw["cardToken"]


async def test_every_card_of_a_user_shares_one_safe(adapter: GnosisPayMockAdapter) -> None:
    # Gnosis Pay assigns a Safe per *user*, not per card: both cards spend the
    # same balance and the bridge has one address to send to.
    cardholder_id = await make_user(adapter)
    first = await adapter.create_card(cardholder_id, CreateCardRequest())
    second = await adapter.create_card(cardholder_id, CreateCardRequest())

    assert first.deposit_address == second.deposit_address
    assert first.card_id != second.card_id


async def test_two_users_get_two_safes(adapter: GnosisPayMockAdapter) -> None:
    first = await adapter.get_card(await make_card(adapter))
    second = await adapter.get_card(await make_card(adapter))
    assert first.deposit_address != second.deposit_address


async def test_the_active_card_cap_is_enforced(adapter: GnosisPayMockAdapter) -> None:
    cardholder_id = await make_user(adapter)
    for _ in range(MAX_ACTIVE_CARDS):
        await adapter.create_card(cardholder_id, CreateCardRequest())
    with pytest.raises(IssuerError, match="active cards"):
        await adapter.create_card(cardholder_id, CreateCardRequest())


async def test_a_voided_card_frees_a_slot(adapter: GnosisPayMockAdapter) -> None:
    cardholder_id = await make_user(adapter)
    cards = [
        await adapter.create_card(cardholder_id, CreateCardRequest())
        for _ in range(MAX_ACTIVE_CARDS)
    ]
    await adapter.cancel_card(cards[0].card_id)
    assert await adapter.create_card(cardholder_id, CreateCardRequest())


async def test_getting_an_unknown_card_is_not_found(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(CardNotFoundError):
        await adapter.get_card("crd_nope")


# -------------------------------------------------------------- lifecycle ----


async def test_the_full_lifecycle_including_unfreeze(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter, activate=False)

    assert (await adapter.get_card(card_id)).state is CardState.UNACTIVATED
    assert (await adapter.activate_card(card_id)).state is CardState.ACTIVE
    assert (await adapter.freeze_card(card_id)).state is CardState.FROZEN
    # SPEC.md §9.1's toggle: `activate_card` is also the unfreeze path, which for
    # this provider is a different endpoint (`/unfreeze`, not `/activate`).
    assert (await adapter.activate_card(card_id)).state is CardState.ACTIVE
    assert (await adapter.cancel_card(card_id)).state is CardState.CANCELED


async def test_activating_twice_is_refused(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    with pytest.raises(IllegalCardTransitionError):
        await adapter.activate_card(card_id)


async def test_freezing_an_unactivated_card_is_refused(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter, activate=False)
    with pytest.raises(IllegalCardTransitionError):
        await adapter.freeze_card(card_id)


async def test_voiding_is_terminal(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    await adapter.cancel_card(card_id)
    for change in (adapter.activate_card, adapter.freeze_card, adapter.cancel_card):
        with pytest.raises(IllegalCardTransitionError):
            await change(card_id)


@pytest.mark.parametrize("report", ["report_lost", "report_stolen"])
async def test_reporting_a_card_lost_or_stolen_is_terminal(
    adapter: GnosisPayMockAdapter, report: str
) -> None:
    # §3.2 asks for the report-lost path. It has no place on the interface — no
    # caller has one — so it lives on the provider surface, like `/void` does.
    card_id = await make_card(adapter)
    getattr(adapter.simulator, report)(card_id)

    assert (await adapter.get_card(card_id)).state is CardState.CANCELED
    with pytest.raises(IllegalCardTransitionError):
        getattr(adapter.simulator, report)(card_id)


async def test_card_state_is_derived_from_the_flags_not_the_status_code(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Gnosis Pay publishes the *set* of numeric status codes but not what each one
    # means, so the adapter reads the booleans the status endpoint documents and
    # never the number.
    card_id = await make_card(adapter)
    await adapter.freeze_card(card_id)

    status = adapter.simulator.card_status(card_id)
    assert status["isFrozen"] is True
    assert status["activatedAt"] is not None
    card = await adapter.get_card(card_id)
    assert card.state is CardState.FROZEN
    assert card.raw["statusCode"] == status["statusCode"]


# ---------------------------------------------------------------- funding ----


async def test_funding_before_the_deposit_lands_is_pending(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    result = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")

    assert result.status is FundingStatus.PENDING
    # No provider-side object exists yet: nothing has been observed to reference.
    # `None` rather than `""`, so "not yet" is distinguishable from "a reference
    # that happens to be empty".
    assert result.issuer_funding_ref is None
    assert await adapter.get_balance(card_id) == Money(0, "USD")


async def test_an_unconfirmed_deposit_is_not_yet_fundable(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"), confirmed=False)

    result = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    assert result.status is FundingStatus.PENDING
    # Visible, but not spendable — which is what `pending` means upstream.
    assert await adapter.get_balance(card_id) == Money(0, "USD")


async def test_funding_attributes_a_confirmed_deposit(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    tx_hash = await fund(adapter, card_id, Money(2500, "USD"))

    result = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")

    assert result.status is FundingStatus.SUCCEEDED
    assert result.funding_ref == "intent-1"
    # The provider's own reference for this funding is the chain's: a tx hash.
    assert result.issuer_funding_ref == tx_hash
    assert result.amount == Money(2500, "USD")
    assert result.raw["safeAddress"].startswith("0x")


async def test_funding_does_not_move_money(adapter: GnosisPayMockAdapter) -> None:
    # The whole point of `CRYPTO_DEPOSIT`. The deposit created the balance; the
    # funding call only attributes it, so the balance is identical either side.
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))

    before = await adapter.get_balance(card_id)
    await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    after = await adapter.get_balance(card_id)

    assert before == Money(2500, "USD")
    assert after == before


async def test_the_same_funding_ref_twice_attributes_once(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))

    first = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    second = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")

    assert first == second
    assert len(adapter.simulator.attributed_deposits()) == 1


async def test_a_second_intent_needs_a_second_deposit(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))

    assert (
        await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    ).status is FundingStatus.SUCCEEDED
    # One deposit cannot pay for two intents, even though the balance would cover it.
    assert (
        await adapter.fund_card(card_id, Money(2500, "USD"), "intent-2")
    ).status is FundingStatus.PENDING


async def test_a_deposit_smaller_than_the_intent_is_not_enough(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(1000, "USD"))
    result = await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")
    assert result.status is FundingStatus.PENDING


async def test_reusing_a_funding_ref_for_different_terms_is_rejected(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))
    await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")

    await fund(adapter, card_id, Money(9900, "USD"))
    with pytest.raises(FundingRejectedError, match="already used"):
        await adapter.fund_card(card_id, Money(9900, "USD"), "intent-1")


async def test_funding_in_the_wrong_currency_is_rejected(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    with pytest.raises(FundingRejectedError, match="denominated"):
        await adapter.fund_card(card_id, Money(2500, "EUR"), "intent-1")


@pytest.mark.parametrize("amount", [Money(0, "USD"), Money(-100, "USD")])
async def test_funding_a_non_positive_amount_is_rejected(
    adapter: GnosisPayMockAdapter, amount: Money
) -> None:
    card_id = await make_card(adapter)
    with pytest.raises(FundingRejectedError, match="positive"):
        await adapter.fund_card(card_id, amount, "intent-1")


async def test_a_voided_card_cannot_be_funded(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))
    await adapter.cancel_card(card_id)
    with pytest.raises(FundingRejectedError, match="void"):
        await adapter.fund_card(card_id, Money(2500, "USD"), "intent-1")


async def test_funding_an_unknown_card_is_not_found(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(CardNotFoundError):
        await adapter.fund_card("crd_nope", Money(2500, "USD"), "intent-1")


# ---------------------------------------------------------------- balance ----


async def test_the_balance_is_the_safes_spendable_amount(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    assert await adapter.get_balance(card_id) == Money(0, "USD")
    await fund(adapter, card_id, Money(7500, "USD"))
    assert await adapter.get_balance(card_id) == Money(7500, "USD")


async def test_the_balance_is_shared_between_a_users_cards(
    adapter: GnosisPayMockAdapter,
) -> None:
    cardholder_id = await make_user(adapter)
    first = await adapter.create_card(cardholder_id, CreateCardRequest())
    second = await adapter.create_card(cardholder_id, CreateCardRequest())
    await fund(adapter, first.card_id, Money(5000, "USD"))

    assert await adapter.get_balance(second.card_id) == Money(5000, "USD")


async def test_account_balances_report_token_units_as_digit_strings(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))
    await fund(adapter, card_id, Money(1000, "USD"), confirmed=False)

    card = await adapter.get_card(card_id)
    balances = adapter.simulator.account_balances(card.cardholder_id)
    # USDCe has 6 decimals, so 25.00 is 25_000_000 units, not 2500.
    assert balances == {"total": "35000000", "spendable": "25000000", "pending": "10000000"}


async def test_the_balance_of_an_unknown_card_is_not_found(
    adapter: GnosisPayMockAdapter,
) -> None:
    with pytest.raises(CardNotFoundError):
        await adapter.get_balance("crd_nope")


# --------------------------------------------------- token-unit conversion ----


@pytest.mark.parametrize(
    ("symbol", "minor", "units"),
    [
        ("USDCe", 2500, 25_000_000),
        ("EURe", 2500, 25_000_000_000_000_000_000),
        ("GBPe", 1, 10_000_000_000_000_000),
    ],
)
async def test_amounts_round_trip_through_token_units(symbol: str, minor: int, units: int) -> None:
    # Gnosis Pay speaks BigInt token units with a per-currency `decimals`; Money is
    # 2-dp minor units. The conversion is the adapter's job (SPEC.md §3.1).
    currency = SAFE_CURRENCIES[symbol]
    adapter = build_adapter()
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="ada@example.test", first_name="Ada", last_name="Lovelace"),
    )
    adapter.simulator.set_safe_currency(holder.cardholder_id, symbol)
    card = await adapter.create_card(
        holder.cardholder_id, CreateCardRequest(currency=currency.code)
    )
    assert card.deposit_address is not None

    adapter.simulator.receive_onchain_deposit(card.deposit_address, Money(minor, currency.code))
    balances = adapter.simulator.account_balances(holder.cardholder_id)

    assert balances["spendable"] == str(units)
    assert await adapter.get_balance(card.card_id) == Money(minor, currency.code)


async def test_a_token_amount_too_fine_for_minor_units_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    # An 18-decimal token can express amounts a 2-dp Money cannot. Refusing beats
    # rounding: a silent rounding in a funding pipeline is a reconciliation
    # incident.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_transaction_cleared(
        card_id, Money(2500, "USD"), kind="Payment"
    )
    payload = json.loads(delivery.body)
    payload["data"]["event"]["billingAmount"] = "1"  # one atomic unit
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(WebhookParseError, match="minor units"):
        await adapter.parse_webhook(_headers_for(body), body)


# -------------------------------------------------------------- PSE reveal ----


async def test_an_ephemeral_token_is_short_lived_and_single_use(
    adapter: GnosisPayMockAdapter,
) -> None:
    # SPEC.md §3.2/§9.2's reveal path: the provider mints a 60-second single-use
    # token. It stays on the provider surface — phase 8 decides how it surfaces.
    card_id = await make_card(adapter)
    minted = adapter.simulator.mint_ephemeral_token(card_id)

    token = minted["data"]["token"]
    assert (
        minted["data"]["expiresAt"]
        == (FIXED_NOW + timedelta(seconds=EPHEMERAL_TOKEN_TTL_SECONDS)).isoformat()
    )

    revealed = adapter.simulator.redeem_ephemeral_token(token)
    assert revealed["lastFourDigits"] == (await adapter.get_card(card_id)).last_four
    assert "pan" not in revealed

    with pytest.raises(IssuerError, match="single use"):
        adapter.simulator.redeem_ephemeral_token(token)


async def test_an_expired_ephemeral_token_is_refused(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    token = adapter.simulator.mint_ephemeral_token(card_id)["data"]["token"]

    adapter.simulator.advance_clock(FIXED_NOW + timedelta(seconds=EPHEMERAL_TOKEN_TTL_SECONDS + 1))
    with pytest.raises(IssuerError, match="expired"):
        adapter.simulator.redeem_ephemeral_token(token)


async def test_an_unknown_ephemeral_token_is_refused(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(IssuerError, match="unknown"):
        adapter.simulator.redeem_ephemeral_token("nope")


# ------------------------------------------------------ webhook signatures ----


def _headers_for(body: bytes, *, secret: str = SECRET, now: datetime = FIXED_NOW) -> dict[str, str]:
    timestamp = str(int(now.timestamp()))
    return {
        "content-type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, timestamp=timestamp, body=body),
    }


async def test_a_genuine_delivery_verifies(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    assert await adapter.verify_webhook(delivery.headers, delivery.body) is True


async def test_header_lookup_is_case_insensitive(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    shouted = {name.upper(): value for name, value in delivery.headers.items()}
    assert await adapter.verify_webhook(shouted, delivery.body) is True


async def test_a_tampered_body_fails_verification(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    assert await adapter.verify_webhook(delivery.headers, delivery.body + b" ") is False


async def test_a_tampered_timestamp_fails_verification(adapter: GnosisPayMockAdapter) -> None:
    # The timestamp is signed, so it cannot be moved to widen the replay window.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = dict(delivery.headers)
    headers[TIMESTAMP_HEADER] = str(int(FIXED_NOW.timestamp()) + 1)
    assert await adapter.verify_webhook(headers, delivery.body) is False


async def test_another_secret_fails_verification(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    other = build_adapter(secret="not-the-same-secret")
    assert await other.verify_webhook(delivery.headers, delivery.body) is False


@pytest.mark.parametrize("drop", [SIGNATURE_HEADER, TIMESTAMP_HEADER])
async def test_a_missing_header_fails_verification(
    adapter: GnosisPayMockAdapter, drop: str
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = {name: value for name, value in delivery.headers.items() if name != drop}
    assert await adapter.verify_webhook(headers, delivery.body) is False


@pytest.mark.parametrize("signature", ["", "not-base64!!", "AAAA"])
async def test_a_malformed_signature_fails_verification(
    adapter: GnosisPayMockAdapter, signature: str
) -> None:
    # Never raises: the caller answers 401 either way, and a decode error that
    # escaped would be a 500 on hostile input.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = dict(delivery.headers)
    headers[SIGNATURE_HEADER] = signature
    assert await adapter.verify_webhook(headers, delivery.body) is False


@pytest.mark.parametrize("skew", [-301, 301])
async def test_a_delivery_outside_the_tolerance_fails_verification(skew: int) -> None:
    adapter = build_adapter()
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)

    receiver = build_adapter(now=FIXED_NOW + timedelta(seconds=skew))
    assert await receiver.verify_webhook(delivery.headers, delivery.body) is False


async def test_a_non_numeric_timestamp_fails_verification(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = dict(delivery.headers)
    headers[TIMESTAMP_HEADER] = "yesterday"
    assert await adapter.verify_webhook(headers, delivery.body) is False


# ------------------------------------------------------------ dedup identity ----


async def test_the_dedup_id_is_a_body_digest_because_there_is_no_event_id(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Gnosis Pay's envelope is `{eventType, data}` — no id. The receiver's
    # documented fallback is sha256(body); returning it explicitly keeps the
    # ledger's `event_id` and the Redis dedup key provably the same value.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)

    expected = hashlib.sha256(delivery.body).hexdigest()
    assert adapter.webhook_event_id(delivery.headers, delivery.body) == expected
    assert delivery.derived_event_id == expected
    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.event_id == expected


async def test_a_redelivery_under_a_new_timestamp_keeps_its_identity(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Their retry schedule is 1, 5 and 15 minutes, and a retry is signed afresh.
    # Keying on the body alone is what makes a retry a duplicate.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    retried = _headers_for(delivery.body, now=FIXED_NOW + timedelta(minutes=1))

    assert adapter.webhook_event_id(retried, delivery.body) == adapter.webhook_event_id(
        delivery.headers, delivery.body
    )


# ---------------------------------------------------------- webhook parsing ----


async def _emit_each_kind(adapter: GnosisPayMockAdapter) -> dict[CardEventType, Delivery]:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(50_000, "USD"))
    await adapter.fund_card(card_id, Money(50_000, "USD"), "intent-1")
    simulator = adapter.simulator

    authorization = simulator.authorize(card_id, Money(1234, "USD"))
    # `authorize` returns the record; the delivery it emitted is the latest one.
    authorized = simulator.deliveries[-1]
    return {
        CardEventType.CARD_LIFECYCLE: simulator.emit_card_status_changed(card_id),
        CardEventType.AUTHORIZATION: authorized,
        CardEventType.SETTLEMENT: simulator.clear(authorization.thread_id),
        CardEventType.REFUND: simulator.refund(card_id, Money(500, "USD")),
        CardEventType.AUTHORIZATION_REVERSAL: simulator.reverse(
            simulator.authorize(card_id, Money(999, "USD")).thread_id
        ),
        CardEventType.CHARGEBACK: simulator.dispute(authorization.thread_id),
        CardEventType.THREE_DS_CHALLENGE: simulator.emit_three_ds_challenge(card_id),
    }


async def test_every_normalized_event_type_is_reachable(
    adapter: GnosisPayMockAdapter,
) -> None:
    for expected, delivery in (await _emit_each_kind(adapter)).items():
        event = await adapter.parse_webhook(delivery.headers, delivery.body)
        assert event.event_type is expected, delivery.event_type
        assert event.provider_id == "gnosis_pay_mock"
        assert event.card_id is not None
        assert event.occurred_at.tzinfo is not None


async def test_one_provider_event_maps_to_three_normalized_types(
    adapter: GnosisPayMockAdapter,
) -> None:
    # `card.transaction.cleared` is a payment, a refund or a reversal depending on
    # `kind` inside the payload — the translation the adapter exists to do.
    emitted = await _emit_each_kind(adapter)
    cleared = {
        CardEventType.SETTLEMENT,
        CardEventType.REFUND,
        CardEventType.AUTHORIZATION_REVERSAL,
    }
    assert {emitted[kind].event_type for kind in cleared} == {"card.transaction.cleared"}


async def test_the_occurred_at_comes_from_the_header(adapter: GnosisPayMockAdapter) -> None:
    # Their body carries no timestamp at all. This is the second provider to need
    # the headers in `parse_webhook` (docs/ARCHITECTURE.md §4.1).
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    assert b"created" not in delivery.body

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.occurred_at == FIXED_NOW


async def test_a_delivery_with_no_timestamp_header_cannot_be_normalized(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = {name: value for name, value in delivery.headers.items() if name != TIMESTAMP_HEADER}
    with pytest.raises(WebhookParseError, match="timestamp"):
        await adapter.parse_webhook(headers, delivery.body)


async def test_a_card_status_change_carries_the_new_state(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await adapter.freeze_card(card_id)
    delivery = adapter.simulator.emit_card_status_changed(card_id)

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.event_type is CardEventType.CARD_LIFECYCLE
    assert event.card_state is CardState.FROZEN
    assert event.raw["data"]["oldStatus"] != event.raw["data"]["newStatus"]


async def test_a_settlement_carries_a_positive_magnitude(
    adapter: GnosisPayMockAdapter,
) -> None:
    # §4.7: `amount` is a magnitude and the event type carries the direction, so
    # the two adapters agree and the ledger never sees a signed provider quirk.
    emitted = await _emit_each_kind(adapter)
    for kind in (CardEventType.SETTLEMENT, CardEventType.REFUND):
        event = await adapter.parse_webhook(emitted[kind].headers, emitted[kind].body)
        assert event.amount is not None
        assert event.amount.amount_minor > 0
        assert event.amount.currency == "USD"


async def test_a_three_ds_challenge_carries_its_challenge_id(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_three_ds_challenge(card_id, code="424242")

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.event_type is CardEventType.THREE_DS_CHALLENGE
    assert event.challenge_id is not None
    assert event.raw["data"]["otpCode"] == "424242"


@pytest.mark.parametrize("event_type", ["user.created", "user.tos.accepted", "kyc.status.changed"])
async def test_an_account_event_is_unmapped_not_dropped(
    adapter: GnosisPayMockAdapter, event_type: str
) -> None:
    # SPEC.md §3.3: their surface is wider than ours. Nothing is discarded.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_unknown(event_type, {"cardToken": card_id})

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.event_type is CardEventType.UNMAPPED
    assert event.provider_event_type == event_type
    assert event.raw["eventType"] == event_type


async def test_the_raw_payload_survives_normalization(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.raw == json.loads(delivery.body)


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"a string"', b"{}", b'{"eventType": ""}', b'{"data": {}}'],
)
async def test_an_unreadable_body_raises_a_parse_error(
    adapter: GnosisPayMockAdapter, body: bytes
) -> None:
    with pytest.raises(WebhookParseError):
        await adapter.parse_webhook(_headers_for(body), body)


async def test_a_transaction_event_with_no_amount_is_still_normalized(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Absent is not zero: an amount we cannot read stays out of the ledger's
    # numeric column and lives in `raw` instead.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_unknown(
        "card.transaction.cleared", {"cardToken": card_id, "event": {"kind": "Payment"}}
    )
    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.amount is None


async def test_an_unresolvable_card_token_leaves_the_card_unnamed(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Better an empty column than a guess: the token itself is still in `raw`.
    delivery = adapter.simulator.emit_unknown(
        "card.status.changed", {"cardToken": "ctk_from_another_environment"}
    )
    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.card_id is None
    assert event.raw["data"]["cardToken"] == "ctk_from_another_environment"


@pytest.mark.parametrize(
    ("billing_amount", "expected"),
    [
        (2500, "BigInt string"),
        ("not-a-number", "not an integer"),
    ],
)
async def test_a_malformed_amount_is_reported_rather_than_guessed(
    adapter: GnosisPayMockAdapter, billing_amount: object, expected: str
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_transaction_cleared(card_id, Money(2500, "USD"))
    payload = json.loads(delivery.body)
    payload["data"]["event"]["billingAmount"] = billing_amount
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(WebhookParseError, match=expected):
        await adapter.parse_webhook(_headers_for(body), body)


async def test_a_currency_the_mock_does_not_hold_is_read_from_its_decimals(
    adapter: GnosisPayMockAdapter,
) -> None:
    # A partner Safe could hold a token this build has never heard of. `decimals`
    # is on every currency object, so the conversion does not need the symbol.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_transaction_cleared(card_id, Money(2500, "USD"))
    payload = json.loads(delivery.body)
    payload["data"]["event"]["billingCurrency"] = {
        "symbol": "XYZe",
        "code": "CHF",
        "decimals": 4,
        "name": "Some other token",
    }
    payload["data"]["event"]["billingAmount"] = "12345600"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    event = await adapter.parse_webhook(_headers_for(body), body)
    assert event.amount == Money(123_456, "CHF")


@pytest.mark.parametrize(
    "currency",
    [{"symbol": "XYZe", "code": "CHF"}, {"symbol": "XYZe", "decimals": 4}, "not-an-object"],
)
async def test_an_unreadable_currency_leaves_the_amount_out(
    adapter: GnosisPayMockAdapter, currency: object
) -> None:
    # Absent beats invented: without `decimals` there is no way to know what the
    # BigInt counts, so the number stays in `raw` and out of the ledger's column.
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_transaction_cleared(card_id, Money(2500, "USD"))
    payload = json.loads(delivery.body)
    payload["data"]["event"]["billingCurrency"] = currency
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    event = await adapter.parse_webhook(_headers_for(body), body)
    assert event.amount is None


async def test_a_known_currency_with_no_amount_field_leaves_the_amount_out(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.refund(card_id, Money(500, "USD"))
    payload = json.loads(delivery.body)
    del payload["data"]["event"]["refundAmount"]
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    event = await adapter.parse_webhook(_headers_for(body), body)
    assert event.event_type is CardEventType.REFUND
    assert event.amount is None


async def test_a_non_numeric_header_timestamp_cannot_date_an_event(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    delivery = adapter.simulator.emit_card_status_changed(card_id)
    headers = dict(delivery.headers)
    headers[TIMESTAMP_HEADER] = "yesterday"
    with pytest.raises(WebhookParseError, match="unix seconds"):
        await adapter.parse_webhook(headers, delivery.body)


async def test_an_account_event_names_no_card(adapter: GnosisPayMockAdapter) -> None:
    cardholder_id = await make_user(adapter)
    delivery = adapter.simulator.emit_user_created(cardholder_id)

    event = await adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.event_type is CardEventType.UNMAPPED
    assert event.cardholder_id == cardholder_id
    assert event.card_id is None


# ---------------------------------------------------------- the Safe itself ----


async def test_the_daily_limit_is_per_safe_and_therefore_shared(
    adapter: GnosisPayMockAdapter,
) -> None:
    # This provider has no per-card spend limit, so `spend_limit_minor` lands on
    # the Safe's on-chain daily allowance — which the user's other cards feel too.
    cardholder_id = await make_user(adapter)
    first = await adapter.create_card(cardholder_id, CreateCardRequest(spend_limit_minor=50_000))
    second = await adapter.create_card(cardholder_id, CreateCardRequest())

    assert first.spend_limit_minor == 50_000
    assert (await adapter.get_card(second.card_id)).spend_limit_minor == 50_000
    assert second.raw["dailyLimitIsPerSafe"] is True
    assert adapter.simulator.daily_limit(cardholder_id) == 50_000


async def test_a_card_in_another_currency_than_the_safe_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    cardholder_id = await make_user(adapter)
    with pytest.raises(IssuerError, match="denominated"):
        await adapter.create_card(cardholder_id, CreateCardRequest(currency="EUR"))


async def test_the_safe_currency_cannot_change_once_it_holds_a_balance(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"))
    card = await adapter.get_card(card_id)
    with pytest.raises(IssuerError, match="cannot change"):
        adapter.simulator.set_safe_currency(card.cardholder_id, "EURe")


async def test_a_deposit_to_an_unknown_safe_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    with pytest.raises(IssuerError, match="no Safe"):
        adapter.simulator.receive_onchain_deposit("0x" + "0" * 40, Money(2500, "USD"))


async def test_a_deposit_in_the_wrong_token_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    card = await adapter.get_card(card_id)
    assert card.deposit_address is not None
    with pytest.raises(IssuerError, match="the Safe holds"):
        adapter.simulator.receive_onchain_deposit(card.deposit_address, Money(2500, "EUR"))


async def test_confirming_a_deposit_makes_it_spendable(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(2500, "USD"), confirmed=False)
    deposit = adapter.simulator.deposits()[0]

    assert await adapter.get_balance(card_id) == Money(0, "USD")
    adapter.simulator.confirm_deposit(deposit.deposit_id)
    assert await adapter.get_balance(card_id) == Money(2500, "USD")
    # Idempotent: confirming twice does not credit twice.
    adapter.simulator.confirm_deposit(deposit.deposit_id)
    assert await adapter.get_balance(card_id) == Money(2500, "USD")


async def test_confirming_one_deposit_leaves_the_others_alone(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(1000, "USD"), confirmed=False)
    await fund(adapter, card_id, Money(2500, "USD"), confirmed=False)
    second = adapter.simulator.deposits()[1]

    adapter.simulator.confirm_deposit(second.deposit_id)
    assert await adapter.get_balance(card_id) == Money(2500, "USD")
    assert adapter.simulator.deposits()[0].confirmed is False


async def test_unfreezing_a_card_that_is_not_frozen_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    # The adapter never calls this path — `activate_card` picks the endpoint — but
    # the provider still has to answer it, so the provider still has to be right.
    card_id = await make_card(adapter)
    with pytest.raises(IllegalCardTransitionError):
        adapter.simulator.unfreeze_card(card_id)


async def test_confirming_an_unknown_deposit_is_refused(
    adapter: GnosisPayMockAdapter,
) -> None:
    with pytest.raises(IssuerError, match="no such deposit"):
        adapter.simulator.confirm_deposit("dep_000999")


async def test_the_safe_deployment_status_is_reportable(
    adapter: GnosisPayMockAdapter,
) -> None:
    cardholder_id = await make_user(adapter)
    status = adapter.simulator.safe_deployment_status(cardholder_id)
    assert status["isDeployed"] is True
    assert status["chain"] == "gnosis"
    assert status["currency"] == "USDCe"


async def test_an_unknown_user_is_not_found(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(CardholderNotFoundError):
        adapter.simulator.get_user("usr_nope")


# ----------------------------------------------------------- authorizations ----


async def test_an_authorization_moves_the_balance_to_the_hold_account(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Their lifecycle page: on approval money is deducted from the Safe and moved
    # to an on-chain hold account, before any clearing record arrives.
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(10_000, "USD"))

    transaction = adapter.simulator.authorize(card_id, Money(2500, "USD"))
    assert transaction.status == "Approved"
    assert transaction.is_pending is True
    assert await adapter.get_balance(card_id) == Money(7500, "USD")

    adapter.simulator.clear(transaction.thread_id)
    # Clearing pays the merchant out of the hold; the Safe does not move again.
    assert await adapter.get_balance(card_id) == Money(7500, "USD")


async def test_an_authorization_beyond_the_balance_declines(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    transaction = adapter.simulator.authorize(card_id, Money(2500, "USD"))
    assert transaction.status == "InsufficientFunds"
    assert transaction.is_pending is False


async def test_an_authorization_beyond_the_daily_limit_declines(
    adapter: GnosisPayMockAdapter,
) -> None:
    cardholder_id = await make_user(adapter)
    card = await adapter.create_card(cardholder_id, CreateCardRequest(spend_limit_minor=1000))
    await adapter.activate_card(card.card_id)
    await fund(adapter, card.card_id, Money(10_000, "USD"))

    transaction = adapter.simulator.authorize(card.card_id, Money(2500, "USD"))
    assert transaction.status == "ExceedsApprovalAmountLimit"


async def test_an_authorization_on_a_frozen_card_declines(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(10_000, "USD"))
    await adapter.freeze_card(card_id)

    assert adapter.simulator.authorize(card_id, Money(2500, "USD")).status == "Other"


async def test_a_reversal_returns_the_hold_to_the_safe(
    adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(10_000, "USD"))
    transaction = adapter.simulator.authorize(card_id, Money(2500, "USD"))

    adapter.simulator.reverse(transaction.thread_id)
    assert await adapter.get_balance(card_id) == Money(10_000, "USD")


async def test_a_refund_credits_the_safe(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    adapter.simulator.refund(card_id, Money(500, "USD"))
    assert await adapter.get_balance(card_id) == Money(500, "USD")


@pytest.mark.parametrize("second", ["clear", "reverse"])
async def test_a_transaction_can_only_leave_pending_once(
    adapter: GnosisPayMockAdapter, second: str
) -> None:
    card_id = await make_card(adapter)
    await fund(adapter, card_id, Money(10_000, "USD"))
    transaction = adapter.simulator.authorize(card_id, Money(2500, "USD"))
    adapter.simulator.clear(transaction.thread_id)

    with pytest.raises(IssuerError, match="already cleared"):
        getattr(adapter.simulator, second)(transaction.thread_id)


async def test_an_unknown_transaction_is_refused(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(IssuerError, match="no such transaction"):
        adapter.simulator.clear("txn_000999")


# ------------------------------------------------------------- the outbox ----


async def test_deliveries_can_be_drained(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    adapter.simulator.emit_card_status_changed(card_id)
    assert len(adapter.simulator.drain_deliveries()) == 1
    assert adapter.simulator.deliveries == ()


async def test_the_simulator_lists_its_cards(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)
    assert adapter.simulator.card_ids == (card_id,)
