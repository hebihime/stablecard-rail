"""The amount that survived the bridge.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, and no backfill: `None` means "the bridge has not reported yet",
    # which is the truth for every intent that predates this column.
    op.add_column(
        "funding_intents",
        sa.Column("bridged_amount_minor", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("funding_intents", "bridged_amount_minor")
