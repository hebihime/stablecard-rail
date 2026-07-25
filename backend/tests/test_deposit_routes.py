"""Which card a deposit is for.

The chain says an amount and two accounts, and neither of them is a card. So the
link has to exist before the money does, and everything here is about the two
ways that goes wrong: no route at all, and a route pointed somewhere new while a
transfer is already on its way to the old destination.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.funding.routes import (
    UnroutableDepositError,
    register_route,
    require_route,
    route_for,
    routes_for_card,
)

SOLANA = "solana-devnet"
ADDRESS = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"


async def a_route(session: AsyncSession, **overrides: str) -> None:
    await register_route(
        session,
        chain=overrides.get("chain", SOLANA),
        deposit_address=overrides.get("deposit_address", ADDRESS),
        provider_id=overrides.get("provider_id", "gnosis_pay_mock"),
        card_id=overrides.get("card_id", "card_1"),
    )
    await session.commit()


async def test_an_address_points_at_exactly_one_card(session: AsyncSession) -> None:
    await a_route(session)

    route = await route_for(session, chain=SOLANA, deposit_address=ADDRESS)

    assert route is not None
    assert (route.provider_id, route.card_id) == ("gnosis_pay_mock", "card_1")


async def test_an_unknown_address_has_no_route(session: AsyncSession) -> None:
    assert await route_for(session, chain=SOLANA, deposit_address=ADDRESS) is None


async def test_requiring_a_known_route_returns_it(session: AsyncSession) -> None:
    await a_route(session)

    route = await require_route(session, chain=SOLANA, deposit_address=ADDRESS)

    assert route.card_id == "card_1"


async def test_requiring_an_unknown_route_names_the_address(session: AsyncSession) -> None:
    # An unroutable deposit is real money at a real address. The engine must be
    # able to say which one, in a message somebody can act on.
    with pytest.raises(UnroutableDepositError) as caught:
        await require_route(session, chain=SOLANA, deposit_address=ADDRESS)

    assert ADDRESS in str(caught.value)
    assert caught.value.deposit_address == ADDRESS
    assert caught.value.chain == SOLANA


async def test_registering_the_same_route_twice_is_a_no_op(session: AsyncSession) -> None:
    # The fund screen registers on every open, so this happens constantly.
    await a_route(session)
    await a_route(session)

    assert len(await routes_for_card(session, provider_id="gnosis_pay_mock", card_id="card_1")) == 1


async def test_repointing_an_address_at_another_card_is_refused(session: AsyncSession) -> None:
    # The dangerous one. Money already in flight to this address was sent for the
    # first card; quietly re-pointing it credits the wrong one on arrival.
    await a_route(session)

    with pytest.raises(ValueError, match="already funds"):
        await register_route(
            session,
            chain=SOLANA,
            deposit_address=ADDRESS,
            provider_id="lithic",
            card_id="card_2",
        )


async def test_the_same_address_on_another_chain_is_a_different_route(
    session: AsyncSession,
) -> None:
    # Address formats collide across chains far more often than they should, and
    # an EVM address is valid on every EVM chain. The chain is part of the key.
    await a_route(session)
    await a_route(session, chain="gnosis-chiado", card_id="card_2")

    solana = await route_for(session, chain=SOLANA, deposit_address=ADDRESS)
    gnosis = await route_for(session, chain="gnosis-chiado", deposit_address=ADDRESS)

    assert solana is not None and solana.card_id == "card_1"
    assert gnosis is not None and gnosis.card_id == "card_2"


async def test_one_card_may_be_funded_from_several_addresses(session: AsyncSession) -> None:
    # One direction is unique and the other is not: a second chain means a second
    # address for the same card, and the fund screen lists them.
    await a_route(session)
    await a_route(session, chain="gnosis-chiado", deposit_address="0xSafe1")

    routes = await routes_for_card(session, provider_id="gnosis_pay_mock", card_id="card_1")

    assert [route.chain for route in routes] == ["gnosis-chiado", SOLANA]


async def test_a_card_with_no_addresses_lists_none(session: AsyncSession) -> None:
    assert await routes_for_card(session, provider_id="lithic", card_id="card_9") == []


async def test_registering_does_not_commit(session: AsyncSession) -> None:
    # Same contract as the ledger writer and the chain cursor: the caller owns
    # the transaction, so a route can be registered alongside the work that
    # needed it.
    await register_route(
        session,
        chain=SOLANA,
        deposit_address=ADDRESS,
        provider_id="gnosis_pay_mock",
        card_id="card_1",
    )
    await session.rollback()

    assert await route_for(session, chain=SOLANA, deposit_address=ADDRESS) is None
