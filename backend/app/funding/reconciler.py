"""Self-healing: finding intents that stopped moving (SPEC.md §5.3).

Everything else in the pipeline is driven by an event — a deposit, a webhook, an
HTTP call. This is the part that runs when nothing happens, which is the failure
mode a funding pipeline actually has: not an error, but silence. A bridge that
accepted an order and went quiet, a `fund_card` whose reply was lost, a process
killed between two commits.

    scan for intents whose state has been unchanged too long
      -> step each one again through the engine
      -> which advances it, retries it, or fails it at the cap

**Backoff is elapsed time, not sleep.** SPEC.md §5.3 asks for retries with
backoff up to a cap, and there is no worker sitting on a timer here — an intent
simply is not eligible again until it has been quiet for
`stuck_after_seconds * 2**retry_count`, bounded by `max_backoff_seconds`. Since
every retry bumps `state_changed_at` and `retry_count` through `advance()`, the
spacing grows on its own and survives a restart, which a sleeping worker's would
not.

**Two states the scan will not touch**, both deliberately:

* `FUNDED` — it is waiting for a provider's settlement event, which at these
  three providers may never be attributable (§9.12). Failing an intent whose card
  *has the money* would turn a provider limitation into a fake incident.
* `PENDING` with no `deposit_tx_ref` — nobody has sent anything yet. There is
  nothing stuck; there is an invoice waiting to be paid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.funding.engine import StepOutcome, TopUpEngine
from app.funding.machine import advance
from app.funding.models import FundingIntent
from app.funding.states import FundingState

__all__ = [
    "RECONCILABLE_STATES",
    "ReconcilePlan",
    "ReconcileReport",
    "ReconcilerPolicy",
    "due_after",
    "find_stuck",
    "reconcile",
]

logger = logging.getLogger(__name__)

#: The states a stalled intent can be recovered from. The engine drives four of
#: them; `PENDING` is here only for the crash window between `create_intent()`
#: and its first transition, which no watcher poll will close (§9.13).
RECONCILABLE_STATES: tuple[FundingState, ...] = (
    FundingState.PENDING,
    FundingState.DEPOSIT_CONFIRMED,
    FundingState.BRIDGING,
    FundingState.BRIDGED,
    FundingState.FUNDING,
)


@dataclass(frozen=True, slots=True)
class ReconcilerPolicy:
    """Thresholds, all config-driven as SPEC.md §5.3 requires."""

    #: How long a state must be unchanged before the intent counts as stuck.
    stuck_after_seconds: int = 120
    #: Ceiling on the doubling, so a much-retried intent is still looked at.
    max_backoff_seconds: int = 3_600
    #: Intents examined per pass. A scan is a batch, not a queue drain.
    batch_limit: int = 50

    @classmethod
    def from_settings(cls, settings: Settings) -> ReconcilerPolicy:
        return cls(
            stuck_after_seconds=settings.reconciler_stuck_after_seconds,
            max_backoff_seconds=settings.reconciler_max_backoff_seconds,
            batch_limit=settings.reconciler_batch_limit,
        )


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    """One intent's eligibility, so a report can explain a quiet pass."""

    intent_id: object
    state: FundingState
    retry_count: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    stepped: tuple[StepOutcome, ...] = ()
    #: Stuck, but still inside its backoff window. Not idle — waiting.
    waiting: tuple[ReconcilePlan, ...] = ()

    @property
    def examined(self) -> int:
        return len(self.stepped) + len(self.waiting)


def due_after(intent: FundingIntent, policy: ReconcilerPolicy) -> datetime:
    """When this intent may be stepped again.

    Doubles per retry from `stuck_after_seconds`, capped. The exponent is the
    intent's own `retry_count`, so the schedule is a property of the row rather
    than of whichever worker happens to be running.
    """
    delay = min(
        policy.stuck_after_seconds * (2**intent.retry_count),
        policy.max_backoff_seconds,
    )
    return intent.state_changed_at + timedelta(seconds=delay)


async def find_stuck(
    session: AsyncSession,
    *,
    now: datetime,
    policy: ReconcilerPolicy,
) -> list[FundingIntent]:
    """Candidate intents, oldest first.

    The query filters on the *base* threshold and the exponential part is applied
    in Python. Expressing the doubling in SQL would let the database do the whole
    job, at the cost of a `POWER(2, retry_count)` in an index predicate that no
    index can serve — and the rows filtered out here are a handful of retried
    intents, not a scan of the table.
    """
    cutoff = now - timedelta(seconds=policy.stuck_after_seconds)
    result = await session.execute(
        select(FundingIntent)
        .where(
            FundingIntent.state.in_(RECONCILABLE_STATES),
            FundingIntent.state_changed_at <= cutoff,
        )
        # Oldest first: whatever has been stuck longest is looked at first, and
        # the batch limit therefore cannot starve it.
        .order_by(FundingIntent.state_changed_at)
        .limit(policy.batch_limit)
    )
    return list(result.scalars().all())


async def reconcile(
    session: AsyncSession,
    engine: TopUpEngine,
    *,
    now: datetime,
    policy: ReconcilerPolicy | None = None,
) -> ReconcileReport:
    """One pass: step every intent that is stuck and out of backoff. Commits."""
    policy = policy or ReconcilerPolicy()
    stepped: list[StepOutcome] = []
    waiting: list[ReconcilePlan] = []

    for intent in await find_stuck(session, now=now, policy=policy):
        due = due_after(intent, policy)
        if now < due:
            waiting.append(
                ReconcilePlan(
                    intent_id=intent.id,
                    state=intent.state,
                    retry_count=intent.retry_count,
                    due_at=due,
                )
            )
            continue

        if intent.state is FundingState.PENDING:
            outcome = await _confirm_stalled_deposit(session, intent)
            if outcome is None:
                continue
            stepped.append(outcome)
            continue

        logger.info(
            "reconciling intent %s, stuck in %s since %s",
            intent.id,
            intent.state,
            intent.state_changed_at,
        )
        stepped.append(await engine.step(session, intent.id))

    return ReconcileReport(stepped=tuple(stepped), waiting=tuple(waiting))


async def _confirm_stalled_deposit(
    session: AsyncSession, intent: FundingIntent
) -> StepOutcome | None:
    """Close the one window a watcher poll cannot (§9.13).

    An intent created for a finalized deposit, whose first transition never
    committed, is invisible to the watcher forever after: the deposit *has* an
    intent, so the next poll records it as a duplicate and moves on. The
    `deposit_tx_ref` is the evidence — the watcher only ever reports `finalized`
    transfers — so confirming it here is recovering a fact, not inventing one.

    An intent with no deposit reference is not stuck. Nobody has sent anything.
    """
    if intent.deposit_tx_ref is None:
        return None

    moved = await advance(
        session,
        intent.id,
        FundingState.DEPOSIT_CONFIRMED,
        reason=f"reconciler confirmed deposit {intent.deposit_tx_ref} after a stalled intake",
        idempotency_key=f"deposit-confirmed:{intent.deposit_tx_ref}",
    )
    return StepOutcome(
        intent_id=intent.id,
        from_state=FundingState.PENDING,
        to_state=moved.state,
        detail="confirmed a deposit whose intake stalled",
    )
