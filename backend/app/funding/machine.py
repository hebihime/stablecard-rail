"""The funding state machine — the only writer of `funding_intents`.

`advance()` is the single entry point for state change (SPEC.md §5.1). It:

1. short-circuits replays, keyed by `idempotency_key`;
2. takes a row lock, so two workers cannot both apply the same hop;
3. enforces the legal-transition table, ledgering and raising on a violation;
4. writes exactly one ledger entry per transition, in the same transaction as the
   state change.

**Transaction contract:** `advance()` and `create_intent()` own their transaction
and commit before returning. A transition is the unit of work; callers should not
batch unrelated writes into the same session, because the illegal-transition path
commits its audit record before raising.

**Ordering note:** the replay check runs *before* the legality check. Under
at-least-once webhook delivery the second copy of an already-applied event would
otherwise look like an illegal repeat of a completed hop and turn into a spurious
error. A known key means "already done", which is a success.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.funding.models import FundingIntent
from app.funding.states import RETRYABLE_STATES, FundingState, legal_targets
from app.ledger import event_types
from app.ledger.writer import find_by_idempotency_key, record

__all__ = [
    "MUTABLE_INTENT_FIELDS",
    "FundingError",
    "IllegalTransitionError",
    "IntentNotFoundError",
    "advance",
    "create_intent",
    "get_intent",
]

logger = logging.getLogger(__name__)

#: External references a caller may set while advancing. Everything else about an
#: intent — state, amount, identity — is either immutable or machine-owned.
MUTABLE_INTENT_FIELDS = frozenset({"deposit_tx_ref", "bridge_ref", "issuer_funding_ref"})

_IDEMPOTENCY_CONSTRAINT = "uq_ledger_events_idempotency_key"


class FundingError(Exception):
    """Base class for funding-machine errors."""


class IntentNotFoundError(FundingError):
    def __init__(self, intent_id: uuid.UUID) -> None:
        super().__init__(f"funding intent {intent_id} does not exist")
        self.intent_id = intent_id


class IllegalTransitionError(FundingError):
    """Raised when a caller attempts a transition outside the table."""

    def __init__(self, frm: FundingState, to: FundingState, intent_id: uuid.UUID) -> None:
        super().__init__(
            f"illegal funding transition {frm} -> {to} for intent {intent_id}; "
            f"legal targets: {sorted(legal_targets(frm)) or 'none (terminal state)'}"
        )
        self.from_state = frm
        self.to_state = to
        self.intent_id = intent_id


async def create_intent(
    session: AsyncSession,
    *,
    provider_id: str,
    card_id: str,
    amount: Money,
    deposit_tx_ref: str | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> FundingIntent:
    """Open a new intent in `PENDING` and ledger its creation. Commits."""
    if amount.amount_minor <= 0:
        raise ValueError(f"funding amount must be positive, got {amount}")

    now = datetime.now(UTC)
    intent = FundingIntent(
        state=FundingState.PENDING,
        provider_id=provider_id,
        card_id=card_id,
        amount_minor=amount.amount_minor,
        currency=amount.currency,
        deposit_tx_ref=deposit_tx_ref,
        retry_count=0,
        state_changed_at=now,
    )
    session.add(intent)
    # Flush before ledgering so the intent id (and the deposit_tx_ref uniqueness
    # violation, if any) surfaces here rather than at commit.
    await session.flush()

    await record(
        session,
        event_type=event_types.INTENT_CREATED,
        occurred_at=now,
        provider_id=provider_id,
        card_id=card_id,
        intent_id=intent.id,
        state_before=None,
        state_after=FundingState.PENDING,
        amount=amount,
        payload={"deposit_tx_ref": deposit_tx_ref, **(dict(payload) if payload else {})},
        idempotency_key=idempotency_key,
    )
    await session.commit()
    logger.info("funding intent created id=%s provider=%s card=%s", intent.id, provider_id, card_id)
    return intent


async def get_intent(session: AsyncSession, intent_id: uuid.UUID) -> FundingIntent:
    intent = await _load(session, intent_id, for_update=False)
    return intent


async def advance(
    session: AsyncSession,
    intent_id: uuid.UUID,
    to_state: FundingState,
    *,
    reason: str | None = None,
    updates: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> FundingIntent:
    """Move an intent to `to_state`, enforcing the transition table. Commits.

    Args:
        reason: human-readable cause; stored on the intent for retries and
            failures, and always recorded in the ledger entry.
        updates: external references to set alongside the transition, restricted
            to `MUTABLE_INTENT_FIELDS`.
        payload: extra context for the ledger entry only.
        idempotency_key: makes the call replay-safe. A repeat with the same key
            is a no-op returning the intent's current state.

    Raises:
        IntentNotFoundError: no such intent.
        IllegalTransitionError: transition absent from the table. The attempt is
            ledgered and committed before the raise.
        ValueError: blank idempotency key, or an update outside the allowlist.
    """
    if idempotency_key is not None and not idempotency_key.strip():
        raise ValueError("idempotency_key must be a non-empty string, or None to opt out")
    if updates:
        forbidden = sorted(set(updates) - MUTABLE_INTENT_FIELDS)
        if forbidden:
            raise ValueError(
                f"fields not updatable via advance(): {forbidden}; "
                f"allowed: {sorted(MUTABLE_INTENT_FIELDS)}"
            )

    if idempotency_key is not None:
        replayed = await find_by_idempotency_key(session, idempotency_key)
        if replayed is not None:
            logger.info("ignoring replayed transition intent=%s key=%s", intent_id, idempotency_key)
            return await _load(session, intent_id, for_update=False)

    intent = await _load(session, intent_id, for_update=True)
    frm = intent.state
    now = datetime.now(UTC)

    if to_state not in legal_targets(frm):
        await _ledger_illegal_attempt(session, intent, to_state, now, reason)
        await session.commit()
        raise IllegalTransitionError(frm, to_state, intent.id)

    is_retry = to_state is frm
    intent.state = to_state
    intent.state_changed_at = now
    if is_retry:
        intent.retry_count += 1
    if reason is not None and (is_retry or to_state.is_failure):
        intent.last_error = reason
    for field, value in (updates or {}).items():
        setattr(intent, field, value)

    entry_payload: dict[str, Any] = {"reason": reason, "retry_count": intent.retry_count}
    if updates:
        entry_payload["updates"] = dict(updates)
    if payload:
        entry_payload["context"] = dict(payload)

    try:
        # The insert and the commit share one `try`: the unique violation can
        # surface at either, depending on when the flush happens.
        await record(
            session,
            event_type=event_types.INTENT_RETRIED if is_retry else event_types.INTENT_TRANSITIONED,
            occurred_at=now,
            provider_id=intent.provider_id,
            card_id=intent.card_id,
            intent_id=intent.id,
            state_before=frm,
            state_after=to_state,
            amount=intent.money,
            payload=entry_payload,
            idempotency_key=idempotency_key,
        )
        await session.commit()
    except IntegrityError as exc:
        # Lost a race on the same idempotency key: the unique index is the durable
        # backstop for dedup once the Redis key has expired (SPEC.md §4). The
        # other writer applied the transition, so this call is a no-op.
        if idempotency_key is None or _IDEMPOTENCY_CONSTRAINT not in str(exc.orig):
            raise
        await session.rollback()
        logger.info("concurrent replay collapsed intent=%s key=%s", intent_id, idempotency_key)
        return await _load(session, intent_id, for_update=False)

    logger.info(
        "funding intent %s %s -> %s%s",
        intent.id,
        frm,
        to_state,
        f" (retry {intent.retry_count})" if is_retry else "",
    )
    return intent


async def _ledger_illegal_attempt(
    session: AsyncSession,
    intent: FundingIntent,
    to_state: FundingState,
    now: datetime,
    reason: str | None,
) -> None:
    await record(
        session,
        event_type=event_types.INTENT_ILLEGAL_TRANSITION,
        occurred_at=now,
        provider_id=intent.provider_id,
        card_id=intent.card_id,
        intent_id=intent.id,
        state_before=intent.state,
        state_after=to_state,
        amount=intent.money,
        payload={
            "reason": reason,
            "legal_targets": sorted(str(state) for state in legal_targets(intent.state)),
            "retryable": intent.state in RETRYABLE_STATES,
        },
    )
    logger.warning(
        "rejected illegal transition intent=%s %s -> %s", intent.id, intent.state, to_state
    )


async def _load(session: AsyncSession, intent_id: uuid.UUID, *, for_update: bool) -> FundingIntent:
    statement = select(FundingIntent).where(FundingIntent.id == intent_id)
    if for_update:
        statement = statement.with_for_update()
    # populate_existing: never read a stale copy out of the identity map — the
    # row lock is worthless if we then act on values fetched before it.
    result = await session.execute(statement.execution_options(populate_existing=True))
    intent = result.scalar_one_or_none()
    if intent is None:
        raise IntentNotFoundError(intent_id)
    return intent
