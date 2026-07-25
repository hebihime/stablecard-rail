"""FastAPI application factory.

Routers land with their phases (SPEC.md §12). Phase 1 brought the ledger read
surface and a health probe; phase 2 adds card lifecycle and webhook receipt. The
OTP surface (§6) and the funding read surface arrive in phases 7 and 5.

Importing `app.issuers` is what registers the adapters — see that package's
docstring. It is imported for effect here so the registry is populated before the
first request rather than on whichever request happens to be first.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app import issuers as _issuer_adapters  # noqa: F401 -- imported for its registrations
from app.api.cards import router as cards_router
from app.api.errors import install_exception_handlers
from app.api.ledger import router as ledger_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.db import get_session
from app.core.logging import configure_logging
from app.core.redis import get_redis

health_router = APIRouter(tags=["ops"])


@health_router.get("/healthz", summary="Liveness plus dependency reachability")
async def healthz(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    # Both are hard dependencies from phase 2 on: without Redis there is no
    # webhook dedup, and a receiver that cannot dedup is worse than one that is down.
    await session.execute(text("SELECT 1"))
    await redis.ping()
    return {"status": "ok", "database": "ok", "redis": "ok"}


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="StableCard Rail",
        version=__version__,
        summary=(
            "Sandbox demonstration of a card funding pipeline. "
            "Testnets and provider sandboxes only — no mainnet funds, no production card programs."
        ),
        docs_url="/docs",
    )
    app.state.environment = settings.environment
    install_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(cards_router)
    app.include_router(webhooks_router)
    app.include_router(ledger_router)
    return app


app = create_app()
