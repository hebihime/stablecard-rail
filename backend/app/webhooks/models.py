"""The dead-letter table (SPEC.md §4).

Where a delivery lands when every retry has failed. Two reasons it is a table and
not a Redis key: giving up on a provider event is an operational fact someone has
to be able to find later, and the stored event is enough to replay the handler by
hand once whatever broke is fixed.

One row per `(provider_id, event_id, handler)`: handlers fail independently, and a
second worker draining the same item must not produce a second row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class WebhookDeadLetter(Base):
    __tablename__ = "webhook_dead_letters"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The provider's event id — the same value dedup is keyed on.
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Which subscriber failed. Others may have succeeded for the same event.
    handler: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    #: The normalized event, JSON-encoded — enough to replay the handler.
    event: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_id", "event_id", "handler", name="uq_webhook_dead_letters_delivery"
        ),
        Index("ix_webhook_dead_letters_provider_id", "provider_id"),
        Index("ix_webhook_dead_letters_event_type", "event_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookDeadLetter id={self.id} handler={self.handler!r} event={self.event_id!r}>"
