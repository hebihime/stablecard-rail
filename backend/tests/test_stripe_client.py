"""The Stripe HTTP client (SPEC.md §3.2, §10, phase 4).

Nothing here touches the network. The response bodies are the documented shapes
(see `tests/fixtures/stripe_issuing/README.md` — hand-authored, not recorded,
which is weaker evidence than Lithic's and is why the client is strict about what
it requires).

The client's job is the same boring job Lithic's has: authenticate, retry what a
retry can fix, paginate to the end, and turn a provider error into one of
`issuers/base.py`'s. What is genuinely different — and therefore what most of
these tests are about — is that **Stripe's API is form-encoded, not JSON**, with
its own bracket syntax for nested values. A JSON body would be accepted with a
200 and every field ignored, which is the worst possible failure mode: a card
created with none of the controls you asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from app.issuers.base import IssuerError
from app.issuers.stripe_issuing.client import (
    DEFAULT_PAGE_SIZE,
    RETRYABLE_STATUSES,
    StripeApiError,
    StripeClient,
    checked_api_key,
    form_encode,
)

BASE_URL = "https://api.stripe.test/v1"
API_KEY = "sk_test_not_a_real_credential_0001"

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_issuing"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


#: Read out of the recorded fixtures, so re-running the recorder cannot break these
#: tests on ids alone. Stripe's ids are opaque to us anyway (SPEC.md §1).
CARD_ID: str = fixture("card_created")["id"]


class Sleeps:
    """Stands in for `asyncio.sleep`, so a retry test costs nothing to run."""

    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def make_client(sleeps: Sleeps, *, api_key: str = API_KEY, api_version: str = "") -> StripeClient:
    return StripeClient(
        base_url=BASE_URL,
        api_key=api_key,
        timeout=5.0,
        api_version=api_version,
        sleep=sleeps,
    )


def sent_form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


# ------------------------------------------------------------ form encoding ----
# Stripe takes `application/x-www-form-urlencoded` and expresses nesting with
# brackets. Every one of these is a shape the adapter actually sends.


def test_flat_values_encode_as_themselves() -> None:
    assert form_encode({"currency": "usd", "type": "virtual"}) == {
        "currency": "usd",
        "type": "virtual",
    }


def test_integers_are_stringified_without_becoming_floats() -> None:
    # Money is integer minor units everywhere in this system, and `100000.0` in a
    # form body is a different amount request than `100000`.
    assert form_encode({"amount": 100_000}) == {"amount": "100000"}


def test_booleans_use_stripes_spelling() -> None:
    assert form_encode({"a": True, "b": False}) == {"a": "true", "b": "false"}


def test_nested_objects_use_bracket_syntax() -> None:
    assert form_encode({"billing": {"address": {"city": "New York"}}}) == {
        "billing[address][city]": "New York"
    }


def test_lists_are_indexed() -> None:
    encoded = form_encode(
        {"spending_controls": {"spending_limits": [{"amount": 5000, "interval": "all_time"}]}}
    )
    assert encoded == {
        "spending_controls[spending_limits][0][amount]": "5000",
        "spending_controls[spending_limits][0][interval]": "all_time",
    }


def test_a_second_list_entry_gets_its_own_index() -> None:
    encoded = form_encode({"limits": [{"amount": 1}, {"amount": 2}]})
    assert encoded == {"limits[0][amount]": "1", "limits[1][amount]": "2"}


def test_a_list_of_scalars_is_indexed_too() -> None:
    assert form_encode({"expand": ["cardholder", "card"]}) == {
        "expand[0]": "cardholder",
        "expand[1]": "card",
    }


def test_none_encodes_as_the_empty_string_because_that_is_how_stripe_unsets() -> None:
    # Load-bearing: clearing a metadata key is how the adapter would ever remove a
    # marker it wrote, and `metadata[x]=` is the documented way to do it. Dropping
    # the key instead would silently leave the old value in place.
    assert form_encode({"metadata": {"stale": None}}) == {"metadata[stale]": ""}


def test_an_empty_object_contributes_nothing() -> None:
    assert form_encode({"metadata": {}}) == {}
    assert form_encode({}) == {}


def test_encoding_refuses_a_float_rather_than_rounding_it() -> None:
    # A float reaching a money field is a bug upstream, and `str(12.99)` would ship
    # it to Stripe as a plausible-looking amount.
    with pytest.raises(IssuerError, match="float"):
        form_encode({"amount": 12.99})


# ---------------------------------------------------------- authentication ----


@respx.mock
async def test_the_api_key_is_a_bearer_token(sleeps: Sleeps) -> None:
    route = respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await make_client(sleeps).get("/issuing/cards/ic_1")

    sent = route.calls.last.request
    # Unlike Lithic's bare `Authorization: <key>`.
    assert f"Bearer {API_KEY}" == sent.headers["authorization"]


@respx.mock
async def test_the_api_version_is_sent_only_when_pinned(sleeps: Sleeps) -> None:
    route = respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await make_client(sleeps).get("/issuing/cards/ic_1")
    await make_client(sleeps, api_version="2025-03-31.basil").get("/issuing/cards/ic_1")

    unpinned, pinned = route.calls[0].request, route.calls[1].request
    assert "stripe-version" not in unpinned.headers
    assert "2025-03-31.basil" == pinned.headers["stripe-version"]


@respx.mock
async def test_an_empty_key_is_refused_by_name_at_first_use(sleeps: Sleeps) -> None:
    # Building the client must stay free of credentials: `registry.describe()`
    # constructs every adapter to report its funding model. So the check lives on
    # the request path, and names the variable rather than the symptom.
    route = respx.get(f"{BASE_URL}/issuing/cards/ic_1")
    client = make_client(sleeps, api_key="")

    with pytest.raises(ValueError, match="STRIPE_ISSUING_API_KEY"):
        await client.get("/issuing/cards/ic_1")

    assert route.call_count == 0


@pytest.mark.parametrize("key", ["sk_live_abc123", "rk_live_abc123"])
def test_a_live_key_is_refused_outright(key: str) -> None:
    # This project is sandbox-only by construction (SPEC.md §2), and Stripe picks
    # test-vs-live from the key rather than the host — so unlike Lithic, there is
    # no separate base URL standing between a misconfiguration and real money.
    with pytest.raises(ValueError, match="live"):
        checked_api_key(key)


@pytest.mark.parametrize("key", ["sk_test_abc123", "rk_test_abc123", "sk_something_else"])
def test_a_test_key_and_an_unfamiliar_one_are_both_allowed(key: str) -> None:
    # Only `_live_` is refused. Guessing at Stripe's full set of key prefixes would
    # be a way to reject a legitimate credential.
    assert checked_api_key(key) == key


# ----------------------------------------------------------------- writing ----


@respx.mock
async def test_a_post_sends_a_form_body_not_json(sleeps: Sleeps) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await make_client(sleeps).post(
        "/issuing/cards",
        body={
            "cardholder": "ich_1",
            "currency": "usd",
            "type": "virtual",
            "spending_controls": {"spending_limits": [{"amount": 100_000, "interval": "all_time"}]},
        },
    )

    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert sent_form(sent) == {
        "cardholder": "ich_1",
        "currency": "usd",
        "type": "virtual",
        "spending_controls[spending_limits][0][amount]": "100000",
        "spending_controls[spending_limits][0][interval]": "all_time",
    }


@respx.mock
async def test_an_idempotency_key_is_sent_only_when_asked_for(sleeps: Sleeps) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )
    client = make_client(sleeps)

    await client.post("/issuing/cards", body={"currency": "usd"}, idempotency_key="key-1")
    await client.post("/issuing/cards", body={"currency": "usd"})

    with_key, without = route.calls[0].request, route.calls[1].request
    assert "key-1" == with_key.headers["idempotency-key"]
    assert "idempotency-key" not in without.headers


@respx.mock
async def test_a_post_with_no_body_still_posts(sleeps: Sleeps) -> None:
    route = respx.post(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await make_client(sleeps).post("/issuing/cards/ic_1")

    assert route.calls.last.request.content == b""


# ----------------------------------------------------------------- reading ----


@respx.mock
async def test_query_parameters_use_the_same_bracket_encoding(sleeps: Sleeps) -> None:
    route = respx.get(f"{BASE_URL}/issuing/transactions").mock(
        return_value=httpx.Response(200, json=fixture("transactions_page_2"))
    )

    await make_client(sleeps).get(
        "/issuing/transactions", params={"card": "ic_1", "created": {"gte": 1785000000}}
    )

    sent = route.calls.last.request
    assert sent.url.params["card"] == "ic_1"
    assert sent.url.params["created[gte]"] == "1785000000"


@respx.mock
async def test_a_successful_body_that_is_not_an_object_is_an_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(return_value=httpx.Response(200, json=[1, 2]))

    with pytest.raises(StripeApiError, match="not a JSON object"):
        await make_client(sleeps).get("/issuing/cards/ic_1")


@respx.mock
async def test_a_success_with_an_unreadable_body_is_an_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(200, content=b"<html>maintenance</html>")
    )

    with pytest.raises(StripeApiError):
        await make_client(sleeps).get("/issuing/cards/ic_1")


# -------------------------------------------------------------- pagination ----


@respx.mock
async def test_list_all_follows_the_cursor_to_the_end(sleeps: Sleeps) -> None:
    # A balance computed from the first page only would be right until a card had
    # more than one page of transactions.
    route = respx.get(f"{BASE_URL}/issuing/transactions").mock(
        side_effect=[
            httpx.Response(200, json=fixture("transactions_page_1")),
            httpx.Response(200, json=fixture("transactions_page_2")),
        ]
    )

    collected = await make_client(sleeps).list_all(
        "/issuing/transactions", params={"card": CARD_ID}
    )

    expected = [
        entry["id"]
        for page in ("transactions_page_1", "transactions_page_2")
        for entry in fixture(page)["data"]
    ]
    assert [entry["id"] for entry in collected] == expected
    first, second = route.calls[0].request, route.calls[1].request
    assert first.url.params["limit"] == str(DEFAULT_PAGE_SIZE)
    assert "starting_after" not in first.url.params
    # The cursor is the last id on the previous page: Stripe lists newest-first, so
    # `starting_after` walks further back in time.
    assert second.url.params["starting_after"] == fixture("transactions_page_1")["data"][-1]["id"]
    assert second.url.params["card"] == CARD_ID


@respx.mock
async def test_list_all_stops_when_a_page_claims_more_but_sends_nothing(sleeps: Sleeps) -> None:
    # Otherwise the same cursor is requested forever: a hang rather than a failure.
    respx.get(f"{BASE_URL}/issuing/transactions").mock(
        return_value=httpx.Response(200, json={"object": "list", "has_more": True, "data": []})
    )

    assert await make_client(sleeps).list_all("/issuing/transactions") == []


@respx.mock
async def test_a_list_response_without_a_data_array_is_an_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/transactions").mock(
        return_value=httpx.Response(200, json={"object": "list", "has_more": False})
    )

    with pytest.raises(StripeApiError, match="`data` array"):
        await make_client(sleeps).list_all("/issuing/transactions")


@respx.mock
async def test_a_page_whose_last_entry_has_no_id_cannot_be_paged_past(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/transactions").mock(
        return_value=httpx.Response(
            200, json={"object": "list", "has_more": True, "data": [{"amount": -1}]}
        )
    )

    with pytest.raises(StripeApiError, match="cursor"):
        await make_client(sleeps).list_all("/issuing/transactions")


# ------------------------------------------------------------------ errors ----


@respx.mock
async def test_an_error_body_becomes_a_typed_issuer_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_nope")

    error = caught.value
    # An `IssuerError`, so everything above `issuers/` already handles it (the API
    # answers 502) without knowing Stripe exists.
    assert isinstance(error, IssuerError)
    assert error.status == 404
    assert error.code == "resource_missing"
    assert error.error_type == "invalid_request_error"
    # Stripe names the parameter it could not resolve, which is not always `id`.
    assert error.param == fixture("error_resource_missing")["error"]["param"]
    assert "No such issuing card" in error.message
    # Stripe's error body carries a link to the request log, which is the first
    # thing their support asks for.
    assert error.request_log_url is not None
    assert not error.retryable


@respx.mock
async def test_the_request_id_header_is_kept_when_stripe_sends_one(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(
            404,
            json=fixture("error_resource_missing"),
            headers={"Request-Id": "req_stablecard0001"},
        )
    )

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_nope")

    assert caught.value.request_id == "req_stablecard0001"


@respx.mock
async def test_an_error_body_that_is_not_stripe_shaped_still_raises(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(500, content=b"upstream exploded")
    )

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_1")

    assert caught.value.status == 500
    assert caught.value.code is None


@respx.mock
async def test_no_error_ever_repeats_the_api_key(sleeps: Sleeps) -> None:
    # These messages reach logs and, through the 502 handler, response bodies.
    respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(401, json={"error": {"message": f"key {API_KEY} is invalid"}})
    )

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_nope")

    assert API_KEY not in str(caught.value)
    assert API_KEY not in repr(caught.value)


# ----------------------------------------------------------------- retrying ----


def test_the_retryable_statuses_are_the_ones_a_retry_can_fix() -> None:
    assert RETRYABLE_STATUSES == frozenset({429, 500, 502, 503, 504})
    # 409 is Stripe's idempotency-key mismatch and 402 its request-failed: neither
    # changes answer on a repeat, and retrying either hides a caller bug.
    assert 409 not in RETRYABLE_STATUSES
    assert 402 not in RETRYABLE_STATUSES


@respx.mock
async def test_a_rate_limit_is_retried_with_increasing_backoff(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        side_effect=[
            httpx.Response(429, json=fixture("error_rate_limited")),
            httpx.Response(429, json=fixture("error_rate_limited")),
            httpx.Response(200, json=fixture("card_created")),
        ]
    )

    card = await make_client(sleeps).get("/issuing/cards/ic_1")

    assert card["id"] == CARD_ID
    assert sleeps.waited == [1.0, 2.0]


@respx.mock
async def test_retries_are_capped_and_the_last_failure_is_reported(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(
        return_value=httpx.Response(429, json=fixture("error_rate_limited"))
    )

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_1")

    assert caught.value.status == 429
    assert caught.value.retryable
    assert len(sleeps.waited) == 3


@respx.mock
async def test_a_client_error_is_never_retried(sleeps: Sleeps) -> None:
    # Retrying a 404 adds latency and hides the error.
    route = respx.get(f"{BASE_URL}/issuing/cards/ic_nope").mock(
        return_value=httpx.Response(404, json=fixture("error_resource_missing"))
    )

    with pytest.raises(StripeApiError):
        await make_client(sleeps).get("/issuing/cards/ic_nope")

    assert route.call_count == 1
    assert sleeps.waited == []


@respx.mock
async def test_a_transport_failure_is_retried_then_reported(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/issuing/cards/ic_1").mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(StripeApiError) as caught:
        await make_client(sleeps).get("/issuing/cards/ic_1")

    # Status 0: nothing answered at all, which is not the same as a 500.
    assert caught.value.status == 0
    assert caught.value.retryable
    assert len(sleeps.waited) == 3


@respx.mock
async def test_a_retried_write_reuses_its_idempotency_key(sleeps: Sleeps) -> None:
    # The point of the header: a retried create must not leave a cardholder with
    # two cards, and the retry has to carry the same key to mean that.
    route = respx.post(f"{BASE_URL}/issuing/cards").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"message": "temporarily unavailable"}}),
            httpx.Response(200, json=fixture("card_created")),
        ]
    )

    await make_client(sleeps).post(
        "/issuing/cards", body={"currency": "usd"}, idempotency_key="key-1"
    )

    assert [call.request.headers["idempotency-key"] for call in route.calls] == ["key-1", "key-1"]
