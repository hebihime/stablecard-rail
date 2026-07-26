"""`GET /otp/pending` — the reliable half of OTP delivery (SPEC.md §6.3).

SPEC.md §6.3 puts polling and push in that order deliberately: "polling is the
reliable fallback; push is the demo-quality path". So this endpoint is the
contract. A client that only ever polls must be able to complete a challenge, and
these tests are written from that angle — what the modal in §6.4 needs in order to
render itself is what the response has to contain.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis

from app.core.time import utcnow
from app.otp.store import OtpChallenge, OtpStore, challenge_key

CODE = "918273"


@pytest.fixture
def store(redis_client: Redis) -> OtpStore:
    return OtpStore(redis_client)


async def seed(
    store: OtpStore,
    *,
    challenge_id: str = "3ds_000001",
    provider_id: str = "gnosis_pay_mock",
    card_id: str | None = "card_1",
    code: str = CODE,
    expires_in: int = 300,
    derived: bool = False,
) -> OtpChallenge:
    now = utcnow()
    challenge = OtpChallenge(
        provider_id=provider_id,
        challenge_id=challenge_id,
        card_id=card_id,
        cardholder_id="user_1",
        event_id="evt-1",
        code=code,
        derived=derived,
        delivered_at=now,
        expires_at=now + timedelta(seconds=expires_in),
        amount_minor=1234,
        currency="USD",
    )
    await store.remember(challenge, now=now)
    return challenge


async def test_nothing_pending_is_an_empty_list_not_a_404(client: httpx.AsyncClient) -> None:
    # A client polls on a timer. "No open challenges" is the normal answer and has
    # to be cheap and unremarkable, not an error to be special-cased.
    response = await client.get("/otp/pending")

    assert 200 == response.status_code
    assert {"count": 0, "challenges": []} == response.json()


async def test_a_pending_challenge_carries_what_the_modal_needs(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store)

    body = (await client.get("/otp/pending")).json()

    assert 1 == body["count"]
    challenge = body["challenges"][0]
    assert "3ds_000001" == challenge["challenge_id"]
    assert "gnosis_pay_mock" == challenge["provider_id"]
    assert "card_1" == challenge["card_id"]
    # The code itself: SPEC.md §6.4's modal shows it with a copy button, so this
    # endpoint is the one place in the service that deliberately hands it out.
    assert CODE == challenge["code"]
    assert challenge["derived"] is False
    assert 1234 == challenge["amount_minor"]
    assert "USD" == challenge["currency"]


async def test_a_pending_challenge_says_how_long_is_left(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    # For the countdown in §6.4. A deadline alone would make the client subtract
    # against its own clock, which is the one clock we have no control over.
    await seed(store, expires_in=120)

    challenge = (await client.get("/otp/pending")).json()["challenges"][0]

    assert 0 < challenge["seconds_remaining"] <= 120
    assert challenge["expires_at"].startswith("20")


async def test_pending_is_ordered_by_which_challenge_dies_first(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store, challenge_id="late", expires_in=300)
    await seed(store, challenge_id="soon", expires_in=30)

    body = (await client.get("/otp/pending")).json()

    assert ["soon", "late"] == [item["challenge_id"] for item in body["challenges"]]


async def test_pending_can_be_narrowed_to_one_card(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store, challenge_id="a", card_id="card_1")
    await seed(store, challenge_id="b", card_id="card_2")

    body = (await client.get("/otp/pending", params={"card_id": "card_2"})).json()

    assert ["b"] == [item["challenge_id"] for item in body["challenges"]]


async def test_a_challenge_whose_code_is_gone_is_not_pending(
    client: httpx.AsyncClient, store: OtpStore, redis_client: Redis
) -> None:
    # Redis drops the code on its own and the index entry outlives it, so this is
    # really a test that the endpoint reads the code rather than the index.
    await seed(store)
    await redis_client.delete(challenge_key("gnosis_pay_mock", "3ds_000001"))

    body = (await client.get("/otp/pending")).json()

    assert 0 == body["count"]


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_an_out_of_range_limit_is_refused(client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get("/otp/pending", params={"limit": limit})

    assert 422 == response.status_code


async def test_the_limit_caps_what_is_returned(client: httpx.AsyncClient, store: OtpStore) -> None:
    for index in range(4):
        await seed(store, challenge_id=f"c{index}", expires_in=30 + index)

    body = (await client.get("/otp/pending", params={"limit": 2})).json()

    assert ["c0", "c1"] == [item["challenge_id"] for item in body["challenges"]]
