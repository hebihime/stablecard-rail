"""Re-run webhook handlers whose retries have come due (SPEC.md §4).

    python scripts/drain_webhook_retries.py --once
    python scripts/drain_webhook_retries.py --interval 5

A separate process on purpose. A retry worker living inside the web process is one
that dies with it, cannot be scaled independently, and cannot be run by hand at
three in the morning when there is a backlog to clear.

Handlers that keep failing are dead-lettered into `webhook_dead_letters` rather
than cycling forever; see `app/webhooks/retry.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime

from app.core.db import get_sessionmaker
from app.core.logging import configure_logging
from app.core.redis import get_redis_client
from app.webhooks.dispatch import drain_due

logger = logging.getLogger("drain_webhook_retries")


async def drain_once(*, limit: int) -> int:
    """Drain everything due right now. Returns how many items were handled."""
    redis = get_redis_client()
    async with get_sessionmaker()() as session:
        report = await drain_due(session, redis, now=datetime.now(UTC), limit=limit)
    handled = len(report.succeeded) + len(report.rescheduled) + len(report.dead_lettered)
    if handled:
        logger.info(
            "drained %s: %s succeeded, %s rescheduled, %s dead-lettered",
            handled,
            len(report.succeeded),
            len(report.rescheduled),
            len(report.dead_lettered),
        )
    return handled


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="drain what is due and exit")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="seconds between passes (default: 5)"
    )
    parser.add_argument("--limit", type=int, default=100, help="items per pass (default: 100)")
    args = parser.parse_args()

    configure_logging()
    if args.once:
        await drain_once(limit=args.limit)
        return

    logger.info("draining webhook retries every %ss; ctrl-c to stop", args.interval)
    while True:
        await drain_once(limit=args.limit)
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
