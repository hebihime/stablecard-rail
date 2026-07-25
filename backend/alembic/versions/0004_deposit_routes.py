"""Deposit routes: which card an address funds.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deposit_routes",
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("deposit_address", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("card_id", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            # clock_timestamp() is the actual write time; now() is transaction start.
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        # The address is the identity, so two cards funded by one address is
        # unrepresentable rather than ambiguous.
        sa.PrimaryKeyConstraint("chain", "deposit_address", name="pk_deposit_routes"),
    )
    # The other direction is one-to-many — a card can be funded from more than one
    # chain — and the fund screen reads it that way.
    op.create_index(
        "ix_deposit_routes_card",
        "deposit_routes",
        ["provider_id", "card_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deposit_routes_card", table_name="deposit_routes")
    op.drop_table("deposit_routes")
