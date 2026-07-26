"""Phase 5 demo: a USDC deposit drives an intent from PENDING to FUNDED.

    python scripts/demo_phase5.py                   # replay recorded devnet data
    python scripts/demo_phase5.py --live            # poll devnet for real, read-only
    python scripts/demo_phase5.py --inject stuck    # a bridge that goes quiet
    python scripts/demo_phase5.py --fee 150         # a bridge that charges

The whole pipeline against a real database: watcher -> intent -> bridge -> issuer,
with every hop written to the ledger by `advance()` and nothing else.

**The default mode needs no network and no credentials.** It replays the devnet
responses in `tests/fixtures/solana/` — a real 1.000000 USDC `transferChecked`,
recorded by `scripts/record_solana_fixtures.py`. `--live` points the same watcher
at devnet and polls it read-only, so whatever transfers that account is receiving
right now drive real intents. Neither mode sends a transaction: submitting one
belongs to the wallet that owns the money (SPEC.md §9.3, phase 8).

What the bridge does to the mock provider's Safe is worth watching for. The
simulated bridge moves nothing — it is a state machine with a clock — so when it
reports delivery this script reflects that into the provider's Safe, which is what
a real bridge transaction would have done. `fund_card` at a `CRYPTO_DEPOSIT`
issuer only ever *observes* money that is already there (ARCHITECTURE §7.1), and
this is the seam where that becomes visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.bridge.simulated import BridgeFailureMode, SimulatedBridge
from app.chain.config import get_solana_settings
from app.chain.rpc import SolanaRpcClient, SolanaRpcError
from app.chain.signer import LocalKeypairSigner, SignerError
from app.chain.solana_watcher import SOLANA_DEVNET, SolanaDepositWatcher
from app.chain.tokens import associated_token_address
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.money import Money
from app.core.time import utcnow
from app.funding.deposits import collect_deposits
from app.funding.engine import TopUpEngine, TopUpPolicy
from app.funding.machine import get_intent
from app.funding.reconciler import ReconcilerPolicy, reconcile
from app.funding.routes import register_route
from app.funding.states import FundingState
from app.issuers import registry
from app.issuers.base import CreateCardholderRequest, CreateCardRequest
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from app.ledger.models import LedgerEvent

PROVIDER = "gnosis_pay_mock"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "solana"

#: The account the recorded transfer credited. Real, active, and read-only to us.
RECORDED_DEPOSIT_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"

#: Replay runs record their cursor under a chain label of their own. Both modes
#: watch the same address, and a cursor is keyed on `(chain, address)` — so
#: without this, a replay run's synthetic signature would be handed to the real
#: node as `until` on the next `--live` run, which answers `Invalid param:
#: WrongSize`. The label is the seam that keeps two worlds' positions apart.
REPLAY_CHAIN = f"{SOLANA_DEVNET}-replay"


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class ReplayRpc(SolanaRpcClient):
    """The recorded devnet responses, served without a network.

    A subclass rather than a mocked transport because `respx` is a test-only
    dependency and a demo has to run from an install that does not have it.

    One deliberate departure from the recording: each *run* presents the transfer
    under a fresh signature. The amounts, the balances and the whole transaction
    shape are the recorded ones, but `deposit_tx_ref` is unique per intent by
    design (§9.5), so replaying the same signature twice would correctly refuse
    to open a second intent — and a demo nobody can run twice is a demo nobody
    runs. `--live` has no such problem: real transfers bring their own.
    """

    def __init__(self) -> None:
        super().__init__(rpc_url="replay://devnet")
        self._signature = f"{_recorded_signature()[:24]}demo{uuid.uuid4().hex[:12]}"
        self._served = False

    @property
    def signature(self) -> str:
        return self._signature

    async def get_signatures_for_address(
        self, address: str, *, limit: int = 25, until: str | None = None, commitment: str = "final"
    ) -> list[dict[str, Any]]:
        if self._served:
            return []
        self._served = True
        entry = dict(_recorded_entry(), signature=self._signature)
        return [entry]

    async def get_transaction(
        self, signature: str, *, commitment: str = "finalized"
    ) -> dict[str, Any] | None:
        if signature != self._signature:
            return None
        transaction: dict[str, Any] = fixture("transaction_transfer_checked")["result"]
        return transaction


def _recorded_entry() -> dict[str, Any]:
    """The recorded list entry for the recorded transfer."""
    transaction = fixture("transaction_transfer_checked")["result"]
    keys = [key["pubkey"] for key in transaction["transaction"]["message"]["accountKeys"]]
    assert RECORDED_DEPOSIT_ACCOUNT in keys
    entry: dict[str, Any] = next(
        candidate
        for candidate in fixture("signatures_for_deposit_account")["result"]
        if candidate["slot"] == transaction["slot"]
    )
    return entry


def _recorded_signature() -> str:
    return str(_recorded_entry()["signature"])


def deposit_address(live: bool) -> str:
    """The Solana account the watcher polls — the *source* address (§9.8).

    Derived from the configured keypair when there is one: a wallet address does
    not hold USDC, the token account it derives to does, and that derived address
    is what a fund screen would show. Without a keypair the demo watches the
    recorded account instead, which is real and needs nothing.
    """
    settings = get_solana_settings()
    raw = _keypair_value()
    if raw:
        try:
            signer = LocalKeypairSigner.from_env_value(raw)
        except SignerError as exc:
            print(f"  ! SOLANA_DEPOSIT_KEYPAIR is set but unusable: {exc}")
        else:
            derived = associated_token_address(
                signer.pubkey, Pubkey.from_string(settings.usdc_mint)
            )
            print(f"  wallet         {signer.public_key}")
            print(f"  deposit ATA    {derived}   <- send devnet USDC here")
            return str(derived)

    if live:
        print("  no keypair configured; watching the recorded account instead")
    print(f"  deposit account {RECORDED_DEPOSIT_ACCOUNT}")
    return RECORDED_DEPOSIT_ACCOUNT


def _keypair_value() -> str:
    import os

    return os.getenv("SOLANA_DEPOSIT_KEYPAIR", "")


async def show_ledger(session: AsyncSession, intent_id: Any) -> None:
    heading("the ledger for this intent")
    result = await session.execute(
        select(LedgerEvent).where(LedgerEvent.intent_id == intent_id).order_by(LedgerEvent.id)
    )
    for event in result.scalars().all():
        before = event.state_before or "-"
        reason = (event.payload or {}).get("reason") or ""
        print(f"  {event.event_type:<34} {before:>17} -> {event.state_after or '-':<17} {reason}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="poll devnet instead of replaying")
    parser.add_argument(
        "--inject",
        choices=[mode.value for mode in BridgeFailureMode],
        default=BridgeFailureMode.NONE.value,
        help="make the bridge misbehave, and let the reconciler deal with it",
    )
    parser.add_argument("--fee", type=int, default=0, help="bridge fee in minor units")
    parser.add_argument(
        "--latency", type=float, default=2.0, help="seconds the bridge stays in flight"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    adapter = registry.get_adapter(PROVIDER)
    assert isinstance(adapter, GnosisPayMockAdapter)

    heading("a card at a crypto-deposit issuer")
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest(currency="USD"))
    await adapter.activate_card(card.card_id)
    print(f"  card           {card.card_id} (**** {card.last_four})")
    print(f"  safe           {card.deposit_address}   <- where the bridge delivers")

    heading("the address the watcher polls")
    source = deposit_address(args.live)

    solana = get_solana_settings()
    rpc = (
        SolanaRpcClient(rpc_url=solana.rpc_url, timeout=solana.request_timeout_seconds)
        if args.live
        else ReplayRpc()
    )
    chain = SOLANA_DEVNET if args.live else REPLAY_CHAIN
    watcher = SolanaDepositWatcher(
        rpc,
        deposit_address=source,
        mint=solana.usdc_mint,
        page_limit=solana.page_limit,
        chain=chain,
    )
    bridge = SimulatedBridge(
        latency_seconds=args.latency,
        fee_minor=args.fee,
        failure_mode=BridgeFailureMode(args.inject),
        clock=utcnow,
    )
    engine = TopUpEngine(
        bridge,
        policy=TopUpPolicy(
            max_retries=2,
            source_chain=settings.funding_source_chain,
            destination_chain=settings.funding_destination_chain,
            settlement_address=settings.funding_settlement_address or "0xDemoTreasury",
        ),
    )

    async with sessionmaker() as session:
        await register_route(
            session,
            chain=chain,
            deposit_address=source,
            provider_id=PROVIDER,
            card_id=card.card_id,
        )
        await session.commit()

        heading("watching the chain")
        try:
            report = await collect_deposits(session, watcher)
        except SolanaRpcError as exc:
            # The public devnet endpoint rate-limits readily — that is where
            # `tests/fixtures/solana/error_rate_limited.json` came from. Retrying
            # is the client's job and it already did; this is what is left.
            print(f"  ! the node could not be read: {exc}")
            if exc.retryable:
                print("    that answer means 'not now'. Try again in a moment,")
                print("    or run without --live to replay the recorded responses.")
            return 1
        print(
            f"  observed {report.observed}: {len(report.opened)} opened, "
            f"{len(report.duplicates)} duplicate, {len(report.unroutable)} unroutable, "
            f"{len(report.ignored)} ignored"
        )
        for ignored in report.ignored:
            print(f"    ignored {ignored.signature[:12]}… — {ignored.reason}")

        if not report.opened:
            print("\n  No creditable deposit. In --live mode that only means nothing has")
            print("  arrived at that account since the last poll; run it again, or send")
            print("  devnet USDC to the address above.")
            return 0

        intent_id = report.opened[0]
        intent = await get_intent(session, intent_id)
        print(f"  intent         {intent.id}")
        print(f"  deposit        {intent.money} from {intent.deposit_tx_ref[:16]}…")  # type: ignore[index]
        print(f"  state          {intent.state}")

        # With a failure injected, the engine takes one step and the reconciler
        # takes it from there — otherwise this loop spends the retry budget that
        # SPEC.md §5.3's self-healing is the point of demonstrating.
        injecting = args.inject != BridgeFailureMode.NONE.value
        heading("one step at a time" if injecting else "driving it to FUNDED")
        delivered = False
        for _ in range(1 if injecting else 8):
            outcome = await engine.step(session, intent_id)
            print(f"  {outcome.from_state:>17} -> {outcome.to_state:<17} {outcome.detail}")
            if outcome.to_state is FundingState.BRIDGED and not delivered:
                # The simulated bridge moves nothing. Reflect its delivery into
                # the Safe, which is what a real bridge transaction would do —
                # and the only way money reaches a card at this provider.
                reloaded = await get_intent(session, intent_id)
                assert card.deposit_address is not None
                adapter.simulator.receive_onchain_deposit(
                    card.deposit_address,
                    Money(
                        reloaded.bridged_amount_minor or reloaded.amount_minor, reloaded.currency
                    ),
                )
                print(f"  {'':>17}    (bridge delivery reflected into the Safe)")
                delivered = True
            if outcome.to_state in (FundingState.FUNDED, *_terminal()):
                break
            if outcome.to_state is FundingState.BRIDGING:
                await asyncio.sleep(args.latency + 0.2)

        final = await get_intent(session, intent_id)
        if final.state is not FundingState.FUNDED and not final.state.is_failure:
            heading("the reconciler, on an intent that stopped moving")
            print("  (thresholds cut to seconds for the demo; the defaults are minutes)")
            policy = ReconcilerPolicy(stuck_after_seconds=1, max_backoff_seconds=2, batch_limit=10)
            for _ in range(6):
                await asyncio.sleep(1.2)
                pass_report = await reconcile(session, engine, now=utcnow(), policy=policy)
                for outcome in pass_report.stepped:
                    print(f"  {outcome.from_state:>17} -> {outcome.to_state:<17} {outcome.detail}")
                for plan in pass_report.waiting:
                    print(f"  {plan.state:>17}    waiting until {plan.due_at:%H:%M:%S}")
                final = await get_intent(session, intent_id)
                if final.state.is_failure:
                    break

        await show_ledger(session, intent_id)

        heading("where it ended")
        print(f"  state          {final.state}")
        print(f"  deposited      {final.money}")
        if final.bridged_amount_minor is not None:
            print(
                f"  bridged        {final.fundable_money}"
                f"   (fee {final.amount_minor - final.bridged_amount_minor} minor units)"
            )
        if final.issuer_funding_ref:
            print(f"  issuer ref     {final.issuer_funding_ref}")
        if final.last_error:
            print(f"  last error     {final.last_error}")
        print(f"  balance        {await adapter.get_balance(card.card_id)}")

    return 0


def _terminal() -> tuple[FundingState, ...]:
    return tuple(state for state in FundingState if state.is_failure)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
