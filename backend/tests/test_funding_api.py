"""Reading a funding intent over HTTP (SPEC.md §9.3).

The fund screen "live-renders the funding intent's state machine progress
(PENDING → … → FUNDED) by polling the intent endpoint". This is that endpoint, and
it did not exist before phase 8 — phase 5 built the whole machine and never needed
a reader, because everything that drives it is a chain poll or a webhook.

**The sequence is served, not assumed.** A client that hardcodes
`['PENDING', 'DEPOSIT_CONFIRMED', ...]` holds a second copy of the state machine,
in another language, updated by hand. Phase 5 added two self-transitions and phase
6 changed what `BRIDGED` means; neither would have reached such a copy. So the
response carries the path and the position in it, derived from the transition table
`app/funding/states.py` already owns.

Read-only, like `GET /ledger`: there is no route here that advances an intent.
Money moves through the engine, never through a request.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.funding.states import FundingState
from tests.support import SeedIntent

CARD = "card_test_1"
PROVIDER = "gnosis_pay_mock"


async def test_an_intent_reads_back_with_its_money_and_its_references(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.BRIDGING, amount_minor=2500)

    response = await client.get(f"/funding/intents/{intent.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(intent.id)
    assert body["state"] == "BRIDGING"
    assert body["amount_minor"] == 2500
    assert body["currency"] == "USD"
    assert body["card_id"] == CARD
    assert body["provider_id"] == "gnosis_pay_mock"


async def test_the_response_carries_the_path_and_where_the_intent_is_on_it(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.BRIDGING)

    body = (await client.get(f"/funding/intents/{intent.id}")).json()

    progress = body["progress"]
    assert progress["sequence"] == [
        "PENDING",
        "DEPOSIT_CONFIRMED",
        "BRIDGING",
        "BRIDGED",
        "FUNDING",
        "FUNDED",
        "SETTLED",
    ]
    assert progress["position"] == 2
    assert progress["is_terminal"] is False
    assert progress["is_failure"] is False


async def test_a_settled_intent_is_at_the_end_and_terminal(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.SETTLED)

    progress = (await client.get(f"/funding/intents/{intent.id}")).json()["progress"]

    assert progress["position"] == 6
    assert progress["is_terminal"] is True
    assert progress["is_failure"] is False


async def test_a_failed_intent_has_no_position_on_the_happy_path(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    """A failure is not a later stage of the same journey.

    Giving `FAILED_BRIDGE` an index would let a client render it as step 3 of 7 with
    four still to come, which is the opposite of what happened. `position` is null
    and `is_failure` says why, so the only thing a client can draw is the truth.
    """
    intent = await seed_intent(state=FundingState.FAILED_BRIDGE)

    body = (await client.get(f"/funding/intents/{intent.id}")).json()

    assert body["progress"]["position"] is None
    assert body["progress"]["is_failure"] is True
    assert body["progress"]["is_terminal"] is True
    assert body["state"] == "FAILED_BRIDGE"


async def test_a_failed_intent_reports_the_reason_it_carries(
    client: httpx.AsyncClient, seed_intent: SeedIntent, session: AsyncSession
) -> None:
    # `last_error` is the only human-readable account of a failure outside the
    # ledger, and a fund screen with a red step and no reason is a support ticket.
    intent = await seed_intent(state=FundingState.FAILED_FUNDING)
    intent.last_error = "issuer refused: insufficient program balance"
    await session.commit()

    body = (await client.get(f"/funding/intents/{intent.id}")).json()

    assert body["last_error"] == "issuer refused: insufficient program balance"


async def test_the_bridge_fee_is_visible_as_the_difference_between_two_numbers(
    client: httpx.AsyncClient, seed_intent: SeedIntent, session: AsyncSession
) -> None:
    # SPEC.md §11 wants bridged amounts net of fees legible. Both numbers are
    # returned rather than one adjusted number, so the fee is a subtraction the
    # client can show rather than a change nobody can see (docs/ARCHITECTURE.md §9).
    intent = await seed_intent(state=FundingState.BRIDGED, amount_minor=2500)
    intent.bridged_amount_minor = 2470
    await session.commit()

    body = (await client.get(f"/funding/intents/{intent.id}")).json()

    assert body["amount_minor"] == 2500
    assert body["bridged_amount_minor"] == 2470


async def test_an_intent_that_has_not_bridged_reports_no_bridged_amount(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.PENDING)

    body = (await client.get(f"/funding/intents/{intent.id}")).json()

    assert body["bridged_amount_minor"] is None


async def test_an_unknown_intent_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/funding/intents/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_malformed_intent_id_is_a_422_not_a_500(client: httpx.AsyncClient) -> None:
    response = await client.get("/funding/intents/not-a-uuid")

    assert response.status_code == 422


# ------------------------------------------------------------------ listing ----


async def test_intents_for_a_card_come_back_newest_first(
    client: httpx.AsyncClient, seed_intent: SeedIntent, session: AsyncSession
) -> None:
    """Newest first, unlike the ledger, and for the opposite reason.

    `GET /ledger` is ascending because it reads as a story. A client asking "what is
    happening with my card right now" wants the current attempt, and the current
    attempt is the most recent one.
    """
    older = await seed_intent(state=FundingState.FAILED_BRIDGE, deposit_tx_ref="sig_older")
    newer = await seed_intent(state=FundingState.FUNDING, deposit_tx_ref="sig_newer")
    # Set explicitly rather than trusting two commits to land on different
    # microseconds. `created_at` defaults to `now()`, which is transaction-start,
    # and a tie here would make the assertion below flake rather than fail —
    # the same class of bomb as the reconciler tests hard-coding a wall time.
    older.created_at = newer.created_at - timedelta(minutes=5)
    await session.commit()

    body = (await client.get("/funding/intents", params={"card_id": CARD})).json()

    assert body["count"] == 2
    assert [intent["id"] for intent in body["intents"]] == [str(newer.id), str(older.id)]


async def test_listing_filters_by_card(client: httpx.AsyncClient, seed_intent: SeedIntent) -> None:
    await seed_intent(deposit_tx_ref="sig_a")
    await seed_intent(card_id="card_other", deposit_tx_ref="sig_b")

    body = (await client.get("/funding/intents", params={"card_id": CARD})).json()

    assert body["count"] == 1
    assert body["intents"][0]["card_id"] == CARD


async def test_listing_with_no_filter_returns_everything_it_is_allowed_to(
    client: httpx.AsyncClient, seed_intent: SeedIntent
) -> None:
    await seed_intent(deposit_tx_ref="sig_a")
    await seed_intent(card_id="card_other", deposit_tx_ref="sig_b")

    body = (await client.get("/funding/intents")).json()

    assert body["count"] == 2


async def test_an_empty_list_is_not_a_404(client: httpx.AsyncClient) -> None:
    # A fund screen polls before any deposit exists, and "nothing yet" is the
    # normal answer for most of that screen's life.
    body = (await client.get("/funding/intents", params={"card_id": "card_nothing"})).json()

    assert body == {"count": 0, "intents": []}


@pytest.mark.parametrize("limit", [0, 501])
async def test_an_out_of_range_limit_is_refused(client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get("/funding/intents", params={"limit": limit})

    assert response.status_code == 422


async def test_the_limit_is_honoured(client: httpx.AsyncClient, seed_intent: SeedIntent) -> None:
    await seed_intent(deposit_tx_ref="sig_a")
    await seed_intent(deposit_tx_ref="sig_b")

    body = (await client.get("/funding/intents", params={"limit": 1})).json()

    assert body["count"] == 1


# ----------------------------------------------------------- deposit routes ----
#
# Where to send money so it reaches a card (SPEC.md §9.3). The address is derived
# from this service's own keypair rather than accepted from the caller, which is the
# property most of these assert.

#: A throwaway devnet keypair, generated for this file and never funded. Not a
#: secret by any definition that matters: it exists so `from_env_value` has 64
#: bytes to parse, and the suite never touches a network.
DEPOSIT_KEYPAIR = (
    "4qqPR89ZbtNgjQkL26AreSyXFv4DwAVtF9JD9qRouAYcTfkCudE431gkZRhqzzwQHJPoY18LMthPLMarywdS4YxK"
)


@pytest.fixture
def configured_keypair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the route endpoints an address to derive.

    Patches the *importing* module's attribute, not `app.chain.config`'s. Anything
    that did `from app.core.config import get_settings` keeps its own reference, and
    patching the source module leaves it untouched — a trap this repo has already
    paid for once (WORKLOG, and `tests/test_lithic_adapter.py` says the same).
    """
    from app.chain.config import get_solana_settings

    configured = get_solana_settings().model_copy(update={"deposit_keypair": DEPOSIT_KEYPAIR})
    monkeypatch.setattr("app.api.funding.get_solana_settings", lambda: configured)


async def test_a_route_names_the_address_this_service_watches(
    client: httpx.AsyncClient, configured_keypair: None
) -> None:
    response = await client.post(
        "/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["card_id"] == CARD
    assert body["chain"] == "solana-devnet"
    assert body["decimals"] == 6
    # The token account, not the wallet. A fund screen showing the wallet collects
    # deposits the watcher never sees — docs/ARCHITECTURE.md §9.8 in its other
    # direction.
    assert body["deposit_address"] != body["owner_address"]


async def test_the_caller_cannot_choose_the_address(
    client: httpx.AsyncClient, configured_keypair: None
) -> None:
    """The one that matters. An address is not a parameter.

    A caller who could name one could point the watcher at somebody else's token
    account and be credited for their deposits.
    """
    response = await client.post(
        "/funding/deposit-routes",
        json={"provider_id": PROVIDER, "card_id": CARD, "deposit_address": "AttackerOwned111"},
    )

    assert response.status_code == 201
    assert response.json()["deposit_address"] != "AttackerOwned111"


async def test_opening_the_fund_screen_twice_is_a_no_op(
    client: httpx.AsyncClient, configured_keypair: None
) -> None:
    # `funding/routes.py` was written for this: "the fund screen will do it every
    # time it is opened".
    first = await client.post(
        "/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD}
    )
    second = await client.post(
        "/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD}
    )

    assert first.json() == second.json()


async def test_routes_can_be_read_back_for_a_card(
    client: httpx.AsyncClient, configured_keypair: None
) -> None:
    await client.post("/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD})

    listed = await client.get(
        "/funding/deposit-routes", params={"provider_id": PROVIDER, "card_id": CARD}
    )

    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_a_card_with_no_route_lists_nothing_rather_than_404(
    client: httpx.AsyncClient, configured_keypair: None
) -> None:
    listed = await client.get(
        "/funding/deposit-routes", params={"provider_id": PROVIDER, "card_id": "card_fresh"}
    )

    assert listed.status_code == 200
    assert listed.json() == []


async def test_an_unconfigured_keypair_says_which_setting_is_missing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 503 and the variable's name: the difference between a two-minute fix and an
    # investigation. Phase 4 recorded the same rule for the webhook secret (§8.11).
    from app.chain import config as chain_config

    settings = chain_config.get_solana_settings()
    monkeypatch.setattr(
        "app.api.funding.get_solana_settings",
        lambda: settings.model_copy(update={"deposit_keypair": ""}),
    )

    response = await client.post(
        "/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD}
    )

    assert response.status_code == 503
    assert "SOLANA_DEPOSIT_KEYPAIR" in response.text


async def test_an_unusable_keypair_says_so_rather_than_500ing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.chain import config as chain_config

    settings = chain_config.get_solana_settings()
    monkeypatch.setattr(
        "app.api.funding.get_solana_settings",
        lambda: settings.model_copy(update={"deposit_keypair": "not-a-keypair"}),
    )

    response = await client.post(
        "/funding/deposit-routes", json={"provider_id": PROVIDER, "card_id": CARD}
    )

    assert response.status_code == 503
    assert "unusable" in response.text
