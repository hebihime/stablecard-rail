"""Webhook dead letters.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_dead_letters",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("handler", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("event", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            # clock_timestamp() is the actual insert time; now() is transaction start.
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_dead_letters"),
        # One row per (delivery, handler): handlers fail independently, and a
        # second worker reaching the same conclusion must not add a second row.
        sa.UniqueConstraint(
            "provider_id", "event_id", "handler", name="uq_webhook_dead_letters_delivery"
        ),
    )
    op.create_index(
        "ix_webhook_dead_letters_provider_id", "webhook_dead_letters", ["provider_id"]
    )
    op.create_index("ix_webhook_dead_letters_event_type", "webhook_dead_letters", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_webhook_dead_letters_event_type", table_name="webhook_dead_letters")
    op.drop_index("ix_webhook_dead_letters_provider_id", table_name="webhook_dead_letters")
    op.drop_table("webhook_dead_letters")
