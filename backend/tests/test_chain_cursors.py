"""Where the watcher got to, and why it may only ever move forward.

The cursor is the difference between "re-process every deposit this account has
ever received" and "miss whatever arrived while the process was down". It is also
the one piece of watcher state that can, if it moves wrongly, credit a card twice
— hence the rewind guard.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.cursors import advance_cursor, cursor_key, load_cursor
from app.chain.solana_watcher import SOLANA_DEVNET

KEY = cursor_key(SOLANA_DEVNET, "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU")


def test_a_key_names_one_address_on_one_chain() -> None:
    # Two watchers on the same address on different chains are different
    # positions, and the same address is the obvious way to collide them.
    assert cursor_key("solana-devnet", "abc") == "solana-devnet:abc"
    assert cursor_key("solana-devnet", "abc") != cursor_key("solana-mainnet", "abc")


async def test_an_unseen_address_has_no_cursor(session: AsyncSession) -> None:
    # `None` rather than a zero row: "we have never looked" and "we looked and
    # found nothing" are different, and only the first should start from scratch.
    assert await load_cursor(session, KEY) is None


async def test_the_first_advance_creates_the_position(session: AsyncSession) -> None:
    await advance_cursor(session, KEY, signature="sig-1", slot=100)
    await session.commit()

    cursor = await load_cursor(session, KEY)

    assert cursor is not None
    assert cursor.last_signature == "sig-1"
    assert cursor.last_slot == 100


async def test_advancing_moves_it_forward(session: AsyncSession) -> None:
    await advance_cursor(session, KEY, signature="sig-1", slot=100)
    await advance_cursor(session, KEY, signature="sig-2", slot=250)
    await session.commit()

    cursor = await load_cursor(session, KEY)

    assert cursor is not None
    assert cursor.last_signature == "sig-2"
    assert cursor.last_slot == 250


async def test_a_cursor_will_not_rewind(session: AsyncSession) -> None:
    # Nothing in the polling loop should try — the node lists newest-first and
    # the watcher processes oldest-first — but a cursor that can rewind is a
    # cursor that can re-credit a card, and making that impossible is cheaper
    # than reasoning about every caller.
    await advance_cursor(session, KEY, signature="sig-2", slot=250)
    await advance_cursor(session, KEY, signature="sig-1", slot=100)
    await session.commit()

    cursor = await load_cursor(session, KEY)

    assert cursor is not None
    assert cursor.last_signature == "sig-2"
    assert cursor.last_slot == 250


async def test_two_addresses_keep_separate_positions(session: AsyncSession) -> None:
    other = cursor_key(SOLANA_DEVNET, "6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG")

    await advance_cursor(session, KEY, signature="sig-1", slot=100)
    await advance_cursor(session, other, signature="sig-9", slot=900)
    await session.commit()

    first = await load_cursor(session, KEY)
    second = await load_cursor(session, other)

    assert first is not None and first.last_slot == 100
    assert second is not None and second.last_slot == 900


async def test_advancing_does_not_commit(session: AsyncSession) -> None:
    # The caller owns the transaction, so the cursor can move in the *same*
    # transaction as the work it accounts for. `record()` in the ledger makes the
    # same promise for the same reason.
    await advance_cursor(session, KEY, signature="sig-1", slot=100)
    await session.rollback()

    assert await load_cursor(session, KEY) is None
