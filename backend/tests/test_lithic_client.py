"""The Lithic HTTP client (SPEC.md §3.2, §10).

Every response here is a fixture recorded from the real sandbox by
`scripts/record_lithic_fixtures.py`; nothing in this suite touches the network.
That is what makes these contract tests rather than mock tests — the shapes being
parsed are shapes Lithic actually sent, and re-running the recorder is how we find
out that they changed.

The client's whole job is to be boring: authenticate, retry what is worth retrying,
paginate to the end, and turn a provider error into one of `issuers/base.py`'s. The
interesting cases are the ones where getting it wrong is silent — a 400 retried
forever, a paginated balance that stops after the first page, an API key in an
exception message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.issuers.base import IssuerError
from app.issuers.lithic.client import DEFAULT_PAGE_SIZE, LithicApiError, LithicClient

BASE_URL = "https://sandbox.lithic.test/v1"
API_KEY = "test-sandbox-key-not-a-real-credential"

FIXTURES = Path(__file__).parent / "fixtures" / "lithic"


def fixture(name: str) -> Any:
    """One recorded sandbox response, verbatim."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Sleeps:
    """Stands in for `asyncio.sleep`, so a retry test costs nothing to run."""

    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def make_client(sleeps: Sleeps, *, api_key: str = API_KEY) -> LithicClient:
    return LithicClient(base_url=BASE_URL, api_key=api_key, timeout=5.0, sleep=sleeps)


@respx.mock
async def test_building_a_client_needs_no_credentials(sleeps: Sleeps) -> None:
    # `registry.describe()` builds every registered adapter in order to report its
    # funding model, and `GET /providers` calls it (docs/ARCHITECTURE.md §3.1, §8.6).
    # An adapter that refused to *exist* without a key would take that endpoint down
    # for the providers that are configured, and would make this suite's own
    # `test_describe_exposes_the_funding_model_taxonomy` depend on LITHIC_API_KEY —
    # green here, red in CI.
    route = respx.get(f"{BASE_URL}/cards/abc")
    client = make_client(sleeps, api_key="")

    with pytest.raises(ValueError, match="LITHIC_API_KEY"):
        await client.get("/cards/abc")

    # And nothing was sent: an empty key is refused before the request, not answered
    # with a 401 we would then have to translate.
    assert route.call_count == 0


# ---------------------------------------------------------- authentication ----


@respx.mock
async def test_the_api_key_goes_in_a_bare_authorization_header(sleeps: Sleeps) -> None:
    # Lithic's own error message for getting this wrong is "Please provide API key
    # in the form Authorization: [api-key]" — no `Bearer`, no scheme.
    route = respx.get(f"{BASE_URL}/cards/abc").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )

    await make_client(sleeps).get("/cards/abc")

    sent = route.calls.last.request
    assert API_KEY == sent.headers["authorization"]
    assert "application/json" == sent.headers["accept"]


@respx.mock
async def test_an_idempotency_key_is_sent_only_when_asked_for(sleeps: Sleeps) -> None:
    route = respx.post(f"{BASE_URL}/cards").mock(
        return_value=httpx.Response(200, json=fixture("card_created"))
    )
    client = make_client(sleeps)

    await client.post("/cards", json_body={"type": "VIRTUAL"}, idempotency_key="key-1")
    await client.post("/cards", json_body={"type": "VIRTUAL"})

    with_key, without = route.calls[0].request, route.calls[1].request
    assert "key-1" == with_key.headers["idempotency-key"]
    assert "idempotency-key" not in without.headers


# ----------------------------------------------------------------- errors ----


@respx.mock
async def test_a_provider_error_carries_its_status_message_and_request_id(
    sleeps: Sleeps,
) -> None:
    body = fixture("error_card_not_found")
    respx.get(f"{BASE_URL}/cards/missing").mock(return_value=httpx.Response(404, json=body))

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/missing")

    error = caught.value
    assert 404 == error.status
    assert error.message == body["message"]
    # The debugging id is what a Lithic support ticket asks for first.
    assert error.request_id == body["debugging_request_id"]
    assert isinstance(error, IssuerError), "must be catchable as an issuer failure"


@respx.mock
async def test_an_error_never_repeats_the_api_key(sleeps: Sleeps) -> None:
    # This exception ends up in logs and, via the 502 handler, in a response body.
    respx.get(f"{BASE_URL}/cards/x").mock(
        return_value=httpx.Response(401, json=fixture("error_unauthorized"))
    )

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/x")

    assert API_KEY not in str(caught.value)
    assert API_KEY not in repr(caught.value)


@respx.mock
async def test_a_non_json_error_body_is_still_an_issuer_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/cards/x").mock(return_value=httpx.Response(502, text="<html>nginx"))

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/x")

    assert 502 == caught.value.status
    assert caught.value.request_id is None


@respx.mock
async def test_a_success_that_is_not_json_is_an_issuer_error(sleeps: Sleeps) -> None:
    # A 200 we cannot read is not a success; better to fail than to guess at {}.
    respx.get(f"{BASE_URL}/cards/x").mock(return_value=httpx.Response(200, text="not json"))

    with pytest.raises(LithicApiError, match="not JSON"):
        await make_client(sleeps).get("/cards/x")


# ---------------------------------------------------------------- retrying ----


@respx.mock
async def test_a_rate_limited_request_is_retried_and_then_succeeds(sleeps: Sleeps) -> None:
    # The sandbox rate-limits readily — recording these very fixtures lost a
    # simulated void to a 429 before the recorder learned to wait.
    respx.post(f"{BASE_URL}/simulate/void").mock(
        side_effect=[
            httpx.Response(429, json={"message": "Rate limited"}),
            httpx.Response(201, json={"debugging_request_id": "abc"}),
        ]
    )

    result = await make_client(sleeps).post("/simulate/void", json_body={"amount": 1})

    assert "abc" == result["debugging_request_id"]
    assert [pytest.approx(1.0)] == sleeps.waited


@respx.mock
async def test_a_server_error_is_retried(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/cards/x").mock(
        side_effect=[
            httpx.Response(503, json={"message": "unavailable"}),
            httpx.Response(200, json=fixture("card_created")),
        ]
    )

    await make_client(sleeps).get("/cards/x")

    assert 1 == len(sleeps.waited)


@respx.mock
async def test_retries_back_off_and_then_give_up(sleeps: Sleeps) -> None:
    route = respx.get(f"{BASE_URL}/cards/x").mock(
        return_value=httpx.Response(429, json={"message": "Rate limited"})
    )

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/x")

    assert caught.value.retryable is True
    # Bounded, and increasing: a client that hammers a rate limiter is the problem.
    assert [1.0, 2.0, 4.0] == sleeps.waited
    assert 4 == route.call_count


@respx.mock
async def test_a_client_error_is_not_retried(sleeps: Sleeps) -> None:
    # Retrying a 404 or a 422 cannot help, and hides the real error behind latency.
    route = respx.get(f"{BASE_URL}/cards/x").mock(
        return_value=httpx.Response(404, json=fixture("error_card_not_found"))
    )

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/x")

    assert caught.value.retryable is False
    assert [] == sleeps.waited
    assert 1 == route.call_count


@respx.mock
async def test_a_timeout_is_retried_and_then_reported(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/cards/x").mock(side_effect=httpx.ConnectTimeout("too slow"))

    with pytest.raises(LithicApiError) as caught:
        await make_client(sleeps).get("/cards/x")

    assert caught.value.retryable is True
    assert 0 == caught.value.status, "no HTTP status: nothing answered"
    assert 3 == len(sleeps.waited)


@respx.mock
async def test_a_timeout_that_resolves_is_not_an_error(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/cards/x").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=fixture("card_created"))]
    )

    card = await make_client(sleeps).get("/cards/x")

    assert fixture("card_created")["token"] == card["token"]


# -------------------------------------------------------------- pagination ----


@respx.mock
async def test_pagination_follows_the_cursor_to_the_end(sleeps: Sleeps) -> None:
    # Lithic pages newest-first and `starting_after` walks *backwards* in time,
    # which reads like the opposite of what it does. Getting this wrong would make
    # every balance right until a card had more than one page of transactions.
    page_1, page_2 = fixture("transactions_page_1"), fixture("transactions_page_2")
    route = respx.get(f"{BASE_URL}/transactions").mock(
        side_effect=[httpx.Response(200, json=page_1), httpx.Response(200, json=page_2)]
    )

    collected = await make_client(sleeps).list_all("/transactions", params={"card_token": "c1"})

    assert page_1["data"] + page_2["data"] == collected
    first, second = route.calls[0].request, route.calls[1].request
    assert str(DEFAULT_PAGE_SIZE) == first.url.params["page_size"]
    assert "starting_after" not in first.url.params
    assert page_1["data"][-1]["token"] == second.url.params["starting_after"]


@respx.mock
async def test_pagination_stops_on_the_first_page_when_there_is_no_more(
    sleeps: Sleeps,
) -> None:
    route = respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json=fixture("transactions_all"))
    )

    collected = await make_client(sleeps).list_all("/transactions", params={"card_token": "c1"})

    assert fixture("transactions_all")["data"] == collected
    assert 1 == route.call_count


@respx.mock
async def test_an_empty_page_ends_the_walk(sleeps: Sleeps) -> None:
    # `has_more` with nothing in `data` would otherwise loop with the same cursor
    # forever — a hang, not a crash, which is worse.
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"data": [], "has_more": True})
    )

    assert [] == await make_client(sleeps).list_all("/transactions")


@respx.mock
async def test_a_page_that_is_not_a_list_is_refused(sleeps: Sleeps) -> None:
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"data": "surprise", "has_more": False})
    )

    with pytest.raises(LithicApiError, match="data"):
        await make_client(sleeps).list_all("/transactions")


# ------------------------------------------------------ a success with no body ----


@respx.mock
async def test_a_bodyless_success_is_refused_by_default(sleeps: Sleeps) -> None:
    # The strictness that was there before phase 7 and is worth keeping: a list
    # endpoint that suddenly answered 200 with nothing would otherwise read as "no
    # records", which is a wrong answer rather than an error.
    respx.get(f"{BASE_URL}/cards").mock(return_value=httpx.Response(200, content=b""))

    with pytest.raises(LithicApiError, match="not JSON"):
        await make_client(sleeps).get("/cards")


@respx.mock
async def test_a_bodyless_success_is_accepted_where_the_caller_expects_one(
    sleeps: Sleeps,
) -> None:
    """Opt-in per call, for the one endpoint that answers 200 with nothing.

    Their 3DS challenge-response endpoint is documented as "Challenge Response was
    received and forwarded to the ACS" with no response content, and the recorded
    404 from it has no body either. Relaxing this globally would have been the wrong
    fix (see the test above); the caller that knows says so (§11.7).
    """
    respx.post(f"{BASE_URL}/three_ds_decisioning/challenge_response").mock(
        return_value=httpx.Response(200, content=b"")
    )

    answered = await make_client(sleeps).post(
        "/three_ds_decisioning/challenge_response",
        json_body={"token": "t", "challenge_response": "APPROVE"},
        allow_empty_body=True,
    )

    assert {} == answered


@respx.mock
async def test_a_failure_with_no_body_still_carries_its_status(sleeps: Sleeps) -> None:
    # What the real sandbox does for an unknown 3DS token: 404 and nothing else. The
    # adapter maps on the status, so an absent error envelope must not become a
    # different kind of failure on the way up.
    respx.post(f"{BASE_URL}/three_ds_decisioning/challenge_response").mock(
        return_value=httpx.Response(404, content=b"")
    )

    with pytest.raises(LithicApiError) as raised:
        await make_client(sleeps).post(
            "/three_ds_decisioning/challenge_response", allow_empty_body=True
        )

    assert 404 == raised.value.status
    assert raised.value.request_id is None
