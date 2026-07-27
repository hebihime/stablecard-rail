"""The reveal surface over HTTP (SPEC.md §9.2).

Two calls, and the split between them is the whole pattern: the backend mints a
short-lived single-use token, and the client exchanges it for card data in an
isolated component. Modelled on Gnosis Pay's PSE, where the mint is an mTLS call
only a partner backend can make.

What is asserted here beyond the happy path is the set of things that would each
look fine in a demo and be wrong in production: a token that works twice, a 404 that
tells an attacker their guess was once real, a provider that quietly returns a
sandbox PAN, and a ledger row with a credential in it.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.issuers import registry
from app.issuers.base import IssuerError, RevealedCard, RevealUnsupported
from tests.support import all_ledger_events
from tests.test_cards_api import PROVIDER, create_card


async def mint(client: httpx.AsyncClient, card_id: str) -> dict[str, object]:
    response = await client.post(f"/providers/{PROVIDER}/cards/{card_id}/reveal-token")
    assert response.status_code == 201, response.text
    return dict(response.json())


# ------------------------------------------------------------------ minting ----


async def test_minting_returns_a_token_and_a_countdown(client: httpx.AsyncClient) -> None:
    card = await create_card(client)

    body = await mint(client, str(card["card_id"]))

    assert body["token"]
    assert body["card_id"] == card["card_id"]
    assert body["provider_id"] == PROVIDER
    # The countdown comes from the server, so the screen's auto-hide does not
    # depend on the one clock this service cannot vouch for.
    assert body["expires_in"] == 60
    assert body["expires_at"]


async def test_minting_is_ledgered_before_anything_is_revealed(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card = await create_card(client)

    await mint(client, str(card["card_id"]))

    minted = [e for e in await all_ledger_events(session) if e.event_type == "reveal.token_minted"]
    assert len(minted) == 1
    assert minted[0].card_id == card["card_id"]
    assert minted[0].provider_id == PROVIDER


async def test_no_ledger_row_from_a_reveal_ever_holds_a_token(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The row records that a reveal happened, never the credential that caused it.

    Phase 7 made the same argument about an OTP code and enforced it in the model
    (`Field(exclude=True)`). Here the token never reaches a model that gets
    serialized, so the enforcement is this test plus the shape of `RevealGrant`,
    which carries no token by construction.
    """
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))
    token = str(body["token"])

    await client.post("/reveal", json={"token": token})

    for event in await all_ledger_events(session):
        assert token not in str(event.payload)


async def test_minting_for_an_unknown_card_is_a_404(client: httpx.AsyncClient) -> None:
    # Checked against the provider before a token exists: handing out a token for a
    # card that is not there would move the failure to the exchange, by which point
    # the client has a credential and a countdown running.
    response = await client.post(f"/providers/{PROVIDER}/cards/card_nope/reveal-token")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_minting_at_a_provider_that_cannot_reveal_says_so_at_once(
    client: httpx.AsyncClient,
) -> None:
    """501 at the mint, not a 502 at the exchange.

    Lithic and Stripe inherit the refusal deliberately (docs/ARCHITECTURE.md §12.2).
    Discovering that only after issuing a token would mean a client showing a reveal
    screen, a countdown, and then an error — for a provider that was never going to
    answer.
    """
    response = await client.post("/providers/lithic/cards/card_whatever/reveal-token")

    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "reveal_unsupported"
    assert "lithic" in body["detail"]


async def test_an_unknown_provider_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/providers/nonesuch/cards/card_1/reveal-token")

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_provider"


# ---------------------------------------------------------------- exchange ----


async def test_exchanging_a_token_renders_the_card(client: httpx.AsyncClient) -> None:
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))

    response = await client.post("/reveal", json={"token": body["token"]})

    assert response.status_code == 200, response.text
    revealed = response.json()
    assert revealed["card_id"] == card["card_id"]
    assert revealed["last_four"] == card["last_four"]
    assert revealed["exp_month"] == card["exp_month"]
    assert revealed["rendered_in"] == "pse-iframe"


async def test_what_comes_back_has_no_card_number_in_it(client: httpx.AsyncClient) -> None:
    # The invariant, asserted at the outermost boundary there is. `RevealedCard`
    # cannot hold a PAN (tests/test_card_reveal.py); this is the promise that no
    # route smuggles one past it in a `raw` blob.
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))

    response = await client.post("/reveal", json={"token": body["token"]})

    assert set(response.json()) == set(RevealedCard.model_fields)
    for forbidden in ("pan", "cvv", "cvc", "number"):
        assert forbidden not in response.text.lower()


async def test_a_reveal_is_ledgered(client: httpx.AsyncClient, session: AsyncSession) -> None:
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))

    await client.post("/reveal", json={"token": body["token"]})

    granted = [e for e in await all_ledger_events(session) if e.event_type == "reveal.granted"]
    assert len(granted) == 1
    assert granted[0].card_id == card["card_id"]
    assert granted[0].payload["rendered_in"] == "pse-iframe"


async def test_a_token_cannot_be_spent_twice(client: httpx.AsyncClient) -> None:
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))

    first = await client.post("/reveal", json={"token": body["token"]})
    second = await client.post("/reveal", json={"token": body["token"]})

    assert first.status_code == 200
    assert second.status_code == 404


async def test_a_replay_and_a_guess_are_indistinguishable_to_the_caller(
    client: httpx.AsyncClient,
) -> None:
    """Same status, same code, same prose — and different rows in the ledger.

    Telling a caller that *this* token was once real and *that* one never existed is
    an oracle: it turns guessing into a search with feedback. The distinction is
    real and it is worth recording, so it is recorded where only we can read it.
    """
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))
    await client.post("/reveal", json={"token": body["token"]})

    replayed = await client.post("/reveal", json={"token": body["token"]})
    guessed = await client.post("/reveal", json={"token": "not-a-token-anyone-minted"})

    assert replayed.status_code == guessed.status_code == 404
    assert replayed.json() == guessed.json()


async def test_the_ledger_does_tell_a_replay_from_a_guess(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    card = await create_card(client)
    body = await mint(client, str(card["card_id"]))
    await client.post("/reveal", json={"token": body["token"]})
    await client.post("/reveal", json={"token": body["token"]})
    await client.post("/reveal", json={"token": "not-a-token-anyone-minted"})

    rejected = [e for e in await all_ledger_events(session) if e.event_type == "reveal.rejected"]
    reasons = [event.payload["reason"] for event in rejected]
    assert reasons == ["replayed", "unknown"]
    # A replay names the card its token was minted for; a guess names nothing,
    # because there is nothing to attribute it to.
    assert rejected[0].card_id == card["card_id"]
    assert rejected[1].card_id is None


async def test_an_empty_token_is_refused_without_reaching_redis(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/reveal", json={"token": ""})

    assert response.status_code == 422


# ------------------------------------------------------------- capability ----


async def test_the_capability_check_agrees_with_actually_calling_reveal() -> None:
    """The one test that keeps the 501 honest.

    `supports_reveal` answers by asking whether an adapter overrides the interface
    default — exact, and with no second flag for a fourth adapter to forget to set.
    The risk in that kind of introspection is drifting from the behaviour it claims
    to describe, so this calls `reveal` on every registered provider and checks that
    "said no" and "would have said no" are the same set.

    The call is against a card id that exists nowhere, which is the point: an adapter
    that *can* reveal fails on the card, and one that cannot fails before looking.
    """
    from app.reveal.capability import supports_reveal

    for provider_id, _ in registry.describe():
        adapter = registry.get_adapter(provider_id)
        try:
            await adapter.reveal("card_that_exists_nowhere")
        except RevealUnsupported:
            refused = True
        except IssuerError:
            # Reached the provider and was turned away there — a capability it has.
            refused = False
        else:  # pragma: no cover -- no adapter can reveal a card that does not exist
            refused = False
        assert supports_reveal(adapter) is not refused, provider_id
