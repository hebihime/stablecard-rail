"""The whole pipeline, with nothing simulated (SPEC.md §5.2, §3.2 revised).

    python scripts/demo_end_to_end.py

Every other demo stops at a seam. `demo_phase5.py` runs the state machine over a
simulated bridge; `demo_phase6.py` runs the real bridge with no state machine; the
end-to-end run recorded in docs/ARCHITECTURE.md §13.5 drove the adapter by hand. This
is the composition: a deposit the watcher finds, an intent the engine walks, a real
Wormhole transfer, and a card funded on a balance read off a chain.

**It spends real testnet money and needs a deposit already sitting in the watched
account** — send one with `spl-token transfer <mint> <amount> <deposit owner>` from a
wallet that is not the one that owns it. A deposit is an *incoming* balance change, so
sending to yourself is invisible to the watcher, which is the one non-obvious part of
setting this up.

What it prints at the end is the point: one intent id and its ledger, every transition
carrying a real reference — a Solana signature, a Wormhole VAA identity, a `balanceOf`
evidence string. That is the artifact SPEC.md §7 exists for.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solders.pubkey import Pubkey
from sqlalchemy import select

from app.chain.bridge.build import BridgeChoice, build_bridge
from app.chain.config import get_solana_settings
from app.chain.rpc import SolanaRpcClient
from app.chain.signer import LocalKeypairSigner
from app.chain.solana_watcher import SolanaDepositWatcher
from app.chain.tokens import associated_token_address
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.funding.deposits import collect_deposits
from app.funding.engine import TopUpEngine, TopUpPolicy
from app.funding.routes import register_route
from app.funding.states import FundingState, is_terminal
from app.issuers import registry
from app.issuers.base import CreateCardholderRequest, CreateCardRequest
from app.ledger.models import LedgerEvent

PROVIDER = "gnosis_pay_mock"


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-steps", type=int, default=40, help="engine steps before giving up")
    parser.add_argument("--interval", type=float, default=6.0, help="seconds between steps")
    parser.add_argument(
        "--intent",
        metavar="UUID",
        help=(
            "drive an intent the watcher already created. The cursor cannot rewind "
            "(docs/ARCHITECTURE.md §9), so a deposit is found exactly once — this is how "
            "to continue after an interrupted run rather than sending more money."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    # This polls for minutes and Python buffers a piped stdout.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    settings = get_settings()
    solana = get_solana_settings()
    chain = settings.funding_source_chain

    heading("the deposit account the watcher polls")
    signer = LocalKeypairSigner.from_env_value(solana.deposit_keypair)
    source = str(associated_token_address(signer.pubkey, Pubkey.from_string(solana.usdc_mint)))
    print(f"  wallet {signer.public_key}")
    print(f"  ATA    {source}  <- a deposit must already be here")

    adapter = registry.get_adapter(PROVIDER)
    heading("a card, on a Safe that is a real address")
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(
            email="end-to-end@stablecard.test", first_name="End", last_name="ToEnd"
        )
    )
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest(currency="USD"))
    await adapter.activate_card(card.card_id)
    safe = holder.raw["safeAddress"]
    print(f"  card {card.card_id}")
    print(f"  Safe {safe}")
    if not str(safe).startswith("0x89"):
        print("  ! this Safe is derived, not configured — set GNOSIS_PAY_MOCK_SAFE_ADDRESS")

    sessionmaker = get_sessionmaker()
    rpc = SolanaRpcClient(rpc_url=solana.rpc_url, timeout=solana.request_timeout_seconds)
    watcher = SolanaDepositWatcher(
        rpc,
        deposit_address=source,
        mint=solana.usdc_mint,
        page_limit=solana.page_limit,
        chain=chain,
    )
    bridge = build_bridge(BridgeChoice.WORMHOLE)
    engine = TopUpEngine(
        bridge,
        policy=TopUpPolicy(
            max_retries=settings.funding_max_retries,
            source_chain=chain,
            destination_chain=settings.funding_destination_chain,
            settlement_address=settings.funding_settlement_address or safe,
        ),
    )

    async with sessionmaker() as session:
        await register_route(
            session, chain=chain, deposit_address=source, provider_id=PROVIDER, card_id=card.card_id
        )
        await session.commit()

        if args.intent is not None:
            intent_id = uuid.UUID(args.intent)
            heading("resuming an intent the watcher already opened")
            print(f"  intent {intent_id}")
        else:
            intent_id = await _open_from_chain(session, watcher)
            if intent_id is None:
                return 1

        heading("the engine, walking it")
        await _walk(session, engine, intent_id, args)
        await _print_ledger(session, intent_id)
    return 0


async def _open_from_chain(session, watcher):  # type: ignore[no-untyped-def]
    heading("watching the chain for a deposit")
    report = await collect_deposits(session, watcher)
    print(
        f"  observed {report.observed}: opened {len(report.opened)}, "
        f"duplicates {len(report.duplicates)}, unroutable {len(report.unroutable)}, "
        f"ignored {len(report.ignored)}"
    )
    for ignored in report.ignored:
        print(f"    ignored: {ignored}")
    intents = list(report.opened)
    if not intents:
        print("\n  no new deposit found. The cursor may already have passed it —")
        print("  send another with `spl-token transfer` and run again.")
        return None
    intent_id = intents[0]
    print(f"  intent {intent_id}")
    return intent_id


async def _walk(session, engine, intent_id, args):  # type: ignore[no-untyped-def]
    for step in range(1, args.max_steps + 1):
        outcome = await engine.step(session, intent_id)
        moved = "->" if outcome.progressed else "  "
        print(
            f"  step {step:>2}  {outcome.from_state.value:<18} {moved} "
            f"{outcome.to_state.value:<18} {outcome.detail}"
        )
        # `FUNDED` is not terminal — it waits for a settlement webhook no provider
        # here sends (docs/ARCHITECTURE.md §9.12) — so it is a stopping point for
        # this demo without being an end state for the machine.
        if is_terminal(outcome.to_state) or outcome.to_state is FundingState.FUNDED:
            break
        await asyncio.sleep(args.interval)


async def _print_ledger(session, intent_id):  # type: ignore[no-untyped-def]
    heading("the ledger for this intent")
    rows = (
        await session.execute(
            select(LedgerEvent).where(LedgerEvent.intent_id == intent_id).order_by(LedgerEvent.id)
        )
    ).scalars()
    for row in rows:
        ref = (
            row.payload.get("bridge_ref")
            or row.payload.get("issuer_funding_ref")
            or row.payload.get("deposit_tx_ref")
            or ""
        )
        print(
            f"  {row.event_type:<32} {row.state_before!s:<20} -> {row.state_after!s:<20} {str(ref)[:44]}"
        )

    print(f"\n  GET /ledger?intent_id={intent_id}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
