"""The funding worker: what makes the pipeline run without being asked.

    python scripts/run_funding_worker.py --once        # one pass, then exit
    python scripts/run_funding_worker.py               # loop until interrupted
    python scripts/run_funding_worker.py --interval 2 --stuck-after 3   # demo pacing

One pass polls every address in `deposit_routes`, drives intents the engine owns
that have not yet retried, and reconciles the rest (docs/ARCHITECTURE.md §9.16).

Deliberately a plain script rather than a task inside the web process, for the
reason `drain_webhook_retries.py` gives: a worker that lives in the API process is
one that dies with it, and one nobody can run by hand. `--once` is what a cron
entry or a Compose `restart: always` sidecar wants; the loop is for a laptop.

**Nothing here is safe to point at mainnet** and nothing tries: the watcher reads
whatever `SOLANA_RPC_URL` says, which defaults to devnet.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from typing import Any

from app.chain.bridge.simulated import BridgeFailureMode, SimulatedBridge, SimulatedBridgeSettings
from app.chain.config import get_solana_settings
from app.chain.rpc import SolanaRpcClient, SolanaRpcError
from app.chain.solana_watcher import SOLANA_DEVNET, SolanaDepositWatcher
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.time import utcnow
from app.funding.engine import TopUpEngine, TopUpPolicy
from app.funding.reconciler import ReconcilerPolicy
from app.funding.routes import watched_addresses
from app.funding.worker import PassReport, WorkerPolicy, run_pass

logger = logging.getLogger("funding.worker")


async def build_watchers(session: Any) -> list[SolanaDepositWatcher]:
    """One watcher per registered address. Re-read every pass, so a route
    registered while this is running is picked up without a restart."""
    solana = get_solana_settings()
    addresses = await watched_addresses(session, chain=SOLANA_DEVNET)
    return [
        SolanaDepositWatcher(
            SolanaRpcClient(rpc_url=solana.rpc_url, timeout=solana.request_timeout_seconds),
            deposit_address=address,
            mint=solana.usdc_mint,
            page_limit=solana.page_limit,
        )
        for address in addresses
    ]


def describe(report: PassReport) -> str:
    parts = [f"{report.opened} opened", f"{len(report.driven)} driven"]
    if report.reconciled.stepped:
        parts.append(f"{len(report.reconciled.stepped)} reconciled")
    if report.reconciled.waiting:
        parts.append(f"{len(report.reconciled.waiting)} waiting")
    return ", ".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--interval", type=float, help="seconds between passes")
    parser.add_argument("--stuck-after", type=int, help="reconciler threshold, in seconds")
    parser.add_argument(
        "--first-attempt-after", type=float, help="seconds before a hop's first attempt"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # One line per RPC call is noise at this level; the worker's own lines say
    # what happened, and a failure is logged with its reason.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    sessionmaker = get_sessionmaker()

    engine = TopUpEngine(
        SimulatedBridge.from_settings(SimulatedBridgeSettings()),
        policy=TopUpPolicy.from_settings(settings),
    )
    worker_policy = WorkerPolicy.from_settings(settings)
    if args.first_attempt_after is not None:
        worker_policy = WorkerPolicy(
            first_attempt_after_seconds=args.first_attempt_after,
            batch_limit=worker_policy.batch_limit,
        )
    reconciler_policy = ReconcilerPolicy.from_settings(settings)
    if args.stuck_after is not None:
        reconciler_policy = ReconcilerPolicy(
            stuck_after_seconds=args.stuck_after,
            max_backoff_seconds=reconciler_policy.max_backoff_seconds,
            batch_limit=reconciler_policy.batch_limit,
        )
    interval = args.interval if args.interval is not None else settings.worker_interval_seconds

    mode = SimulatedBridgeSettings().failure_mode
    if mode is not BridgeFailureMode.NONE:
        logger.warning("the bridge has failure injection on: %s", mode)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - platform dependent
            loop.add_signal_handler(sig, stopping.set)

    passes = 0
    while True:
        passes += 1
        async with sessionmaker() as session:
            watchers = await build_watchers(session)
            if not watchers:
                logger.info(
                    "no deposit routes registered — nothing to watch. Register one "
                    "(scripts/demo_phase5.py does) and this will pick it up."
                )
            try:
                report = await run_pass(
                    session,
                    engine,
                    watchers=watchers,
                    now=utcnow(),
                    policy=worker_policy,
                    reconciler_policy=reconciler_policy,
                )
            except SolanaRpcError as exc:
                # A node that cannot be read is not a reason to stop: the next
                # pass asks again, and `until` means nothing is lost meanwhile.
                logger.warning("rpc unavailable this pass (retryable=%s): %s", exc.retryable, exc)
            else:
                if report.did_anything:
                    logger.info("pass %s: %s", passes, describe(report))

        if args.once or stopping.is_set():
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        if stopping.is_set():
            break

    logger.info("stopped after %s pass(es)", passes)  # attempted, not necessarily quiet
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
