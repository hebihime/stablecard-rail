"""The funding intent row — one per top-up attempt (SPEC.md §5.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.money import Money
from app.funding.states import FundingState


class FundingIntent(Base):
    """A single deposit-to-card top-up, tracked through the state machine.

    Written *only* by `app.funding.machine`. The id doubles as the idempotent
    `funding_ref` handed to issuer adapters (SPEC.md §5.2 step 3), which is why it
    is a UUID: opaque, client-safe, and generated before any external call.
    """

    __tablename__ = "funding_intents"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    state: Mapped[FundingState] = mapped_column(
        # native_enum=False -> VARCHAR + CHECK. Adding a state is then an ordinary
        # migration instead of an ALTER TYPE that cannot run in a transaction.
        Enum(
            FundingState,
            name="ck_funding_intents_state",
            native_enum=False,
            length=32,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )

    #: Issuer that will be funded. Matches an `issuers/` registry key.
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Provider's opaque card identifier.
    card_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: What was deposited. Immutable: it is the record of what arrived on the
    #: source chain, and nothing downstream may edit history.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    #: What the bridge delivered, net of its fee (SPEC.md §11). `None` until the
    #: transfer completes. A separate column rather than an overwrite of
    #: `amount_minor`, so the fee is the difference between two recorded numbers
    #: instead of a change nobody can see afterwards.
    bridged_amount_minor: Mapped[int | None] = mapped_column(BigInteger)

    #: Source chain transaction reference. Unique, so a replayed deposit
    #: notification can only ever produce one intent.
    deposit_tx_ref: Mapped[str | None] = mapped_column(String(128))
    #: Bridge order/transfer reference, once submitted.
    bridge_ref: Mapped[str | None] = mapped_column(String(128))
    #: Issuer's own reference for the completed funding.
    issuer_funding_ref: Mapped[str | None] = mapped_column(String(128))

    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Reason attached to the most recent retry or failure.
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    #: When the intent last changed state — the reconciler's staleness clock.
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("deposit_tx_ref", name="uq_funding_intents_deposit_tx_ref"),
        CheckConstraint("amount_minor > 0", name="ck_funding_intents_amount_positive"),
        Index("ix_funding_intents_state", "state"),
        Index("ix_funding_intents_card_id", "card_id"),
        Index("ix_funding_intents_provider_id", "provider_id"),
        # Supports the reconciler's "stuck intents" scan (SPEC.md §5.3).
        Index("ix_funding_intents_state_changed_at", "state", "state_changed_at"),
    )

    @property
    def money(self) -> Money:
        """What was deposited."""
        return Money(self.amount_minor, self.currency)

    @property
    def fundable_money(self) -> Money:
        """What the card can actually be funded with.

        The bridged amount once there is one, because a card funded with the
        deposited amount is a card funded with the bridge's fee as well.
        """
        if self.bridged_amount_minor is None:
            return self.money
        return Money(self.bridged_amount_minor, self.currency)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FundingIntent {self.id} {self.state} {self.money}>"
