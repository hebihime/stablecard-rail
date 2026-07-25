"""Chain watcher cursors.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chain_cursors",
        # `<chain>:<address>` — one position per watched address, so the natural
        # key is the key. A surrogate id would need a unique constraint saying
        # exactly this.
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("last_signature", sa.String(length=128), nullable=True),
        sa.Column("last_slot", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            # clock_timestamp() is the actual write time; now() is transaction start.
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_chain_cursors"),
    )


def downgrade() -> None:
    op.drop_table("chain_cursors")
