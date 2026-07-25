"""The issuer interface is a contract, so its shape is asserted (SPEC.md §3.1).

These tests are deliberately structural. The point of the abstraction is that
`funding/`, `ledger/`, `webhooks/` and the mobile client depend on *this* surface
and nothing else, so a silent change to it — a renamed method, a dropped event
type, a `Money` that accepts floats — is a breaking change and should fail here
rather than in phase 4 when the second real provider lands.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.money import Money
from app.issuers.base import (
    Card,
    CardEvent,
    CardEventType,
    Cardholder,
    CardholderNotFoundError,
    CardIssuerAdapter,
    CardNotFoundError,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingRejectedError,
    FundingResult,
    FundingStatus,
    IllegalCardTransitionError,
    IssuerError,
    WebhookParseError,
)
from app.issuers.lithic.client import LithicApiError
from app.issuers.stripe_issuing.client import StripeApiError

#: Exactly the methods SPEC.md §3.1 specifies, plus the two documented additions
#: (docs/ARCHITECTURE.md §3.3): `get_card` and `webhook_event_id`.
SPEC_METHODS = (
    "create_cardholder",
    "create_card",
    "activate_card",
    "freeze_card",
    "cancel_card",
    "fund_card",
    "get_balance",
    "verify_webhook",
    "parse_webhook",
)
ADDED_METHODS = ("get_card", "webhook_event_id")


@pytest.mark.parametrize("name", SPEC_METHODS)
def test_every_spec_method_exists_and_is_a_coroutine(name: str) -> None:
    method = getattr(CardIssuerAdapter, name)
    assert inspect.iscoroutinefunction(method), f"{name} must be async"


@pytest.mark.parametrize("name", SPEC_METHODS)
def test_every_spec_method_is_abstract(name: str) -> None:
    assert name in CardIssuerAdapter.__abstractmethods__


def test_the_interface_has_no_undocumented_methods() -> None:
    public = {
        name
        for name, value in vars(CardIssuerAdapter).items()
        if not name.startswith("_") and callable(value)
    }
    assert public == set(SPEC_METHODS) | set(ADDED_METHODS)


@pytest.mark.parametrize("name", ("verify_webhook", "parse_webhook"))
def test_both_webhook_methods_see_the_delivery_headers(name: str) -> None:
    # Phase 3 finding, from Lithic: the event id arrives in a `webhook-id` header
    # and some payloads (`card.created`) carry no timestamp at all, so a body-only
    # `parse_webhook` cannot fill in `CardEvent`. Widening it here rather than
    # smuggling headers past the interface is the whole point of the rule
    # (docs/ARCHITECTURE.md §4.1). This deviates from SPEC.md §3.1's sketch.
    params = list(inspect.signature(getattr(CardIssuerAdapter, name)).parameters)
    assert ["self", "headers", "body"] == params


def test_webhook_event_id_is_optional_for_adapters() -> None:
    # Adapters whose provider puts no id in the envelope inherit the default and
    # the receiver falls back to a body digest; implementing it is not a burden
    # on "a new issuer is one file".
    assert "webhook_event_id" not in CardIssuerAdapter.__abstractmethods__


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        CardIssuerAdapter()  # type: ignore[abstract]


def test_funding_models_are_exactly_the_two_in_the_spec() -> None:
    assert {member.value for member in FundingModel} == {"fiat_rail", "crypto_deposit"}


def test_normalized_event_types_cover_the_spec_list() -> None:
    # SPEC.md §3.3, plus `unmapped` for provider events we do not model.
    assert {member.value for member in CardEventType} == {
        "authorization",
        "authorization_reversal",
        "settlement",
        "refund",
        "chargeback",
        "three_ds_challenge",
        "card_lifecycle",
        "unmapped",
    }


def test_card_states_cover_the_lifecycle_methods() -> None:
    assert {member.value for member in CardState} == {
        "unactivated",
        "active",
        "frozen",
        "canceled",
    }


def _event(**overrides: object) -> CardEvent:
    defaults: dict[str, object] = {
        "provider_id": "gnosis_pay_mock",
        "event_id": "evt_1",
        "event_type": CardEventType.AUTHORIZATION,
        "occurred_at": datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        "card_id": "card_1",
        "amount": Money(1299, "USD"),
    }
    return CardEvent(**(defaults | overrides))  # type: ignore[arg-type]


def test_card_event_round_trips_through_json() -> None:
    # The EventBus ships CardEvents as JSON (SPEC.md §4 dispatch), so fidelity
    # through a serialize/deserialize cycle is a functional requirement.
    event = _event(raw={"nested": {"merchant": "Coffee"}})
    restored = CardEvent.model_validate_json(event.model_dump_json())
    assert restored == event
    assert restored.amount == Money(1299, "USD")


def test_card_event_amounts_stay_integer_minor_units() -> None:
    with pytest.raises(ValidationError):
        _event(amount={"amount_minor": 12.99, "currency": "USD"})


def test_card_event_timestamps_must_be_aware_and_are_normalized_to_utc() -> None:
    with pytest.raises(ValidationError):
        _event(occurred_at=datetime(2026, 7, 25, 12, 0))

    berlin = timezone(timedelta(hours=2))
    event = _event(occurred_at=datetime(2026, 7, 25, 14, 0, tzinfo=berlin))
    assert event.occurred_at == datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    assert event.occurred_at.tzinfo is UTC


def test_card_event_is_immutable() -> None:
    event = _event()
    with pytest.raises(ValidationError):
        event.card_id = "card_2"


def test_card_event_rejects_unknown_fields() -> None:
    # A provider-specific field must go in `raw`, not widen the normalized model.
    with pytest.raises(ValidationError):
        _event(lithic_specific_thing="nope")


def test_dtos_carry_no_secret_material() -> None:
    # Full PAN/CVV reveal is a separate short-lived, single-use path (SPEC.md
    # §9.2, phase 8); the everyday card DTO must not be able to hold them.
    forbidden = {"pan", "cvv", "cvc", "number", "secret", "api_key"}
    for model in (Card, Cardholder, CreateCardRequest, CreateCardholderRequest, FundingResult):
        assert not forbidden & set(model.model_fields)


def test_funding_result_reports_a_status_and_both_references() -> None:
    result = FundingResult(
        provider_id="gnosis_pay_mock",
        card_id="card_1",
        funding_ref="our-intent-id",
        issuer_funding_ref="their-id",
        status=FundingStatus.SUCCEEDED,
        amount=Money(2500, "USD"),
    )
    # Our ref is the idempotency key (SPEC.md §5.2 step 3); theirs is for support
    # tickets and reconciliation. Both are opaque strings.
    assert isinstance(result.funding_ref, str)
    assert isinstance(result.issuer_funding_ref, str)
    assert result.status is FundingStatus.SUCCEEDED


def test_the_issuers_reference_is_optional_because_pending_funding_has_none() -> None:
    # A `CRYPTO_DEPOSIT` provider asked to fund before a deposit confirms has no
    # provider-side object to name. `""` would have claimed one exists and is
    # nameless; `None` says nothing has been observed. `FundingIntent`'s column is
    # nullable for the same reason, so the DTO and the row now agree.
    result = FundingResult(
        provider_id="gnosis_pay_mock",
        card_id="card_1",
        funding_ref="our-intent-id",
        status=FundingStatus.PENDING,
        amount=Money(2500, "USD"),
    )
    assert result.issuer_funding_ref is None
    assert FundingResult.model_fields["issuer_funding_ref"].default is None


def test_deposit_address_is_optional_so_fiat_rail_adapters_need_not_invent_one() -> None:
    assert Card.model_fields["deposit_address"].default is None


# ------------------------------------------------------------------ errors ----
#
# The funding engine (phase 5) catches `IssuerError` and has to decide between
# retrying and failing the intent, knowing nothing about which provider raised.
# That decision is what `retryable` carries across the boundary.


@pytest.mark.parametrize(
    "error",
    [
        CardNotFoundError("card_1"),
        CardholderNotFoundError("ch_1"),
        IllegalCardTransitionError("card_1", CardState.CANCELED, CardState.ACTIVE),
        FundingRejectedError("card_1", "insufficient program balance"),
        WebhookParseError("body is not JSON"),
        IssuerError("something went wrong"),
    ],
)
def test_every_issuer_error_says_whether_a_retry_could_help(error: IssuerError) -> None:
    # Readable off any `IssuerError` without a cast or a provider import: an
    # engine that has to ask `isinstance(exc, LithicApiError)` first is an engine
    # that has to change when a fourth adapter lands.
    assert error.retryable is False


def test_not_retryable_is_the_default_because_a_wrong_true_loops_on_a_refusal() -> None:
    # A permanent refusal retried is worse than a failed intent: it spends the
    # retry budget a genuinely transient failure needed, and delays the FAILED_*
    # an operator has to see. So the flag is opt-in, per failure, at the adapter.
    assert IssuerError.retryable is False
    assert IssuerError("busy", retryable=True).retryable is True


def test_an_adapter_may_declare_a_permanently_retryable_error_type() -> None:
    # Construction must not clobber a subclass's own default, or "this failure is
    # always transient" would have to be restated at every raise site.
    class ProviderUnavailableError(IssuerError):
        retryable = True

    assert ProviderUnavailableError("upstream is down").retryable is True
    assert ProviderUnavailableError("gave up", retryable=False).retryable is False


@pytest.mark.parametrize("error_type", [LithicApiError, StripeApiError])
def test_both_real_providers_report_retryability_through_the_shared_base(
    error_type: type[IssuerError],
) -> None:
    # The two adapters already agreed on *which* failures are worth retrying
    # (429 and 5xx, and a timeout that answered nothing). What phase 5 adds is
    # that the agreement is legible from above `issuers/`.
    busy = error_type("service unavailable", status=503, retryable=True)  # type: ignore[call-arg]
    refused = error_type("no such card", status=404)  # type: ignore[call-arg]

    assert isinstance(busy, IssuerError) and isinstance(refused, IssuerError)
    assert busy.retryable is True
    assert refused.retryable is False
