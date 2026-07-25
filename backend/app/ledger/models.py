"""The append-only event ledger (SPEC.md §7).

Every card action, webhook delivery, state transition and OTP delivery lands
here. Two properties make it usable as evidence rather than as a log:

* **Append-only** — enforced by a database trigger, not by convention, so no
  application bug or console session can rewrite history.
* **Uniquely keyed** — `idempotency_key` is unique, which is what makes replayed
  webhook deliveries safe once Redis has evicted the short-lived dedup key
  (SPEC.md §4).

Entity references are stored as opaque values with no foreign keys: an audit
record must be writable even for an entity this service has never seen (an
unmapped provider event), and must outlive whatever it refers to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LedgerEvent(Base):
    __tablename__ = "ledger_events"

    # A monotonic identity column doubles as the total order of the ledger:
    # `occurred_at` can tie, and provider clocks are not ours to trust.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    #: When the event happened (provider timestamp where available). Always UTC.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When this service durably recorded it. Always UTC, set by the database.
    #: clock_timestamp(), not now(): now() is the *transaction* start time, which
    #: for a multi-event transaction would read as earlier than `occurred_at`.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64))

    # Opaque external identifiers.
    cardholder_id: Mapped[str | None] = mapped_column(String(128))
    card_id: Mapped[str | None] = mapped_column(String(128))
    intent_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # Free-form state strings rather than the funding enum: this column also
    # records card lifecycle states from issuer adapters.
    state_before: Mapped[str | None] = mapped_column(String(32))
    state_after: Mapped[str | None] = mapped_column(String(32))

    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))

    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_events_idempotency_key"),
        Index("ix_ledger_events_card_id", "card_id"),
        Index("ix_ledger_events_intent_id", "intent_id"),
        Index("ix_ledger_events_event_type", "event_type"),
        Index("ix_ledger_events_occurred_at", "occurred_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LedgerEvent id={self.id} type={self.event_type!r}>"
