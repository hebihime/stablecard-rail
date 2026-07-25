"""Funding and balance at Stripe Issuing (SPEC.md §5.2 step 3, §10, phase 4).

The contract every adapter owes the funding engine is the one that matters here:
**calling `fund_card` twice with the same `funding_ref` funds once and returns the
same result**, because the engine may retry without knowing whether the first call
landed. Stripe and Lithic keep that promise by different means, and the point of
this file is that the engine cannot tell.

Where Stripe lands in the same place as Lithic:

* There is no per-card balance to move money into. Cards spend from the
  *account's* Issuing balance, so funding is a raise of the card's own `all_time`
  spending limit, and `get_balance` is a figure we derive rather than one Stripe
  hands us.

Where it is better, and why that is worth recording:

* `Idempotency-Key` works on every POST, not just creation — so a retry inside
  Stripe's 24-hour window replays *their* record of the funding rather than
  relying on ours. The metadata marker is the backstop for a retry after that.
* "Unlimited" is the absence of a limit, not a zero, so a card can start at zero
  and be funded up. At Lithic a zero limit means unlimited and funding cannot
  start from nothing (docs/ARCHITECTURE.md §4.4, §8.4).
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

from app.core.money import Money
from app.issuers.base import (
    CardNotFoundError,
    FundingRejectedError,
    FundingStatus,
    IssuerError,
)
from app.issuers.stripe_issuing.adapter import (
    ALL_TIME,
    FUNDING_AMOUNT_KEY,
    FUNDING_REF_KEY,
    PROVIDER_ID,
    StripeIssuingAdapter,
)
from app.issuers.stripe_issuing.client import StripeClient

BASE_URL = "https://api.stripe.test/v1"
CARD_ID = "ic_1SbCardStablecard0001"
FUNDING_REF = "intent-1"

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_issuing"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def adapter() -> StripeIssuingAdapter:
    return StripeIssuingAdapter(
        client=StripeClient(
            base_url=BASE_URL, api_key="sk_test_funding", timeout=5.0, sleep=no_sleep
        ),
        clock=lambda: datetime(2026, 7, 25, 18, 0, tzinfo=UTC),
    )


def sent_form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def reads(name: str) -> respx.Route:
    return respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture(name))
    )


def writes(name: str) -> respx.Route:
    return respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=fixture(name))
    )


# ----------------------------------------------------------------- funding ----


@respx.mock
async def test_funding_raises_the_all_time_limit_by_the_amount(
    adapter: StripeIssuingAdapter,
) -> None:
    reads("card_with_limit")
    route = writes("card_funded")

    result = await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    sent = sent_form(route.calls.last.request)
    # 100_000 already on the card, plus 2_500.
    assert sent["spending_controls[spending_limits][0][amount]"] == "102500"
    # Restated every time: a limit on any other interval is not a balance.
    assert sent["spending_controls[spending_limits][0][interval]"] == ALL_TIME
    assert sent["spending_controls[spending_limits_currency]"] == "usd"

    assert result.provider_id == PROVIDER_ID
    assert result.card_id == CARD_ID
    assert result.funding_ref == FUNDING_REF
    assert result.status is FundingStatus.SUCCEEDED
    assert result.amount == Money(2500, "USD")
    # No provider-side funding object to name, so the reference is the provider-side
    # *result*: this card at this limit. That is what a reconciler can go and check.
    assert result.issuer_funding_ref == f"{CARD_ID}:102500"
    assert result.raw["spending_limit_before"] == 100_000
    assert result.raw["spending_limit_after"] == 102_500


@respx.mock
async def test_the_funding_ref_is_recorded_on_the_card_itself(
    adapter: StripeIssuingAdapter,
) -> None:
    # The provider holds the record, which is what makes it survive our crashing
    # mid-call — the same reason Lithic's ref goes in the card memo (§4.5).
    reads("card_with_limit")
    route = writes("card_funded")

    await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    sent = sent_form(route.calls.last.request)
    assert sent[f"metadata[{FUNDING_REF_KEY}]"] == FUNDING_REF
    assert sent[f"metadata[{FUNDING_AMOUNT_KEY}]"] == "2500"


@respx.mock
async def test_funding_restates_the_activation_marker(
    adapter: StripeIssuingAdapter,
) -> None:
    # Same insurance as `activate_card`: if Stripe replaced the metadata map rather
    # than merging it, funding would erase the record that this card was ever
    # activated and a frozen card would start reporting itself unactivated.
    reads("card_with_limit")
    route = writes("card_funded")

    await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert "metadata[stablecard_activated_at]" in sent_form(route.calls.last.request)


@respx.mock
async def test_funding_carries_an_idempotency_key_derived_from_the_ref(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe honours the header on every POST, unlike Lithic — so a retry inside
    # their 24-hour window replays their record of the funding rather than depending
    # on ours having been written.
    reads("card_with_limit")
    route = writes("card_funded")

    await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)
    first = route.calls.last.request.headers["idempotency-key"]

    await adapter.fund_card(CARD_ID, Money(2500, "USD"), "intent-2")
    second = route.calls.last.request.headers["idempotency-key"]

    assert first and first != second


@respx.mock
async def test_funding_a_card_that_starts_at_zero_works(
    adapter: StripeIssuingAdapter,
) -> None:
    # The Lithic footgun that does not recur here: `0` is a real limit, not
    # "unlimited", so a card can be created unable to spend and funded up.
    reads("card_limited_to_zero")
    route = writes("card_funded")

    result = await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert sent_form(route.calls.last.request)["spending_controls[spending_limits][0][amount]"] == (
        "2500"
    )
    assert result.raw["spending_limit_before"] == 0


# ------------------------------------------------------------- idempotency ----


@respx.mock
async def test_replaying_the_same_funding_ref_funds_once(
    adapter: StripeIssuingAdapter,
) -> None:
    # The contract from SPEC.md §10. The card already carries this ref, so the
    # second call must not raise the limit again.
    reads("card_funded")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    result = await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert route.call_count == 0, "a replay must not write"
    assert result.status is FundingStatus.SUCCEEDED
    assert result.amount == Money(2500, "USD")
    assert result.issuer_funding_ref == f"{CARD_ID}:102500"
    assert result.raw["replayed"] is True


@respx.mock
async def test_a_replay_with_a_different_amount_is_rejected_rather_than_applied(
    adapter: StripeIssuingAdapter,
) -> None:
    # Two different amounts under one idempotency key is a caller bug, and funding
    # the difference would be the worst possible way to resolve it.
    reads("card_funded")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="2500"):
        await adapter.fund_card(CARD_ID, Money(9999, "USD"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_a_second_distinct_funding_replaces_the_marker(
    adapter: StripeIssuingAdapter,
) -> None:
    # One marker slot, so the card remembers only the most recent ref — the same
    # limitation Lithic's memo tag has, kept identical on purpose: the funding
    # engine advances one intent at a time, and both adapters therefore offer it the
    # same guarantee (docs/ARCHITECTURE.md §8.4).
    reads("card_funded")
    route = writes("card_funded")

    await adapter.fund_card(CARD_ID, Money(1000, "USD"), "intent-2")

    sent = sent_form(route.calls.last.request)
    assert sent[f"metadata[{FUNDING_REF_KEY}]"] == "intent-2"
    assert sent[f"metadata[{FUNDING_AMOUNT_KEY}]"] == "1000"
    assert sent["spending_controls[spending_limits][0][amount]"] == "103500"


@respx.mock
async def test_an_unreadable_recorded_amount_is_refused_rather_than_guessed_at(
    adapter: StripeIssuingAdapter,
) -> None:
    # If the marker cannot be read, we do not know whether this ref was applied.
    # Funding again might double it; assuming it landed might skip it. Refusing is
    # the only answer that cannot lose money silently.
    payload = fixture("card_funded")
    payload["metadata"][FUNDING_AMOUNT_KEY] = "not-a-number"
    respx.get(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="not-a-number"):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert route.call_count == 0


# -------------------------------------------------------------- rejections ----


@pytest.mark.parametrize("amount_minor", [0, -100])
@respx.mock
async def test_a_non_positive_amount_is_refused(
    adapter: StripeIssuingAdapter, amount_minor: int
) -> None:
    reads("card_with_limit")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="positive"):
        await adapter.fund_card(CARD_ID, Money(amount_minor, "USD"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_funding_in_the_wrong_currency_is_refused(
    adapter: StripeIssuingAdapter,
) -> None:
    # Adding 2500 EUR to a USD limit would be a silent conversion at parity.
    reads("card_with_limit")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="USD"):
        await adapter.fund_card(CARD_ID, Money(2500, "EUR"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_funding_a_canceled_card_is_refused(adapter: StripeIssuingAdapter) -> None:
    reads("card_canceled")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="canceled"):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_funding_an_unlimited_card_is_refused(adapter: StripeIssuingAdapter) -> None:
    # Raising the limit of a card that has none would replace "spends freely" with
    # "spends 2500", which is a reduction dressed up as a top-up.
    reads("card_unlimited")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match="unlimited"):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_funding_a_card_limited_on_the_wrong_interval_is_refused(
    adapter: StripeIssuingAdapter,
) -> None:
    # A monthly limit resets, so a funding raised into it disappears at the start of
    # the month. Reported as unlimited because that is what it is for our purposes:
    # there is no `all_time` limit to raise.
    reads("card_monthly_limit")
    route = respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}")

    with pytest.raises(FundingRejectedError, match=ALL_TIME):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)

    assert route.call_count == 0


@respx.mock
async def test_funding_an_unknown_card_is_a_typed_not_found(
    adapter: StripeIssuingAdapter,
) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.fund_card("ic_nope", Money(2500, "USD"), FUNDING_REF)


@respx.mock
async def test_a_card_deleted_between_the_read_and_the_raise_is_a_not_found(
    adapter: StripeIssuingAdapter,
) -> None:
    # Two calls, so the card can stop existing between them.
    reads("card_with_limit")
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)


@respx.mock
async def test_a_failed_raise_stays_a_provider_failure(
    adapter: StripeIssuingAdapter,
) -> None:
    # The funding engine has to be able to tell "try again" from "this card is
    # wrong": a 502 out of the API means retry, a 409 means fix the request.
    reads("card_with_limit")
    respx.post(f"{BASE_URL}/issuing/cards/{CARD_ID}").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )

    with pytest.raises(IssuerError, match="boom"):
        await adapter.fund_card(CARD_ID, Money(2500, "USD"), FUNDING_REF)


# ----------------------------------------------------------------- balance ----


def transactions(*pages: str) -> respx.Route:
    return respx.get(f"{BASE_URL}/issuing/transactions").mock(
        side_effect=[httpx.Response(200, json=fixture(page)) for page in pages]
    )


def authorizations(name: str) -> respx.Route:
    return respx.get(f"{BASE_URL}/issuing/authorizations").mock(
        return_value=httpx.Response(200, json=fixture(name))
    )


@respx.mock
async def test_the_balance_is_the_limit_less_what_is_spent_and_held(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe exposes no per-card balance, so this is derived — exactly as Lithic's
    # is. Captures are negative and refunds positive on their side, so transactions
    # sum straight in; a pending authorization is a positive hold and comes off.
    reads("card_with_limit")
    transactions("transactions_page_1", "transactions_page_2")
    authorizations("authorizations_pending")

    # 100_000 limit + 250 refund - 1_234 capture - 500 capture - 3_000 held.
    assert await adapter.get_balance(CARD_ID) == Money(95_516, "USD")


@respx.mock
async def test_the_balance_reads_every_page(adapter: StripeIssuingAdapter) -> None:
    # A balance computed from the first page only would be right until a card had
    # more than a hundred transactions, and then quietly too high.
    reads("card_with_limit")
    route = transactions("transactions_page_1", "transactions_page_2")
    authorizations("authorizations_none")

    await adapter.get_balance(CARD_ID)

    assert route.call_count == 2
    assert route.calls[1].request.url.params["starting_after"] == "ipi_1SbTxnStablecard0001"


@respx.mock
async def test_only_pending_authorizations_are_asked_for(
    adapter: StripeIssuingAdapter,
) -> None:
    # A closed authorization has either become a transaction or released its hold,
    # so counting one would double it. Filtering server-side also keeps a busy
    # card's history off the wire.
    reads("card_with_limit")
    transactions("transactions_page_2")
    route = authorizations("authorizations_none")

    await adapter.get_balance(CARD_ID)

    params = route.calls.last.request.url.params
    assert params["card"] == CARD_ID
    assert params["status"] == "pending"


@respx.mock
async def test_an_unapproved_pending_authorization_holds_nothing(
    adapter: StripeIssuingAdapter,
) -> None:
    # Declined money is not held money, and treating it as held would understate the
    # balance and refuse a top-up the card could take.
    payload = fixture("authorizations_pending")
    payload["data"][0]["approved"] = False
    reads("card_with_limit")
    transactions("transactions_page_2")
    respx.get(f"{BASE_URL}/issuing/authorizations").mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert await adapter.get_balance(CARD_ID) == Money(99_500, "USD")


@respx.mock
async def test_a_card_with_no_limit_has_no_balance_to_report(
    adapter: StripeIssuingAdapter,
) -> None:
    reads("card_unlimited")

    with pytest.raises(IssuerError, match="unlimited"):
        await adapter.get_balance(CARD_ID)


@respx.mock
async def test_a_transaction_with_a_non_integer_amount_fails_loudly(
    adapter: StripeIssuingAdapter,
) -> None:
    # A plausible wrong balance funds the wrong amount, so a transaction we cannot
    # read must not silently contribute zero.
    payload = fixture("transactions_page_2")
    payload["data"][0]["amount"] = "-500"
    reads("card_with_limit")
    respx.get(f"{BASE_URL}/issuing/transactions").mock(
        return_value=httpx.Response(200, json=payload)
    )
    authorizations("authorizations_none")

    with pytest.raises(IssuerError, match="ipi_1SbTxnStablecard0000"):
        await adapter.get_balance(CARD_ID)


@respx.mock
async def test_an_authorization_with_a_non_integer_amount_fails_loudly(
    adapter: StripeIssuingAdapter,
) -> None:
    payload = fixture("authorizations_pending")
    payload["data"][0]["amount"] = None
    reads("card_with_limit")
    transactions("transactions_page_2")
    respx.get(f"{BASE_URL}/issuing/authorizations").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(IssuerError, match="iauth_1SbAuthStablecard003"):
        await adapter.get_balance(CARD_ID)


@respx.mock
async def test_the_balance_is_denominated_in_the_cards_currency(
    adapter: StripeIssuingAdapter,
) -> None:
    # Stripe's currency codes are lower-case and `Money`'s are not.
    reads("card_with_limit")
    transactions("transactions_page_2")
    authorizations("authorizations_none")

    balance = await adapter.get_balance(CARD_ID)

    assert balance.currency == "USD"
    assert balance.amount_minor == 99_500
