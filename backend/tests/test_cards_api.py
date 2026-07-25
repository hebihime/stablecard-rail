"""Card lifecycle over HTTP (SPEC.md §12 phase 2).

These routes are the surface the mobile client talks to in phase 8, so what is
asserted here is the contract: the provider is named in the path, card state comes
from the provider rather than from a local copy, every action lands in the ledger,
and each way a request can fail has its own status code.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from tests.support import all_ledger_events

PROVIDER = "gnosis_pay_mock"
HOLDER = {"email": "ada@example.test", "first_name": "Ada", "last_name": "Lovelace"}


async def create_card(client: httpx.AsyncClient, *, currency: str = "USD") -> dict[str, object]:
    holder = await client.post(f"/providers/{PROVIDER}/cardholders", json=HOLDER)
    assert holder.status_code == 201, holder.text
    cardholder_id = holder.json()["cardholder_id"]
    card = await client.post(
        f"/providers/{PROVIDER}/cardholders/{cardholder_id}/cards",
        json={"currency": currency, "spend_limit_minor": 100_000},
    )
    assert card.status_code == 201, card.text
    return dict(card.json())


# --------------------------------------------------------------- discovery ----


async def test_providers_are_listed_with_their_funding_model(client: httpx.AsyncClient) -> None:
    # A client can pick a provider without knowing any adapter exists (SPEC.md §3.2).
    response = await client.get("/providers")
    assert response.status_code == 200
    listed = response.json()
    assert {"provider_id": PROVIDER, "funding_model": "crypto_deposit"} in listed
    # Phase 3: both halves of the taxonomy are now real, and this endpoint gained
    # the second one by a `register()` line — no change here or in `api/`.
    assert {"provider_id": "lithic", "funding_model": "fiat_rail"} in listed


# --------------------------------------------------------------- lifecycle ----


async def test_creating_a_cardholder_is_ledgered_without_storing_personal_data(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(f"/providers/{PROVIDER}/cardholders", json=HOLDER)

    assert response.status_code == 201
    body = response.json()
    assert body["provider_id"] == PROVIDER
    assert body["cardholder_id"]

    (event,) = await all_ledger_events(session)
    assert event.event_type == "cardholder.created"
    assert event.cardholder_id == body["cardholder_id"]
    # The ledger is not a place to keep names and addresses; the domain is enough
    # to debug a delivery problem.
    assert event.payload == {"email_domain": "example.test"}


async def test_a_created_card_reports_provider_state_and_a_deposit_address(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card = await create_card(client)

    assert card["state"] == "unactivated"
    assert card["currency"] == "USD"
    assert len(str(card["last_four"])) == 4
    assert str(card["deposit_address"]).startswith("0x")

    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == ["cardholder.created", "card.created"]
    created = events[-1]
    assert created.card_id == card["card_id"]
    assert created.state_after == "unactivated"
    assert created.payload["deposit_address"] == card["deposit_address"]
    assert created.payload["funding_model"] == "crypto_deposit"


async def test_no_card_response_ever_carries_card_number_material(
    client: httpx.AsyncClient,
) -> None:
    # Full PAN/CVV is a separate short-lived single-use reveal path (SPEC.md §9.2).
    card = await create_card(client)
    assert not {"pan", "cvv", "cvc", "number"} & set(card)


async def test_activate_freeze_unfreeze_cancel_each_record_before_and_after(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card_id = (await create_card(client))["card_id"]
    base = f"/providers/{PROVIDER}/cards/{card_id}"

    for action, expected in (
        ("activate", "active"),
        ("freeze", "frozen"),
        ("activate", "active"),
        ("cancel", "canceled"),
    ):
        response = await client.post(f"{base}/{action}")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == expected

    transitions = [
        (event.event_type, event.state_before, event.state_after)
        for event in await all_ledger_events(session)
        if event.event_type.startswith("card.") and event.event_type != "card.created"
    ]
    assert transitions == [
        ("card.activated", "unactivated", "active"),
        ("card.frozen", "active", "frozen"),
        ("card.activated", "frozen", "active"),
        ("card.canceled", "active", "canceled"),
    ]


async def test_reading_a_card_reflects_the_provider_and_ledgers_nothing(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card_id = (await create_card(client))["card_id"]
    await client.post(f"/providers/{PROVIDER}/cards/{card_id}/activate")
    before = len(await all_ledger_events(session))

    response = await client.get(f"/providers/{PROVIDER}/cards/{card_id}")

    assert response.status_code == 200
    assert response.json()["state"] == "active"
    assert len(await all_ledger_events(session)) == before, "a read is not an event"


async def test_the_balance_endpoint_speaks_integer_minor_units(
    client: httpx.AsyncClient, mock_adapter: GnosisPayMockAdapter
) -> None:
    card_id = str((await create_card(client))["card_id"])
    await client.post(f"/providers/{PROVIDER}/cards/{card_id}/activate")
    # A confirmed transfer into the card's Safe is what creates a balance at this
    # provider; `fund_card` only attributes one, so calling it here would leave the
    # balance at zero (SPEC.md §3.2).
    card = await mock_adapter.get_card(card_id)
    assert card.deposit_address is not None
    mock_adapter.simulator.receive_onchain_deposit(card.deposit_address, Money(2500, "USD"))

    response = await client.get(f"/providers/{PROVIDER}/cards/{card_id}/balance")

    assert response.status_code == 200
    assert response.json() == {"card_id": card_id, "amount_minor": 2500, "currency": "USD"}


async def test_there_is_no_route_that_funds_a_card_directly(client: httpx.AsyncClient) -> None:
    # Money reaches a card through the funding state machine, never through an
    # HTTP call to an issuer — so there is deliberately nothing to POST here.
    card_id = (await create_card(client))["card_id"]
    response = await client.post(
        f"/providers/{PROVIDER}/cards/{card_id}/fund", json={"amount_minor": 2500}
    )
    assert response.status_code == 405 or response.status_code == 404


# ------------------------------------------------------------------ errors ----


async def test_an_unknown_provider_is_a_404_naming_what_exists(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/providers/wells_fargo/cards/card_1")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "unknown_provider"
    assert PROVIDER in body["detail"]


async def test_an_unknown_card_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/providers/{PROVIDER}/cards/card_nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_an_unknown_cardholder_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/providers/{PROVIDER}/cardholders/chr_nope/cards", json={"currency": "USD"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_refused_lifecycle_change_is_a_409_and_changes_nothing(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card_id = (await create_card(client))["card_id"]
    base = f"/providers/{PROVIDER}/cards/{card_id}"
    await client.post(f"{base}/activate")
    await client.post(f"{base}/cancel")
    before = await all_ledger_events(session)

    response = await client.post(f"{base}/activate")

    assert response.status_code == 409
    assert response.json()["code"] == "illegal_card_transition"
    assert (await client.get(base)).json()["state"] == "canceled"
    assert len(await all_ledger_events(session)) == len(before), "a refusal is not an action"


async def test_a_malformed_cardholder_request_is_a_422(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/providers/{PROVIDER}/cardholders", json={"email": "a@b.test"})
    assert response.status_code == 422


async def test_unknown_fields_are_refused_rather_than_ignored(
    client: httpx.AsyncClient,
) -> None:
    # `extra="forbid"` on the request models: a client sending `spendLimit` should
    # find out, not silently get an unlimited card.
    response = await client.post(
        f"/providers/{PROVIDER}/cardholders", json={**HOLDER, "ssn": "000-00-0000"}
    )
    assert response.status_code == 422
