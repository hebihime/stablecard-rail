"""Funding and balance at Lithic (SPEC.md §3.1, §5, §10).

Lithic's ledger APIs — financial accounts, book transfers, management operations —
are not available to this sandbox program: `POST /v1/financial_accounts` answers 403
and `GET /v1/balances?account_token=…` answers 400 "Account does not support this
operation". What the program *does* expose per card is a spend limit. So for this
adapter:

* **funding** raises the card's `spend_limit` by the amount, over a `FOREVER`
  window, and records the funding ref in the card's `memo`;
* **balance** is that limit minus everything the card has spent, computed from the
  card's own transaction list.

The interesting property is idempotency. Lithic offers no idempotent funding
primitive here (`Idempotency-Key` covers card creation, not `PATCH`), so the memo
tag *is* the idempotency record — the provider holds it, not us. These tests pin
both what that buys and what it does not: an immediately retried funding is applied
once, and a ref replayed with different terms is refused rather than quietly
re-applied. What it cannot do is remember an *older* ref once a newer one has landed
(docs/ARCHITECTURE.md §4.5).

Every provider response here is a fixture recorded from the sandbox; the memo tag is
our own convention, so tagged variants are built from those recordings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from app.issuers.lithic import LithicAdapter
from app.issuers.lithic.client import LithicClient

BASE_URL = "https://sandbox.lithic.test/v1"
API_KEY = "test-sandbox-key-not-a-real-credential"

FIXTURES = Path(__file__).parent / "fixtures" / "lithic"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


CARD = fixture("card_created")
CARD_TOKEN = CARD["token"]
BASE_MEMO = CARD["memo"]
NO_TRANSACTIONS: dict[str, Any] = {"data": [], "has_more": False}


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def adapter() -> LithicAdapter:
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0, sleep=no_sleep)
    return LithicAdapter(client=client, clock=lambda: datetime(2026, 7, 25, 12, 0, tzinfo=UTC))


def card_with(**overrides: Any) -> dict[str, Any]:
    return {**CARD, **overrides}


def mock_card(card: dict[str, Any]) -> respx.Route:
    return respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json=card)
    )


def mock_patch(card: dict[str, Any]) -> respx.Route:
    return respx.patch(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json=card)
    )


# ----------------------------------------------------------------- funding ----


@respx.mock
async def test_funding_raises_the_spend_limit_by_the_amount(adapter: LithicAdapter) -> None:
    mock_card(card_with(spend_limit=50_000))
    patched = mock_patch(fixture("card_funded"))

    result = await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    sent = json.loads(patched.calls.last.request.content)
    assert 75_000 == sent["spend_limit"]
    # Always restated: a limit on a MONTHLY or TRANSACTION window is not a balance.
    assert "FOREVER" == sent["spend_limit_duration"]

    assert FundingStatus.SUCCEEDED is result.status
    assert Money(25_000, "USD") == result.amount
    assert "fi_0001" == result.funding_ref
    assert CARD_TOKEN == result.card_id
    # There is no provider-side funding object to point at, so the reference is the
    # provider-side *result*: this card at this limit. That is what reconciliation
    # can actually check.
    assert f"{CARD_TOKEN}:75000" == result.issuer_funding_ref
    assert 50_000 == result.raw["spend_limit_before"]
    assert 75_000 == result.raw["spend_limit_after"]


@respx.mock
async def test_the_funding_ref_is_recorded_on_the_card_beside_its_memo(
    adapter: LithicAdapter,
) -> None:
    # The tag has to live somewhere the provider keeps, or a retry after a crash has
    # nothing to read. `memo` is the only writable free-text field a card has.
    mock_card(card_with(spend_limit=50_000))
    patched = mock_patch(fixture("card_funded"))

    await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    memo = json.loads(patched.calls.last.request.content)["memo"]
    assert memo.startswith(BASE_MEMO), "the cardholder's own label survives"
    assert "fi_0001" in memo
    assert "25000" in memo, "the amount too, so a replay with other terms is visible"


@respx.mock
async def test_funding_twice_with_the_same_ref_funds_once(adapter: LithicAdapter) -> None:
    # The contract every adapter owes the funding engine (SPEC.md §10): the engine
    # retries without knowing whether the first call landed.
    already = card_with(spend_limit=75_000, memo=f"{BASE_MEMO} [fund:fi_0001:25000]")
    mock_card(already)
    patched = mock_patch(already)

    result = await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    assert 0 == patched.call_count, "nothing was changed the second time"
    assert FundingStatus.SUCCEEDED is result.status
    assert Money(25_000, "USD") == result.amount
    assert f"{CARD_TOKEN}:75000" == result.issuer_funding_ref
    assert result.raw["replayed"] is True


@respx.mock
async def test_the_same_ref_with_a_different_amount_is_refused(
    adapter: LithicAdapter,
) -> None:
    # Either the caller has a bug or two different fundings share a ref. Applying
    # the second silently would double-fund the card.
    mock_card(card_with(spend_limit=75_000, memo=f"{BASE_MEMO} [fund:fi_0001:25000]"))
    patched = mock_patch(CARD)

    with pytest.raises(FundingRejectedError, match="fi_0001"):
        await adapter.fund_card(CARD_TOKEN, Money(30_000, "USD"), "fi_0001")

    assert 0 == patched.call_count


@respx.mock
async def test_a_second_funding_stacks_and_replaces_the_tag(adapter: LithicAdapter) -> None:
    # Tags must not accumulate: `memo` is a display field, not a log, and it is
    # length-limited.
    mock_card(card_with(spend_limit=75_000, memo=f"{BASE_MEMO} [fund:fi_0001:25000]"))
    patched = mock_patch(card_with(spend_limit=100_000))

    await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0002")

    sent = json.loads(patched.calls.last.request.content)
    assert 100_000 == sent["spend_limit"]
    assert "fi_0001" not in sent["memo"]
    assert "[fund:fi_0002:25000]" in sent["memo"]
    assert sent["memo"].startswith(BASE_MEMO)


@respx.mock
async def test_funding_a_card_with_no_memo_of_its_own_still_works(
    adapter: LithicAdapter,
) -> None:
    mock_card(card_with(spend_limit=50_000, memo=""))
    patched = mock_patch(fixture("card_funded"))

    await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    assert "[fund:fi_0001:25000]" == json.loads(patched.calls.last.request.content)["memo"]


@respx.mock
async def test_a_long_cardholder_memo_is_truncated_rather_than_dropping_the_tag(
    adapter: LithicAdapter,
) -> None:
    # If something has to give, it is the label, not the idempotency record.
    mock_card(card_with(spend_limit=50_000, memo="x" * 400))
    patched = mock_patch(fixture("card_funded"))

    await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    memo = json.loads(patched.calls.last.request.content)["memo"]
    assert len(memo) <= 128
    assert memo.endswith("[fund:fi_0001:25000]")


@pytest.mark.parametrize("amount", [Money(0, "USD"), Money(-100, "USD")])
@respx.mock
async def test_funding_a_card_with_nothing_is_refused(
    adapter: LithicAdapter, amount: Money
) -> None:
    mock_card(CARD)

    with pytest.raises(FundingRejectedError):
        await adapter.fund_card(CARD_TOKEN, amount, "fi_0001")


@respx.mock
async def test_funding_in_the_wrong_currency_is_refused(adapter: LithicAdapter) -> None:
    # A spend limit is denominated in the card's own currency. Adding 25000 "EUR" to
    # a USD limit would be a silent currency conversion at parity.
    mock_card(card_with(cardholder_currency="USD"))

    with pytest.raises(FundingRejectedError, match="EUR"):
        await adapter.fund_card(CARD_TOKEN, Money(25_000, "EUR"), "fi_0001")


@respx.mock
async def test_funding_a_closed_card_is_refused_before_it_is_attempted(
    adapter: LithicAdapter,
) -> None:
    mock_card(fixture("card_closed"))
    patched = mock_patch(fixture("card_closed"))

    with pytest.raises(FundingRejectedError, match="canceled"):
        await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")

    assert 0 == patched.call_count


@respx.mock
async def test_funding_a_card_with_no_limit_at_all_is_refused(
    adapter: LithicAdapter,
) -> None:
    # `spend_limit: 0` is Lithic for *unlimited*. Raising it by 25000 would replace
    # an unlimited card with a limited one — a reduction dressed up as funding.
    mock_card(card_with(spend_limit=0))

    with pytest.raises(FundingRejectedError, match="unlimited"):
        await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")


@respx.mock
async def test_funding_a_card_that_does_not_exist_is_not_found(
    adapter: LithicAdapter,
) -> None:
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(404, json=fixture("error_card_not_found"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.fund_card(CARD_TOKEN, Money(25_000, "USD"), "fi_0001")


# ----------------------------------------------------------------- balance ----


@respx.mock
async def test_the_balance_is_the_limit_minus_what_the_card_has_spent(
    adapter: LithicAdapter,
) -> None:
    # The arithmetic against a real recorded set: a 75000 limit, one settled 1234
    # purchase, one 250 refund, one voided authorization and one decline.
    mock_card(fixture("card_funded"))
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json=fixture("transactions_all"))
    )

    assert Money(74_016, "USD") == await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_a_declined_or_voided_authorization_does_not_hold_funds(
    adapter: LithicAdapter,
) -> None:
    # Both are in the recorded set above; this states the property the number proves.
    recorded = fixture("transactions_all")
    statuses = {t["status"] for t in recorded["data"]}
    assert {"DECLINED", "VOIDED"} <= statuses, "the fixture must contain both"

    mock_card(fixture("card_funded"))
    respx.get(f"{BASE_URL}/transactions").mock(return_value=httpx.Response(200, json=recorded))

    spent = 75_000 - (await adapter.get_balance(CARD_TOKEN)).amount_minor
    assert 1_234 - 250 == spent, "only the settled purchase and the refund moved money"


@respx.mock
async def test_a_pending_authorization_holds_funds(adapter: LithicAdapter) -> None:
    # An authorization that has not cleared is still money the cardholder cannot
    # spend twice. Counting only settled amounts is how a card gets overdrawn.
    mock_card(card_with(spend_limit=50_000))
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(
            200, json={"data": [fixture("transaction_pending")], "has_more": False}
        )
    )

    assert Money(50_000 - 1_234, "USD") == await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_the_balance_of_an_unused_card_is_its_whole_limit(
    adapter: LithicAdapter,
) -> None:
    mock_card(card_with(spend_limit=50_000))
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json=NO_TRANSACTIONS)
    )

    assert Money(50_000, "USD") == await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_the_balance_reads_every_page_of_transactions(adapter: LithicAdapter) -> None:
    # A balance from the first page only would be right until a card had spent more
    # than a hundred times — which is exactly when being wrong matters.
    mock_card(fixture("card_funded"))
    route = respx.get(f"{BASE_URL}/transactions").mock(
        side_effect=[
            httpx.Response(200, json=fixture("transactions_page_1")),
            httpx.Response(200, json=fixture("transactions_page_2")),
        ]
    )

    balance = await adapter.get_balance(CARD_TOKEN)

    assert 2 == route.call_count
    assert Money(74_016, "USD") == balance, "same total as the single-page read"
    assert CARD_TOKEN == route.calls[0].request.url.params["card_token"]


@respx.mock
async def test_the_balance_is_denominated_in_the_cards_currency(
    adapter: LithicAdapter,
) -> None:
    mock_card(card_with(spend_limit=50_000, cardholder_currency="GBP"))
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json=NO_TRANSACTIONS)
    )

    assert "GBP" == (await adapter.get_balance(CARD_TOKEN)).currency


@respx.mock
async def test_a_card_with_no_limit_has_no_balance_to_report(
    adapter: LithicAdapter,
) -> None:
    # Refuse rather than answer 0: an unlimited card's available balance is not zero,
    # and reporting it as such would stop a funding engine from ever topping up.
    mock_card(card_with(spend_limit=0))

    with pytest.raises(IssuerError, match="unlimited"):
        await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_a_transaction_whose_amounts_we_cannot_read_fails_the_balance(
    adapter: LithicAdapter,
) -> None:
    # A balance that silently skips a transaction is worse than no balance: it is a
    # number that looks right and funds the wrong amount.
    mock_card(card_with(spend_limit=50_000))
    broken = {**fixture("transaction_pending"), "amounts": {"hold": {"amount": "lots"}}}
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"data": [broken], "has_more": False})
    )

    with pytest.raises(IssuerError, match=broken["token"]):
        await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_a_transaction_with_no_amounts_at_all_fails_the_balance(
    adapter: LithicAdapter,
) -> None:
    mock_card(card_with(spend_limit=50_000))
    broken = {"token": "txn_shapeless", "status": "PENDING"}
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"data": [broken], "has_more": False})
    )

    with pytest.raises(IssuerError, match="txn_shapeless"):
        await adapter.get_balance(CARD_TOKEN)


@respx.mock
async def test_a_balance_can_go_negative_if_the_limit_was_lowered_under_the_spend(
    adapter: LithicAdapter,
) -> None:
    # Clamping at zero would hide a card that is over its limit, which is a
    # reconciliation signal, not a display problem.
    mock_card(card_with(spend_limit=1_000))
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(
            200, json={"data": [fixture("transaction_settled")], "has_more": False}
        )
    )

    assert Money(1_000 - 1_234, "USD") == await adapter.get_balance(CARD_TOKEN)
