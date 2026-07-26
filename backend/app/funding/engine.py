"""The auto top-up engine (SPEC.md §5.2 steps 2-3).

One intent, one hop, one call to an outside system. `step()` looks at where an
intent is, does the single thing that state is waiting for, and hands the result
to `advance()` — which is still the only writer of funding state. The engine
decides *what* to attempt and *whether a failure is worth repeating*; the
transition table decides whether the hop is legal, and the ledger records it
either way (docs/ARCHITECTURE.md §9).

    DEPOSIT_CONFIRMED  submit a bridge order          -> BRIDGING
    BRIDGING           ask the bridge about it        -> BRIDGED | retry | FAILED_BRIDGE
    BRIDGED            call fund_card                 -> FUNDING -> FUNDED | retry | FAILED
    FUNDING            call fund_card again           -> FUNDED  | retry | FAILED_FUNDING

The two states the engine leaves alone are as deliberate as the four it drives.
`PENDING` belongs to the watcher: only a confirmed deposit may move it. `FUNDED`
belongs to the settlement consumer: only the provider can say a funding settled,
and it says so by webhook.

**Three outcomes for a failure, not two** (§9.1):

* an `ExternalError` marked retryable — retry in place, up to the cap, then
  `FAILED_*` with the reason;
* an `ExternalError` not marked retryable — `FAILED_*` immediately;
* anything else — *propagate*, leaving the intent exactly where it was. An
  `AttributeError` from a bad deploy is not evidence that a funding failed, and
  the reconciler will step the intent again once the deploy is fixed. Failing an
  intent is irreversible; leaving it alone is not.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.bridge.base import BridgeOrder, BridgeProvider, BridgeStatus
from app.core.config import Settings
from app.core.errors import ExternalError
from app.funding.machine import advance, get_intent
from app.funding.models import FundingIntent
from app.funding.states import FundingState
from app.issuers import registry
from app.issuers.base import CardIssuerAdapter, FundingModel, FundingStatus

__all__ = ["StepOutcome", "TopUpEngine", "TopUpPolicy"]

logger = logging.getLogger(__name__)

#: Which failure state each stage falls into when it gives up.
_FAILURE_FOR: dict[FundingState, FundingState] = {
    FundingState.DEPOSIT_CONFIRMED: FundingState.FAILED_BRIDGE,
    FundingState.BRIDGING: FundingState.FAILED_BRIDGE,
    FundingState.BRIDGED: FundingState.FAILED_FUNDING,
    FundingState.FUNDING: FundingState.FAILED_FUNDING,
}


@dataclass(frozen=True, slots=True)
class TopUpPolicy:
    """Everything the engine needs to know that is not about one intent.

    A value object rather than a settings read, so a test — or a second worker
    with a different route — constructs one directly. `from_settings()` is the
    only place the environment is consulted.
    """

    #: Self-transitions allowed per state before the intent is failed
    #: (SPEC.md §5.3's cap). Counted on the intent, so it survives a restart.
    max_retries: int = 5
    source_chain: str = "solana-devnet"
    destination_chain: str = "gnosis-chiado"
    #: Where a bridge order is delivered when the issuer is a `FIAT_RAIL` and so
    #: has no on-chain address of its own (§9.3). Empty means "not configured",
    #: which fails the intent rather than sending money nowhere.
    settlement_address: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> TopUpPolicy:
        return cls(
            max_retries=settings.funding_max_retries,
            source_chain=settings.funding_source_chain,
            destination_chain=settings.funding_destination_chain,
            settlement_address=settings.funding_settlement_address,
        )


@dataclass(frozen=True, slots=True)
class StepOutcome:
    intent_id: uuid.UUID
    from_state: FundingState
    to_state: FundingState
    detail: str

    @property
    def progressed(self) -> bool:
        """True when the intent actually moved on, as opposed to retried or idled."""
        return self.to_state is not self.from_state


class TopUpEngine:
    """Drives intents from a confirmed deposit to a funded card.

    Holds a `BridgeProvider` rather than looking one up: which bridge a process
    uses is a startup choice, not something resolvable from a row (§9.3).
    Adapters *are* looked up, through the registry, because `provider_id` is
    stored on the intent and has to be resolvable from data.
    """

    def __init__(self, bridge: BridgeProvider, *, policy: TopUpPolicy | None = None) -> None:
        self._bridge = bridge
        self._policy = policy or TopUpPolicy()

    async def step(self, session: AsyncSession, intent_id: uuid.UUID) -> StepOutcome:
        """Do the one thing this intent's state is waiting for. Commits."""
        intent = await get_intent(session, intent_id)
        state = intent.state

        if state is FundingState.DEPOSIT_CONFIRMED:
            return await self._submit_to_bridge(session, intent)
        if state is FundingState.BRIDGING:
            return await self._check_bridge(session, intent)
        if state in (FundingState.BRIDGED, FundingState.FUNDING):
            return await self._fund_card(session, intent)
        return self._idle(intent, self._why_idle(state))

    # ------------------------------------------------------------- stages ----

    async def _submit_to_bridge(self, session: AsyncSession, intent: FundingIntent) -> StepOutcome:
        started = intent.state
        try:
            adapter = registry.get_adapter(intent.provider_id)
            destination = await self._destination_for(adapter, intent)
            transfer = await self._bridge.submit(
                BridgeOrder(
                    # The intent id is the idempotency key, so a retry after a
                    # timeout cannot produce a second order for one deposit.
                    order_ref=str(intent.id),
                    amount=intent.money,
                    source_chain=self._policy.source_chain,
                    destination_chain=self._policy.destination_chain,
                    destination_address=destination,
                )
            )
        except ExternalError as exc:
            return await self._after_failure(session, intent, exc, started_at=started)

        moved = await advance(
            session,
            intent.id,
            FundingState.BRIDGING,
            updates={"bridge_ref": transfer.bridge_ref},
            payload={
                "bridge_id": transfer.bridge_id,
                "destination_address": destination,
                "destination_chain": self._policy.destination_chain,
            },
            idempotency_key=f"intent:{intent.id}:bridging",
        )
        return self._outcome(started, moved, f"submitted to {transfer.bridge_id}")

    async def _check_bridge(self, session: AsyncSession, intent: FundingIntent) -> StepOutcome:
        started = intent.state
        if intent.bridge_ref is None:
            # BRIDGING without a reference is unreachable through `step()`, and
            # unrecoverable if it ever happens: there is nothing to ask about.
            return await self._give_up(
                session, intent, "in BRIDGING with no bridge reference", started_at=started
            )

        try:
            transfer = await self._bridge.status(intent.bridge_ref)
        except ExternalError as exc:
            return await self._after_failure(session, intent, exc, started_at=started)

        if transfer.status is BridgeStatus.FAILED:
            return await self._give_up(
                session,
                intent,
                transfer.failure_reason or "the bridge reported a failure",
                started_at=started,
            )

        if transfer.status is BridgeStatus.PENDING:
            return await self._retry(
                session, intent, "the bridge has not delivered yet", started_at=started
            )

        delivered = transfer.amount_out
        if delivered is None or delivered.currency != intent.currency:
            # A completed transfer that cannot say what arrived, or says it in
            # another currency, is not something to guess about with money.
            return await self._give_up(
                session,
                intent,
                f"the bridge completed without a usable amount: {delivered}",
                started_at=started,
            )

        moved = await advance(
            session,
            intent.id,
            FundingState.BRIDGED,
            # The fee is now the difference between two recorded numbers (§9.10).
            updates={"bridged_amount_minor": delivered.amount_minor},
            payload={
                "destination_tx_ref": transfer.destination_tx_ref,
                "amount_in_minor": transfer.amount_in.amount_minor,
                "amount_out_minor": delivered.amount_minor,
                "fee_minor": transfer.amount_in.amount_minor - delivered.amount_minor,
            },
            idempotency_key=f"intent:{intent.id}:bridged",
        )
        return self._outcome(started, moved, f"bridge delivered {delivered}")

    async def _fund_card(self, session: AsyncSession, intent: FundingIntent) -> StepOutcome:
        started = intent.state
        if intent.state is FundingState.BRIDGED:
            # Enter FUNDING *before* calling, so a crash mid-call leaves the
            # intent in the state that means "fund_card is in flight" and the
            # reconciler retries it. The call is idempotent under the intent id,
            # so a repeat funds once.
            intent = await advance(
                session,
                intent.id,
                FundingState.FUNDING,
                idempotency_key=f"intent:{intent.id}:funding",
            )

        try:
            adapter = registry.get_adapter(intent.provider_id)
            result = await adapter.fund_card(intent.card_id, intent.fundable_money, str(intent.id))
        except ExternalError as exc:
            return await self._after_failure(session, intent, exc, started_at=started)

        if result.status is FundingStatus.FAILED:
            return await self._give_up(
                session, intent, "the provider refused the funding", started_at=started
            )

        if result.status is FundingStatus.PENDING:
            # A normal answer, not an error: a CRYPTO_DEPOSIT provider asked to
            # fund before its own deposit confirms has nothing to report yet
            # (§7.1). The engine sits in FUNDING and asks again.
            return await self._retry(
                session, intent, "the provider has not confirmed the funding", started_at=started
            )

        moved = await advance(
            session,
            intent.id,
            FundingState.FUNDED,
            updates={"issuer_funding_ref": result.issuer_funding_ref},
            payload={
                "funded_minor": result.amount.amount_minor,
                "issuer_funding_ref": result.issuer_funding_ref,
            },
            idempotency_key=f"intent:{intent.id}:funded",
        )
        return self._outcome(started, moved, f"card funded with {result.amount}")

    # ---------------------------------------------------------- decisions ----

    async def _after_failure(
        self,
        session: AsyncSession,
        intent: FundingIntent,
        exc: ExternalError,
        *,
        started_at: FundingState | None = None,
    ) -> StepOutcome:
        if exc.retryable:
            return await self._retry(session, intent, str(exc), started_at=started_at)
        return await self._give_up(session, intent, str(exc), started_at=started_at)

    async def _retry(
        self,
        session: AsyncSession,
        intent: FundingIntent,
        reason: str,
        *,
        started_at: FundingState | None = None,
    ) -> StepOutcome:
        if intent.retry_count >= self._policy.max_retries:
            return await self._give_up(
                session,
                intent,
                f"gave up after {intent.retry_count} attempts: {reason}",
                started_at=started_at,
            )

        from_state = started_at or intent.state
        moved = await advance(session, intent.id, intent.state, reason=reason)
        return self._outcome(from_state, moved, f"retrying ({moved.retry_count}): {reason}")

    async def _give_up(
        self,
        session: AsyncSession,
        intent: FundingIntent,
        reason: str,
        *,
        started_at: FundingState | None = None,
    ) -> StepOutcome:
        from_state = started_at or intent.state
        failure = _FAILURE_FOR[intent.state]
        logger.warning("failing intent %s at %s: %s", intent.id, intent.state, reason)
        moved = await advance(
            session,
            intent.id,
            failure,
            reason=reason,
            idempotency_key=f"intent:{intent.id}:{failure}",
        )
        return self._outcome(from_state, moved, reason)

    async def _destination_for(self, adapter: CardIssuerAdapter, intent: FundingIntent) -> str:
        """Where the bridge should deliver, which depends on the funding model.

        `CRYPTO_DEPOSIT`: the card's own address, and arrival *is* the funding.
        `FIAT_RAIL`: our settlement address, and funding is a separate call (§9.3).
        The card is read from the provider rather than from a local copy (§3.4).
        """
        if adapter.funding_model is FundingModel.CRYPTO_DEPOSIT:
            card = await adapter.get_card(intent.card_id)
            if not card.deposit_address:
                raise _Unroutable(
                    f"{adapter.provider_id} card {intent.card_id} is a crypto-deposit card "
                    f"with no deposit address"
                )
            return card.deposit_address

        if not self._policy.settlement_address:
            raise _Unroutable(
                f"{adapter.provider_id} is a fiat rail, so a bridge order needs a settlement "
                f"address; none is configured (FUNDING_SETTLEMENT_ADDRESS)"
            )
        return self._policy.settlement_address

    # ------------------------------------------------------------ reports ----

    @staticmethod
    def _outcome(from_state: FundingState, after: FundingIntent, detail: str) -> StepOutcome:
        """The hop that just happened.

        Takes the state as a value rather than the intent it came from:
        `advance()` mutates and returns the *same* identity-mapped object, so
        reading `before.state` afterwards reads the state it moved to. Caught by
        `test_a_confirmed_deposit_is_submitted_to_the_bridge`, which is the kind
        of bug that would otherwise have made every outcome look like a no-op.
        """
        return StepOutcome(
            intent_id=after.id,
            from_state=from_state,
            to_state=after.state,
            detail=detail,
        )

    def _idle(self, intent: FundingIntent, detail: str) -> StepOutcome:
        return StepOutcome(
            intent_id=intent.id,
            from_state=intent.state,
            to_state=intent.state,
            detail=detail,
        )

    @staticmethod
    def _why_idle(state: FundingState) -> str:
        if state is FundingState.PENDING:
            return "waiting for the watcher to confirm the deposit"
        if state is FundingState.FUNDED:
            return "waiting for the provider's settlement event"
        return "terminal"


class _Unroutable(ExternalError):
    """A bridge order that cannot be addressed. Not worth repeating.

    An `ExternalError` so it travels the same path as a provider's refusal: the
    fact came from outside this process — a card with no deposit address, or a
    deployment with no settlement address — and the answer is the same. Fail the
    intent and say which.
    """
