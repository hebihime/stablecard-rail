"""The process that makes the pipeline autonomous (SPEC.md §5.2, §5.3).

Everything phase 5 built works when something calls it, and until this module
nothing did outside a demo script: an intent would be created by an HTTP request
and then sit there, because the watcher polls when asked and the engine steps when
asked. This is the caller.

One pass, in order:

1. **poll every watched address** — each route in `deposit_routes` is an address
   somebody was told to send money to, so the routes table *is* the list of what
   to watch;
2. **drive what is new** — intents the engine owns that have never been retried;
3. **reconcile the rest** — the stuck-scanner from §5.3, which paces retries with
   backoff and gives up at the cap.

**Why two stepping paths rather than one.** The reconciler is defined by state
*age* (SPEC.md §5.3: "state age > threshold"), and that threshold is minutes,
because it exists to notice silence. A freshly confirmed deposit should not wait
minutes to be handed to the bridge, so first attempts are driven promptly and
only *retries* are paced. A single mechanism would have had to be either slow for
new work or impatient with failing work.

The two paths cannot both step the same intent in one pass: the driver reports
what it touched and the reconciler is told to skip it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.solana_watcher import SolanaDepositWatcher
from app.core.config import Settings
from app.funding.deposits import DepositReport, collect_deposits
from app.funding.engine import StepOutcome, TopUpEngine
from app.funding.models import FundingIntent
from app.funding.reconciler import ReconcileReport, ReconcilerPolicy, reconcile
from app.funding.states import FundingState

__all__ = [
    "ENGINE_STATES",
    "PassReport",
    "WorkerPolicy",
    "find_ready",
    "run_pass",
]

logger = logging.getLogger(__name__)

#: The states the engine can act on. `PENDING` belongs to the watcher and
#: `FUNDED` to the settlement consumer, so neither is driven here.
ENGINE_STATES: tuple[FundingState, ...] = (
    FundingState.DEPOSIT_CONFIRMED,
    FundingState.BRIDGING,
    FundingState.BRIDGED,
    FundingState.FUNDING,
)


@dataclass(frozen=True, slots=True)
class WorkerPolicy:
    """How eagerly to drive work that has not failed yet."""

    #: How long a hop is left alone before its first attempt. Not zero: an intent
    #: created microseconds ago is about to be stepped by whichever process
    #: created it, and a worker racing that just contends on the row lock.
    first_attempt_after_seconds: float = 3.0
    #: Intents driven per pass, so one pass is bounded work.
    batch_limit: int = 50

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerPolicy:
        return cls(
            first_attempt_after_seconds=settings.worker_first_attempt_after_seconds,
            batch_limit=settings.reconciler_batch_limit,
        )


@dataclass(frozen=True, slots=True)
class PassReport:
    """What one pass did, in the order it did it."""

    deposits: tuple[DepositReport, ...] = ()
    driven: tuple[StepOutcome, ...] = ()
    reconciled: ReconcileReport = field(default_factory=ReconcileReport)

    @property
    def opened(self) -> int:
        return sum(len(report.opened) for report in self.deposits)

    @property
    def stepped(self) -> int:
        return len(self.driven) + len(self.reconciled.stepped)

    @property
    def did_anything(self) -> bool:
        return bool(self.opened or self.stepped)


async def find_ready(
    session: AsyncSession, *, now: datetime, policy: WorkerPolicy
) -> list[FundingIntent]:
    """Intents in the engine's states that have never been retried.

    `retry_count == 0` is the whole filter beyond age: the moment an intent
    retries once, its pacing becomes the reconciler's business and driving it
    here would spend the retry budget as fast as the loop runs.
    """
    cutoff = now - timedelta(seconds=policy.first_attempt_after_seconds)
    result = await session.execute(
        select(FundingIntent)
        .where(
            FundingIntent.state.in_(ENGINE_STATES),
            FundingIntent.retry_count == 0,
            FundingIntent.state_changed_at <= cutoff,
        )
        .order_by(FundingIntent.state_changed_at)
        .limit(policy.batch_limit)
    )
    return list(result.scalars().all())


async def run_pass(
    session: AsyncSession,
    engine: TopUpEngine,
    *,
    watchers: list[SolanaDepositWatcher],
    now: datetime,
    policy: WorkerPolicy | None = None,
    reconciler_policy: ReconcilerPolicy | None = None,
) -> PassReport:
    """Poll, drive, reconcile. Commits as it goes."""
    policy = policy or WorkerPolicy()
    deposits: list[DepositReport] = []
    driven: list[StepOutcome] = []

    for watcher in watchers:
        deposits.append(await collect_deposits(session, watcher))

    for intent in await find_ready(session, now=now, policy=policy):
        driven.append(await engine.step(session, intent.id))

    # The reconciler must not re-step what was just driven: an intent that moved
    # a moment ago is not stuck, and stepping it twice in one pass would poll the
    # same bridge twice and count the second answer as a retry.
    already = frozenset(outcome.intent_id for outcome in driven)
    reconciled = await reconcile(session, engine, now=now, policy=reconciler_policy, skip=already)

    report = PassReport(deposits=tuple(deposits), driven=tuple(driven), reconciled=reconciled)
    if report.did_anything:
        logger.info(
            "funding pass: %s deposits opened, %s driven, %s reconciled, %s waiting",
            report.opened,
            len(report.driven),
            len(reconciled.stepped),
            len(reconciled.waiting),
        )
    return report


def intent_ids(report: PassReport) -> tuple[uuid.UUID, ...]:
    """Every intent this pass touched, for a caller that wants to report on them."""
    return tuple(outcome.intent_id for outcome in (*report.driven, *report.reconciled.stepped))
