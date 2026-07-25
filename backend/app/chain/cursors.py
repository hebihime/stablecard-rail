"""Where a chain watcher got to, persisted (SPEC.md §5.2).

A watcher that starts from the beginning of an account's history on every restart
is a watcher that re-processes every deposit; one that starts from *now* misses
whatever arrived while it was down. So the position is a row.

**The cursor moves after the work, never before.** The caller creates the funding
intents, and only then advances the cursor — in that order, so a crash in between
re-observes deposits rather than losing them. Re-observing is safe because
`funding_intents.deposit_tx_ref` is unique: the second attempt to open an intent
for the same transaction is refused by the database. At-least-once delivery plus
a unique key is exactly-once effect, which is the same bargain the webhook
receiver makes (docs/ARCHITECTURE.md §2.4).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

__all__ = ["ChainCursor", "advance_cursor", "cursor_key", "load_cursor"]

logger = logging.getLogger(__name__)


class ChainCursor(Base):
    """One watcher's position on one chain."""

    __tablename__ = "chain_cursors"

    #: `<chain>:<address>` — see `cursor_key()`. A natural key rather than a
    #: surrogate id: there is exactly one position per watched address, and the
    #: primary key saying so is cheaper than a unique constraint that says it.
    key: Mapped[str] = mapped_column(String(160), primary_key=True)

    #: The newest signature already processed. Passed back to the node as
    #: `until`, so a poll asks only for what is new. `None` before the first one.
    last_signature: Mapped[str | None] = mapped_column(String(128))
    #: The slot that signature landed in. Not used to resume — `until` is — but
    #: it is what makes a stalled watcher legible in an operational sense.
    last_slot: Mapped[int | None] = mapped_column(BigInteger)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChainCursor {self.key} slot={self.last_slot}>"


def cursor_key(chain: str, address: str) -> str:
    """`solana-devnet:GXGc5RJU…` — one position per watched address."""
    return f"{chain}:{address}"


async def load_cursor(session: AsyncSession, key: str) -> ChainCursor | None:
    result = await session.execute(select(ChainCursor).where(ChainCursor.key == key))
    return result.scalar_one_or_none()


async def advance_cursor(
    session: AsyncSession, key: str, *, signature: str, slot: int
) -> ChainCursor:
    """Move a cursor forward. Flushes; the caller owns the commit.

    Refuses to move backwards. Nothing in the polling loop should ever try — the
    node returns signatures newest-first and the watcher processes them oldest-
    first — but a cursor that can rewind is a cursor that can re-credit a card,
    so it is cheaper to make that impossible than to reason about it.
    """
    cursor = await load_cursor(session, key)
    if cursor is None:
        cursor = ChainCursor(key=key, last_signature=signature, last_slot=slot)
        session.add(cursor)
        await session.flush()
        return cursor

    if cursor.last_slot is not None and slot < cursor.last_slot:
        logger.warning(
            "refusing to rewind cursor %s from slot %s to %s", key, cursor.last_slot, slot
        )
        return cursor

    cursor.last_signature = signature
    cursor.last_slot = slot
    await session.flush()
    return cursor
