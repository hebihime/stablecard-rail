"""Reconciling a provider's settlement against a funding intent (SPEC.md §5.2 step 4).

The first consumer on the `EventBus`. Phase 2 built the pipe — verify, dedup,
parse, ledger, dispatch — and left the subscriptions for the phases that own them
(docs/ARCHITECTURE.md §3.10). This is funding's.

**It attributes on `funding_ref` and on nothing else.** A `CardEvent` reaches here
having been normalized by an adapter, and `funding_ref` is the field that exists
for exactly this: "links a settlement back to the funding intent that caused it".
When it is absent, the event is not attributed at all.

That is a deliberate refusal, and it overrules a note phase 4 left behind. The
mock adapter's `parse_webhook` says of its own settlements:

    Their transactions carry no reference to our funding intent: money arrives
    on-chain, so a settlement cannot echo a `funding_ref`. Phase 5 reconciles on
    the card and the amount instead.

Phase 5 does not, because at every provider here `SETTLEMENT` is overwhelmingly a
**purchase** clearing rather than a funding landing. Matching on card and amount
would let a $25 coffee settle a $25 top-up, which is a false reconciliation — a
worse outcome than no reconciliation, because it is silent and it is wrong.
An intent resting at `FUNDED` is accurate: the provider said the card has the
money. See §9.12 for what would make `SETTLED` reachable.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.funding.machine import IntentNotFoundError, advance, get_intent
from app.funding.states import FundingState
from app.issuers.base import CardEvent, CardEventType
from app.webhooks.dispatch import Handler, subscribe

__all__ = ["HANDLER_NAME", "make_settlement_handler", "settle_from_event", "subscribe_settlement"]

logger = logging.getLogger(__name__)

#: How the retry queue refers to this handler. Stable across deploys: renaming it
#: strands whatever retries are queued under the old name (see `dispatch.py`).
HANDLER_NAME = "funding.settle"


async def settle_from_event(session: AsyncSession, event: CardEvent) -> uuid.UUID | None:
    """Advance the intent this settlement belongs to, if it names one.

    Returns the intent id it settled, or `None` when the event could not be
    attributed — which is the common case and not an error.

    Raises whatever `advance()` raises. A settlement for an intent that is not
    `FUNDED` is an `IllegalTransitionError`, which is ledgered and then travels
    the handler-failure path: retried with backoff, then dead-lettered (§3.7).
    Something is genuinely wrong, and losing it quietly would be worse.
    """
    if event.funding_ref is None:
        # Overwhelmingly a purchase clearing. The receiver has already ledgered
        # it as a provider event; there is no funding here to reconcile.
        logger.debug(
            "settlement %s from %s names no funding intent", event.event_id, event.provider_id
        )
        return None

    try:
        intent_id = uuid.UUID(event.funding_ref)
    except ValueError:
        # Our own reference came back malformed. Not something to guess at.
        logger.warning(
            "settlement %s carries an unparseable funding_ref %r",
            event.event_id,
            event.funding_ref,
        )
        return None

    try:
        intent = await get_intent(session, intent_id)
    except IntentNotFoundError:
        logger.warning(
            "settlement %s names funding intent %s, which does not exist",
            event.event_id,
            intent_id,
        )
        return None

    if intent.state is FundingState.SETTLED:
        # Already reconciled, by an earlier delivery of this or another event.
        return intent.id

    await advance(
        session,
        intent.id,
        FundingState.SETTLED,
        payload={
            "provider_event_id": event.event_id,
            "provider_event_type": event.provider_event_type,
            "settled_minor": event.amount.amount_minor if event.amount else None,
        },
        # Keyed on the intent rather than the delivery: a second settlement for
        # one funding — a replay, or a provider sending two — is a no-op, not an
        # illegal transition out of a terminal state.
        idempotency_key=f"intent:{intent.id}:{FundingState.SETTLED}",
    )
    logger.info("funding intent %s settled by %s", intent.id, event.event_id)
    return intent.id


def make_settlement_handler(sessionmaker: async_sessionmaker[AsyncSession]) -> Handler:
    """A handler with a session of its own.

    `Handler` takes only the event (that is what keeps `webhooks/` free of the
    database), so the consumer opens its own session per event — the same shape a
    Kafka consumer would have.
    """

    async def handle(event: CardEvent) -> None:
        async with sessionmaker() as session:
            await settle_from_event(session, event)

    return handle


def subscribe_settlement(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Register the consumer. Called by the composition root, not at import."""
    # idempotency key: (intent_id, "SETTLED") — `advance()` dedups on it, so a
    # replayed delivery and a second settlement for the same funding both
    # collapse to a no-op rather than raising out of a terminal state.
    subscribe(CardEventType.SETTLEMENT, HANDLER_NAME, make_settlement_handler(sessionmaker))
