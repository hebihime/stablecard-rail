"""Ledger events and funding intents.

Revision ID: 0001
Revises:
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum values are spelled out rather than imported from the application: a
# migration records the schema as it was at this revision and must not change
# when the Python enum grows.
FUNDING_STATES = (
    "PENDING",
    "DEPOSIT_CONFIRMED",
    "BRIDGING",
    "BRIDGED",
    "FUNDING",
    "FUNDED",
    "SETTLED",
    "FAILED_DEPOSIT",
    "FAILED_BRIDGE",
    "FAILED_FUNDING",
    "FAILED_SETTLEMENT",
)

# The ledger is evidence, so append-only is enforced by the database rather than
# by application discipline. TRUNCATE is intentionally still permitted: it does
# not fire row-level triggers, which is what lets the test suite reset cheaply.
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION ledger_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'ledger_events is append-only: % is not permitted on this table', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER ledger_events_append_only
BEFORE UPDATE OR DELETE ON ledger_events
FOR EACH ROW EXECUTE FUNCTION ledger_events_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "funding_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                *FUNDING_STATES,
                name="ck_funding_intents_state",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("card_id", sa.String(length=128), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("deposit_tx_ref", sa.String(length=128), nullable=True),
        sa.Column("bridge_ref", sa.String(length=128), nullable=True),
        sa.Column("issuer_funding_ref", sa.String(length=128), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_funding_intents_amount_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_funding_intents"),
        sa.UniqueConstraint("deposit_tx_ref", name="uq_funding_intents_deposit_tx_ref"),
    )
    op.create_index("ix_funding_intents_state", "funding_intents", ["state"])
    op.create_index("ix_funding_intents_card_id", "funding_intents", ["card_id"])
    op.create_index("ix_funding_intents_provider_id", "funding_intents", ["provider_id"])
    op.create_index(
        "ix_funding_intents_state_changed_at",
        "funding_intents",
        ["state", "state_changed_at"],
    )

    op.create_table(
        "ledger_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            # clock_timestamp() is the actual insert time; now() is transaction start.
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=True),
        sa.Column("cardholder_id", sa.String(length=128), nullable=True),
        sa.Column("card_id", sa.String(length=128), nullable=True),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state_before", sa.String(length=32), nullable=True),
        sa.Column("state_after", sa.String(length=32), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_events"),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_events_idempotency_key"),
    )
    op.create_index("ix_ledger_events_card_id", "ledger_events", ["card_id"])
    op.create_index("ix_ledger_events_intent_id", "ledger_events", ["intent_id"])
    op.create_index("ix_ledger_events_event_type", "ledger_events", ["event_type"])
    op.create_index("ix_ledger_events_occurred_at", "ledger_events", ["occurred_at"])

    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_events_append_only ON ledger_events")
    op.execute("DROP FUNCTION IF EXISTS ledger_events_append_only()")

    op.drop_index("ix_ledger_events_occurred_at", table_name="ledger_events")
    op.drop_index("ix_ledger_events_event_type", table_name="ledger_events")
    op.drop_index("ix_ledger_events_intent_id", table_name="ledger_events")
    op.drop_index("ix_ledger_events_card_id", table_name="ledger_events")
    op.drop_table("ledger_events")

    op.drop_index("ix_funding_intents_state_changed_at", table_name="funding_intents")
    op.drop_index("ix_funding_intents_provider_id", table_name="funding_intents")
    op.drop_index("ix_funding_intents_card_id", table_name="funding_intents")
    op.drop_index("ix_funding_intents_state", table_name="funding_intents")
    op.drop_table("funding_intents")
