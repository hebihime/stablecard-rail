"""The Stripe Issuing adapter: cardholders and the card lifecycle (phase 4).

Phase 4's purpose is to test the abstraction, not to extend it (SPEC.md §12.4), so
what these tests are really checking is that every awkward thing about Stripe can
be absorbed *here*. Four are worth naming, because each one is somewhere the
abstraction could have needed changing and did not:

**`inactive` means two different things.** Stripe has `active`, `inactive` and
`canceled`; `CardState` distinguishes a card that has never been activated from
one that was and is now blocked, and Stripe does not. It creates cards `inactive`
by default, so both readings are live. The adapter stores the missing bit itself,
in the card's own `metadata` — the same trick Lithic uses for funding refs
(docs/ARCHITECTURE.md §4.5), and the reason `freeze_card` does not report a card
as never-activated.

**A cardholder needs more identity than `CreateCardholderRequest` carries.**
Stripe requires a billing address and a display name of at most 24 characters with
no digits. Sandbox placeholders live in this module, exactly as Lithic's do; the
DTO did not have to grow a field for one provider.

**The card response embeds the whole cardholder**, name, email and address
included. `raw` reaches the ledger, so it is an allowlist, not a copy.

**Timestamps are Unix integers**, not the ISO strings Lithic sends — and so the
naive-datetime trap from phase 3 cannot recur here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from app.issuers import registry
from app.issuers.base import (
    CardholderNotFoundError,
    CardIssuerAdapter,
    CardNotFoundError,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    IllegalCardTransitionError,
    IssuerError,
)
from app.issuers.stripe_issuing.adapter import (
    ACTIVATED_AT_KEY,
    CARD_STATUSES,
    PROVIDER_ID,
    SANDBOX_ADDRESS,
    SANDBOX_TERMS_IP,
    StripeIssuingAdapter,
)
from app.issuers.stripe_issuing.client import StripeApiError, StripeClient

BASE_URL = "https://api.stripe.test/v1"
API_KEY = "sk_test_not_a_real_credential_0001"

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_issuing"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


#: Read out of the recorded fixtures rather than written down, so re-running
#: `scripts/record_stripe_fixtures.py` cannot break these tests on the ids alone.
#: Stripe's ids are opaque to us anyway (SPEC.md §1), so the literal never mattered.
CARD_ID: str = fixture("card_created")["id"]
HOLDER_ID: str = fixture("card_created")["cardholder"]["id"]


def utc(unix: int) -> datetime:
    return datetime.fromtimestamp(unix, tz=UTC)


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def adapter() -> StripeIssuingAdapter:
    return StripeIssuingAdapter(
        client=StripeClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0, sleep=no_sleep),
        webhook_secret="whsec_c3RhYmxlY2FyZA==",
        clock=lambda: datetime(2026, 7, 25, 18, 0, tzinfo=UTC),
    )


def sent_form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


# ---------------------------------------------------------------- identity ----


def test_the_provider_id_is_the_registry_key() -> None:
    # Stored on funding intents and ledger rows, so it is opaque and stable.
    assert PROVIDER_ID == "stripe_issuing"
    assert StripeIssuingAdapter.provider_id == PROVIDER_ID


def test_stripe_is_a_fiat_rail_issuer() -> None:
    # The second real one, which is what makes `describe()`'s taxonomy meaningful
    # rather than a two-element example.
    assert StripeIssuingAdapter.funding_model is FundingModel.FIAT_RAIL


def test_every_stripe_status_maps_to_one_of_ours() -> None:
    assert set(CARD_STATUSES) == {"active", "inactive", "canceled"}


# ------------------------------------------------------------- cardholders ----


@respx.mock
async def test_creating_a_cardholder_sends_the_identity_stripe_requires(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    holder = await adapter.create_cardholder(
        CreateCardholderRequest(
            email="ada@example.test",
            first_name="Ada",
            last_name="Lovelace",
            external_ref="user-42",
        )
    )

    sent = sent_form(route.calls.last.request)
    assert sent["type"] == "individual"
    assert sent["name"] == "Ada Lovelace"
    assert sent["email"] == "ada@example.test"
    # Required before a card of theirs can ever be activated.
    assert sent["individual[first_name]"] == "Ada"
    assert sent["individual[last_name]"] == "Lovelace"
    # So is card-issuing terms acceptance, on a US program. Without these two the
    # cardholder sits at `requirements.past_due` and *every* `activate_card` fails
    # with "This cardholder has outstanding requirements preventing them from
    # activating an issued card" — which a live account said and no published example
    # object shows (docs/ARCHITECTURE.md §8.10).
    assert sent["individual[card_issuing][user_terms_acceptance][ip]"] == SANDBOX_TERMS_IP
    assert int(sent["individual[card_issuing][user_terms_acceptance][date]"]) > 0
    # A billing address is required and `CreateCardholderRequest` does not carry
    # one, so the placeholder lives in the adapter rather than in the DTO.
    assert sent["billing[address][line1]"] == SANDBOX_ADDRESS["line1"]
    assert sent["billing[address][country]"] == SANDBOX_ADDRESS["country"]
    assert sent["billing[address][postal_code]"] == SANDBOX_ADDRESS["postal_code"]
    # Our own reference, echoed back rather than stored by them under a name of
    # their choosing.
    assert sent["metadata[stablecard_external_ref]"] == "user-42"

    assert holder.provider_id == PROVIDER_ID
    assert holder.cardholder_id == HOLDER_ID
    assert holder.email == "ada@example.test"
    assert holder.created_at == utc(fixture("cardholder_created")["created"])


@respx.mock
async def test_the_terms_acceptance_timestamp_is_the_adapters_clock(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe wants a Unix timestamp, and it is meant to be when the user accepted.
    # A sandbox program has no such moment, so "now" is the honest placeholder — and
    # a real program must pass the user's own IP and timestamp, which is the one place
    # `CreateCardholderRequest` would genuinely have to grow (§8.1).
    route = respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    await adapter.create_cardholder(
        CreateCardholderRequest(email="a@b.test", first_name="Ada", last_name="Lovelace")
    )

    sent = sent_form(route.calls.last.request)
    accepted_at = int(sent["individual[card_issuing][user_terms_acceptance][date]"])
    assert accepted_at == int(datetime(2026, 7, 25, 18, 0, tzinfo=UTC).timestamp())


@respx.mock
async def test_a_cardholder_without_an_external_ref_sends_no_metadata(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    await adapter.create_cardholder(
        CreateCardholderRequest(email="ada@example.test", first_name="Ada", last_name="Lovelace")
    )

    assert "metadata[stablecard_external_ref]" not in sent_form(route.calls.last.request)


@respx.mock
async def test_a_cardholder_record_keeps_no_personal_data_beyond_the_email(
    adapter: StripeIssuingAdapter,
) -> None:
    # `raw` ends up in the ledger's payload column, and Stripe's cardholder object
    # carries a name, a phone number and a postal address.
    respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="ada@example.test", first_name="Ada", last_name="Lovelace")
    )

    flattened = json.dumps(holder.raw)
    for leaked in ("Ada", "Lovelace", "+15555550123", "Analytical Engine", "10128"):
        assert leaked not in flattened, f"{leaked!r} reached the ledger payload"
    # What is kept is why a card might refuse to activate.
    assert "requirements" in holder.raw


@respx.mock
async def test_a_long_name_is_truncated_to_stripes_limit(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe caps the display name at 24 characters, and a 400 for a long surname
    # is not a useful failure.
    route = respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    await adapter.create_cardholder(
        CreateCardholderRequest(
            email="a@b.test", first_name="Augusta", last_name="Ada King-Noel Byron Lovelace"
        )
    )

    assert len(sent_form(route.calls.last.request)["name"]) <= 24


@respx.mock
async def test_digits_and_punctuation_are_stripped_from_the_name(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe documents "no special characters or numbers" for this field. Cleaning
    # it here beats a 400 the caller cannot act on.
    route = respx.post(f"{BASE_URL}/issuing/cardholders").mock(
        return_value=httpx.Response(200, json=fixture("cardholder_created"))
    )

    await adapter.create_cardholder(
        CreateCardholderRequest(email="a@b.test", first_name="Ada2", last_name="Lovelace!")
    )

    assert sent_form(route.calls.last.request)["name"] == "Ada Lovelace"


@respx.mock
async def test_a_name_with_no_letters_at_all_is_refused_before_the_call(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cardholders")

    with pytest.raises(IssuerError, match="name"):
        await adapter.create_cardholder(
            CreateCardholderRequest(email="a@b.test", first_name="123", last_name="456")
        )

    assert route.call_count == 0


@respx.mock
async def test_an_unknown_cardholder_is_a_typed_error(adapter: StripeIssuingAdapter) -> None:
    respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "resource_missing",
                    "message": "No such issuing cardholder: 'ich_nope'",
                    "param": "cardholder",
                    "type": "invalid_request_error",
                }
            },
        )
    )

    with pytest.raises(CardholderNotFoundError):
        await adapter.create_card("ich_nope", CreateCardRequest())


# ------------------------------------------------------------------- cards ----


@respx.mock
async def test_a_created_card_is_inactive_and_reports_itself_unactivated(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    card = await adapter.create_card(HOLDER_ID, CreateCardRequest(currency="USD"))

    sent = sent_form(route.calls.last.request)
    assert sent["cardholder"] == HOLDER_ID
    assert sent["type"] == "virtual"
    # Stripe's currency is lower-case; ours is not.
    assert sent["currency"] == "usd"
    # Stated rather than left to Stripe's default, so the state we return is not a
    # guess about what they did.
    assert sent["status"] == "inactive"

    assert card.provider_id == PROVIDER_ID
    assert card.card_id == CARD_ID
    assert card.cardholder_id == HOLDER_ID
    assert card.state is CardState.UNACTIVATED
    assert card.currency == "USD"
    recorded = fixture("card_created")
    assert card.last_four == recorded["last4"]
    assert card.exp_month == recorded["exp_month"]
    assert card.exp_year == recorded["exp_year"]
    # The recorder creates the card already limited, so this is its limit rather than
    # `None`; the no-limit case has its own test below.
    assert card.spend_limit_minor == recorded["spending_controls"]["spending_limits"][0]["amount"]
    # A fiat rail has no deposit address to hand out (SPEC.md §3.2).
    assert card.deposit_address is None
    assert card.created_at == utc(fixture("card_created")["created"])


@respx.mock
async def test_a_spend_limit_becomes_an_all_time_control(
    adapter: StripeIssuingAdapter,
) -> None:
    # `all_time` is what makes a spend limit behave like a balance. Any other
    # interval resets, and funding would leak away with it.
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_with_limit"))
    )

    card = await adapter.create_card(
        HOLDER_ID, CreateCardRequest(currency="USD", spend_limit_minor=100_000)
    )

    sent = sent_form(route.calls.last.request)
    assert sent["spending_controls[spending_limits][0][amount]"] == "100000"
    assert sent["spending_controls[spending_limits][0][interval]"] == "all_time"
    # NOT sent, though it is on the response object: `spending_limits_currency` is
    # read-only, derived from the card's own currency, and Stripe answers 400
    # "Received unknown parameter" if you try to set it. Found by recording against a
    # live account, after the phase shipped sending it (docs/ARCHITECTURE.md §8.10) —
    # which is precisely the class of mistake documentation-derived fixtures cannot
    # catch, since the field really is in the published card object.
    assert "spending_controls[spending_limits_currency]" not in sent
    assert card.spend_limit_minor == 100_000


@respx.mock
async def test_a_zero_spend_limit_is_legal_here_unlike_at_lithic(
    adapter: StripeIssuingAdapter,
) -> None:
    # Lithic reads `spend_limit: 0` as *unlimited*, so a card cannot start at zero
    # and be funded up. Stripe says "unlimited" by the *absence* of a limit, so a
    # zero all_time limit is a real "cannot spend yet" and is the natural base for
    # a funding raise (docs/ARCHITECTURE.md §8.4).
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_limited_to_zero"))
    )

    card = await adapter.create_card(HOLDER_ID, CreateCardRequest(spend_limit_minor=0))

    sent = sent_form(route.calls.last.request)
    assert sent["spending_controls[spending_limits][0][amount]"] == "0"
    assert card.spend_limit_minor == 0


@respx.mock
async def test_a_negative_spend_limit_is_refused_before_the_call(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards")

    with pytest.raises(IssuerError):
        await adapter.create_card(HOLDER_ID, CreateCardRequest(spend_limit_minor=-1))

    assert route.call_count == 0


@respx.mock
async def test_a_memo_is_kept_in_metadata_because_stripe_has_no_memo(
    adapter: StripeIssuingAdapter,
) -> None:
    # `second_line` is physical-card-only, so a virtual card's free text has
    # nowhere to go but the metadata map.
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await adapter.create_card(HOLDER_ID, CreateCardRequest(memo="Ada's travel card"))

    assert sent_form(route.calls.last.request)["metadata[stablecard_memo]"] == "Ada's travel card"


@respx.mock
async def test_creating_a_card_is_idempotent_under_a_derived_key(
    adapter: StripeIssuingAdapter,
) -> None:
    # A retried create must not leave a cardholder with two cards. Unlike Lithic,
    # Stripe honours `Idempotency-Key` on every POST, so the same trick works for
    # funding too (4d).
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )
    request = CreateCardRequest(currency="USD", spend_limit_minor=100_000, memo="one")

    await adapter.create_card(HOLDER_ID, request)
    await adapter.create_card(HOLDER_ID, request)
    await adapter.create_card(HOLDER_ID, CreateCardRequest(currency="USD", memo="two"))

    keys = [call.request.headers["idempotency-key"] for call in route.calls]
    assert keys[0] == keys[1], "the same logical card must reuse its key"
    assert keys[1] != keys[2], "a different request must not collide with it"


@respx.mock
async def test_a_card_response_never_carries_pan_or_cvc_into_the_ledger(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe returns `number` and `cvc` when they are expanded, and the card object
    # embeds the whole cardholder. `raw` is an allowlist for both reasons.
    payload = fixture("card_created")
    payload["number"] = "4242424242424242"
    payload["cvc"] = "911"
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    card = await adapter.get_card(CARD_ID)

    flattened = json.dumps(card.raw)
    for leaked in ("4242424242424242", "911", "Ada", "Lovelace", "Analytical Engine"):
        assert leaked not in flattened, f"{leaked!r} reached the ledger payload"
    assert card.raw["provider_status"] == "inactive"
    assert card.raw["brand"] == "Visa"


@respx.mock
async def test_a_cardholder_sent_as_a_bare_id_reads_the_same_as_an_expanded_one(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe expands `cardholder` on the card object but sends it as a string on
    # transactions and authorizations. Both have to resolve to the same opaque id.
    payload = fixture("card_created")
    payload["cardholder"] = HOLDER_ID
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert (await adapter.get_card(CARD_ID)).cardholder_id == HOLDER_ID


@respx.mock
async def test_an_unmappable_status_fails_loudly(adapter: StripeIssuingAdapter) -> None:
    # Calling an unknown state something else would be a claim about whether the
    # card can spend money.
    payload = fixture("card_created") | {"status": "quarantined"}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="quarantined"):
        await adapter.get_card(CARD_ID)


@respx.mock
async def test_a_card_missing_a_required_field_fails_loudly(
    adapter: StripeIssuingAdapter,
) -> None:
    payload = fixture("card_created")
    del payload["last4"]
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="last4"):
        await adapter.get_card(CARD_ID)


@respx.mock
async def test_an_unknown_card_is_a_typed_not_found(adapter: StripeIssuingAdapter) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(CardNotFoundError) as caught:
        await adapter.get_card("ic_nope")

    assert caught.value.card_id == "ic_nope"


@respx.mock
async def test_a_resource_missing_on_any_status_is_a_not_found(
    adapter: StripeIssuingAdapter,
) -> None:
    # Provider ids are opaque to us (SPEC.md §1), so we cannot check an id's shape
    # ourselves — and "not an id of theirs" and "no such card" are the same fact
    # whichever status they choose to say it with.
    respx.get(f"{BASE_URL}/issuing/cards/nope").mock(
        return_value=httpx.Response(400, json=fixture("error_resource_missing"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.get_card("nope")


@respx.mock
async def test_a_provider_failure_stays_a_provider_failure(
    adapter: StripeIssuingAdapter,
) -> None:
    # Not every error is a missing card: a 500 must not be reported as one.
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )

    with pytest.raises(StripeApiError):
        await adapter.get_card(CARD_ID)


@respx.mock
async def test_a_failed_creation_that_is_not_a_missing_cardholder_stays_itself(
    adapter: StripeIssuingAdapter,
) -> None:
    # "Issuing is not activated on this account" is not a naming mistake, and
    # reporting it as one would send whoever debugs it to the wrong place.
    respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "message": "Issuing is not enabled for this account.",
                    "type": "invalid_request_error",
                }
            },
        )
    )

    with pytest.raises(StripeApiError, match="not enabled"):
        await adapter.create_card(HOLDER_ID, CreateCardRequest())


@respx.mock
async def test_a_card_with_no_cardholder_at_all_fails_loudly(
    adapter: StripeIssuingAdapter,
) -> None:
    # Every card belongs to somebody; a card we cannot attribute is not one we can
    # ledger against a cardholder.
    payload = fixture("card_created") | {"cardholder": None}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="cardholder"):
        await adapter.get_card(CARD_ID)


@respx.mock
async def test_a_card_with_no_metadata_map_reads_as_never_activated(
    adapter: StripeIssuingAdapter,
) -> None:
    payload = fixture("card_created")
    del payload["metadata"]
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    card = await adapter.get_card(CARD_ID)

    assert card.state is CardState.UNACTIVATED
    assert card.raw["metadata"] == {}


@pytest.mark.parametrize(
    ("controls", "reason"),
    [
        (None, "no spending controls at all"),
        ({"spending_limits": None}, "limits that are not a list"),
        ({"spending_limits": ["not an object"]}, "a limit entry that is not an object"),
    ],
    ids=["absent", "not-a-list", "not-an-object"],
)
@respx.mock
async def test_an_unreadable_spending_control_reports_no_limit(
    adapter: StripeIssuingAdapter, controls: Any, reason: str
) -> None:
    # `None` means unlimited, which is what Stripe says by omission — so an
    # unreadable control reads the same way rather than inventing a number.
    payload = fixture("card_created") | {"spending_controls": controls}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert (await adapter.get_card(CARD_ID)).spend_limit_minor is None, reason


@respx.mock
async def test_a_limit_on_another_interval_is_not_reported_as_a_balance(
    adapter: StripeIssuingAdapter,
) -> None:
    # A monthly limit resets, so it is not a balance. Reporting it as one would let
    # `fund_card` raise a number that vanishes at the start of next month.
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_monthly_limit"))
    )

    assert (await adapter.get_card(CARD_ID)).spend_limit_minor is None


@respx.mock
async def test_a_non_integer_spending_limit_fails_loudly(
    adapter: StripeIssuingAdapter,
) -> None:
    # A plausible wrong balance funds the wrong amount, so this must not fall back
    # to "unlimited".
    payload = fixture("card_with_limit")
    payload["spending_controls"]["spending_limits"][0]["amount"] = "100000"
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="minor units"):
        await adapter.get_card(CARD_ID)


@respx.mock
async def test_an_unreadable_expiry_fails_loudly(adapter: StripeIssuingAdapter) -> None:
    payload = fixture("card_created") | {"exp_month": "August"}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="exp_month"):
        await adapter.get_card(CARD_ID)


@pytest.mark.parametrize(
    "created",
    [None, "1785000060", True, 10**20],
    ids=["absent", "string", "boolean", "out-of-range"],
)
@respx.mock
async def test_an_unreadable_timestamp_falls_back_to_our_clock(
    adapter: StripeIssuingAdapter, created: Any
) -> None:
    # Never a naive or invented datetime: every timestamp crossing the interface is
    # aware UTC (SPEC.md §1).
    payload = fixture("card_created") | {"created": created}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert (await adapter.get_card(CARD_ID)).created_at == datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


# --------------------------------------------------------------- lifecycle ----


@respx.mock
async def test_activating_a_card_records_that_it_has_ever_been_activated(
    adapter: StripeIssuingAdapter,
) -> None:
    # The bit Stripe does not keep. Without it, `inactive` cannot be told apart
    # from "created and never activated", and a frozen card would report itself
    # unactivated.
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_activated"))
    )

    card = await adapter.activate_card(CARD_ID)

    sent = sent_form(route.calls.last.request)
    assert sent["status"] == "active"
    assert sent[f"metadata[{ACTIVATED_AT_KEY}]"] == "2026-07-25T18:00:00+00:00"
    assert card.state is CardState.ACTIVE


@respx.mock
async def test_a_frozen_card_reports_frozen_not_unactivated(
    adapter: StripeIssuingAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_frozen"))
    )

    card = await adapter.freeze_card(CARD_ID)

    assert sent_form(route.calls.last.request) == {"status": "inactive"}
    assert card.state is CardState.FROZEN


@respx.mock
async def test_freezing_does_not_touch_metadata_so_the_marker_survives(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe only modifies metadata when the request carries metadata parameters,
    # so sending none is the safest way to leave the marker — and the funding
    # record — alone.
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_frozen"))
    )

    await adapter.freeze_card(CARD_ID)

    assert not [key for key in sent_form(route.calls.last.request) if key.startswith("metadata")]


@respx.mock
async def test_unfreezing_is_the_activate_path(adapter: StripeIssuingAdapter) -> None:
    # SPEC.md §9.1 is a toggle, and `base.py` says activate is the unfreeze path.
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_frozen"))
    )
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_activated"))
    )

    assert (await adapter.activate_card(CARD_ID)).state is CardState.ACTIVE


@respx.mock
async def test_activating_preserves_the_funding_record(
    adapter: StripeIssuingAdapter,
) -> None:
    # Insurance against an unverified assumption: if Stripe replaced the metadata
    # map rather than merging it, an unfreeze would erase the funding idempotency
    # record. Restating our own keys is correct under either behaviour, and costs
    # one read (docs/ARCHITECTURE.md §8.5).
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_funded"))
    )
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_activated"))
    )

    await adapter.activate_card(CARD_ID)

    sent = sent_form(route.calls.last.request)
    assert sent["metadata[stablecard_funding_ref]"] == "intent-1"
    assert sent["metadata[stablecard_funding_amount]"] == "2500"


@respx.mock
async def test_activating_keeps_no_metadata_that_is_not_ours(
    adapter: StripeIssuingAdapter,
) -> None:
    # Restating only our own namespace: someone else's key is not ours to rewrite,
    # and under merge semantics it is untouched either way.
    payload = fixture("card_created")
    payload["metadata"] = {"their_key": "their value"}
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_activated"))
    )

    await adapter.activate_card(CARD_ID)

    assert "metadata[their_key]" not in sent_form(route.calls.last.request)


@respx.mock
async def test_canceling_a_card_is_a_status_change(adapter: StripeIssuingAdapter) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_canceled"))
    )

    card = await adapter.cancel_card(CARD_ID)

    assert sent_form(route.calls.last.request) == {"status": "canceled"}
    assert card.state is CardState.CANCELED


@respx.mock
async def test_reviving_a_canceled_card_is_reported_as_an_illegal_transition(
    adapter: StripeIssuingAdapter,
) -> None:
    # Deliberately keyed on the card's *state*, read back, rather than on Stripe's
    # message or error code: their exact wording for this is not something this
    # suite can verify, and a caller mistake should be a 409 rather than a 502.
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(400, json=fixture("error_card_canceled"))
    )
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_canceled"))
    )

    with pytest.raises(IllegalCardTransitionError) as caught:
        await adapter.activate_card(CARD_ID)

    assert caught.value.from_state is CardState.CANCELED
    assert caught.value.to_state is CardState.ACTIVE


@respx.mock
async def test_a_rejected_transition_on_a_live_card_reports_stripes_own_error(
    adapter: StripeIssuingAdapter,
) -> None:
    # If the read-back shows a state the transition is legal from, we do not know
    # better than Stripe why they refused, so their error stands.
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "Cardholder requirements are not satisfied.",
                    "type": "invalid_request_error",
                }
            },
        )
    )
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    with pytest.raises(StripeApiError, match="requirements"):
        await adapter.activate_card(CARD_ID)


@respx.mock
async def test_a_transition_on_a_card_that_cannot_be_read_back_reports_the_original(
    adapter: StripeIssuingAdapter,
) -> None:
    # No idea what state it is in, so do not invent one.
    respx.post(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(400, json=fixture("error_card_canceled"))
    )
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(StripeApiError, match="canceled"):
        await adapter.freeze_card("ic_nope")


@respx.mock
async def test_a_five_hundred_on_a_transition_is_not_a_transition_problem(
    adapter: StripeIssuingAdapter,
) -> None:
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )

    with pytest.raises(StripeApiError, match="boom"):
        await adapter.freeze_card(CARD_ID)


# ------------------------------------------------------------------ factory ----


def test_the_adapter_is_registered_by_importing_the_package() -> None:
    # The whole claim, from the outside: the app imports `app.issuers` and a third
    # provider resolves. The only production line phase 4 added outside its own
    # package is the `register()` call that makes this true.
    assert PROVIDER_ID in registry.known_providers()


def test_the_registry_resolves_it_through_the_interface_alone() -> None:
    resolved = registry.get_adapter(PROVIDER_ID)

    assert isinstance(resolved, CardIssuerAdapter)
    assert resolved.provider_id == PROVIDER_ID


def test_the_taxonomy_now_has_two_real_fiat_rails_and_one_crypto_deposit() -> None:
    # SPEC.md §3.2's point: both funding models are covered, and `GET /providers`
    # gained this row without a change in `api/` (docs/ARCHITECTURE.md §8.6).
    described = dict(registry.describe())

    assert described[PROVIDER_ID] is FundingModel.FIAT_RAIL
    assert described["lithic"] is FundingModel.FIAT_RAIL
    assert described["gnosis_pay_mock"] is FundingModel.CRYPTO_DEPOSIT


def test_describing_the_providers_does_not_need_stripe_credentials() -> None:
    # `describe()` builds every registered adapter. Lithic's client validates its key
    # in the constructor, so that call already depends on LITHIC_API_KEY being set
    # (reported, not changed — it is phase 3's behaviour). This adapter must not add
    # a second such dependency, or `GET /providers` would need two credentials to
    # answer at all.
    registry.reset_instances()

    assert PROVIDER_ID in dict(registry.describe())


def test_the_adapter_builds_from_its_own_settings_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `registry.describe()` builds every adapter to report its funding model, and
    # `GET /providers` calls it. An adapter that refused to exist without a key
    # would take that route down for the providers that are configured.
    from app.issuers.stripe_issuing import adapter as module
    from app.issuers.stripe_issuing.config import StripeIssuingSettings

    monkeypatch.setattr(
        module,
        "get_stripe_issuing_settings",
        lambda: StripeIssuingSettings(_env_file=None),  # type: ignore[call-arg]
    )

    built = StripeIssuingAdapter.from_settings()

    assert built.provider_id == PROVIDER_ID
    assert built.funding_model is FundingModel.FIAT_RAIL
