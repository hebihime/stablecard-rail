"""Which card a deposit is for (SPEC.md §5.2 step 1).

A watcher sees money arrive at an address. Nothing in the transfer says what it
is *for* — the chain carries an amount and two accounts, and neither of them is a
card. So the link has to exist before the deposit does, and this is it: one row
per watched address, naming the card it funds.

**Two different addresses, and §3.4 conflated them.** That decision predicted this
index would be projected from the ledger's `card.created` events, which already
record a `deposit_address`. Building the watcher showed the prediction was wrong,
because those are not the same address:

| Address | Whose | What it is |
| --- | --- | --- |
| the **source** | ours | the Solana account the user sends to — what the watcher polls |
| the **destination** | the card's | the Safe a `CRYPTO_DEPOSIT` issuer credits |

`card.created` records the second one. The watcher needs the first, and no
provider knows it: it is a Solana account this service assigns (SPEC.md §8's demo
deposit keypair). Hence a row rather than a projection. See
docs/ARCHITECTURE.md §9.8.

**Only the route lives here.** Not the destination address, not the card's state,
not its balance — those belong to the provider, and §3.4's argument against a
local card table applies unchanged. The engine asks the adapter for the
destination when it submits a bridge order.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

__all__ = [
    "DepositRoute",
    "UnroutableDepositError",
    "register_route",
    "require_route",
    "route_for",
    "routes_for_card",
    "watched_addresses",
]

logger = logging.getLogger(__name__)


class UnroutableDepositError(Exception):
    """Money arrived at an address no card claims.

    Never silently discarded and never guessed at: an unroutable deposit is real
    money at a real address, and the honest answer is to record it and stop.
    """

    def __init__(self, chain: str, deposit_address: str) -> None:
        super().__init__(f"no card is registered for deposits to {deposit_address} on {chain}")
        self.chain = chain
        self.deposit_address = deposit_address


class DepositRoute(Base):
    """One watched address, and the card it funds."""

    __tablename__ = "deposit_routes"

    #: `(chain, deposit_address)` is the whole identity, so it is the key. That
    #: also makes "two cards funded by one address" unrepresentable rather than
    #: ambiguous — §7.6 worried about a many-to-one mapping needing a second
    #: discriminator, and one address per card is the cheaper answer.
    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    deposit_address: Mapped[str] = mapped_column(String(128), primary_key=True)

    #: Where the top-up lands. Both opaque strings, as everywhere else.
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    card_id: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The other direction is one-to-many — a card may be funded from more
        # than one chain — and the fund screen (SPEC.md §9.3) reads it that way.
        Index("ix_deposit_routes_card", "provider_id", "card_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DepositRoute {self.chain}:{self.deposit_address} -> {self.card_id}>"


async def register_route(
    session: AsyncSession,
    *,
    chain: str,
    deposit_address: str,
    provider_id: str,
    card_id: str,
) -> DepositRoute:
    """Point an address at a card. Flushes; the caller owns the commit.

    Re-registering the same address for the same card is a no-op, because the
    fund screen (SPEC.md §9.3) will do it every time it is opened. Pointing it at
    a *different* card is refused: money already in flight to that address was
    sent for the first one.
    """
    existing = await route_for(session, chain=chain, deposit_address=deposit_address)
    if existing is not None:
        if (existing.provider_id, existing.card_id) != (provider_id, card_id):
            raise ValueError(
                f"{deposit_address} on {chain} already funds "
                f"{existing.provider_id}:{existing.card_id}; a deposit already on its way "
                f"there was sent for that card, not for {provider_id}:{card_id}"
            )
        return existing

    route = DepositRoute(
        chain=chain,
        deposit_address=deposit_address,
        provider_id=provider_id,
        card_id=card_id,
    )
    session.add(route)
    await session.flush()
    logger.info(
        "registered deposit route %s:%s -> %s:%s", chain, deposit_address, provider_id, card_id
    )
    return route


async def route_for(
    session: AsyncSession, *, chain: str, deposit_address: str
) -> DepositRoute | None:
    result = await session.execute(
        select(DepositRoute).where(
            DepositRoute.chain == chain,
            DepositRoute.deposit_address == deposit_address,
        )
    )
    return result.scalar_one_or_none()


async def require_route(session: AsyncSession, *, chain: str, deposit_address: str) -> DepositRoute:
    """The route, or `UnroutableDepositError` naming the address."""
    route = await route_for(session, chain=chain, deposit_address=deposit_address)
    if route is None:
        raise UnroutableDepositError(chain, deposit_address)
    return route


async def watched_addresses(session: AsyncSession, *, chain: str) -> list[str]:
    """Every address on one chain that some card is waiting on.

    This table *is* the worker's list of what to watch: a route exists because
    somebody was told to send money to that address, so anything registered has
    to be polled and nothing else needs to be.
    """
    result = await session.execute(
        select(DepositRoute.deposit_address)
        .where(DepositRoute.chain == chain)
        .order_by(DepositRoute.deposit_address)
    )
    return list(result.scalars().all())


async def routes_for_card(
    session: AsyncSession, *, provider_id: str, card_id: str
) -> list[DepositRoute]:
    """Every address funding one card — what the fund screen shows.

    A list, not one: the mapping is one card to many addresses (a second chain
    would mean a second address), and only the other direction is unique.
    """
    result = await session.execute(
        select(DepositRoute)
        .where(DepositRoute.provider_id == provider_id, DepositRoute.card_id == card_id)
        .order_by(DepositRoute.chain, DepositRoute.deposit_address)
    )
    return list(result.scalars().all())
