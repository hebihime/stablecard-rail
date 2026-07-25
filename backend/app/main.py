"""FastAPI application factory.

Phase 1 exposes the ledger read surface and a health probe. Card, funding, webhook
and OTP routers arrive with their phases (SPEC.md §12).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.ledger import router as ledger_router
from app.core.config import get_settings
from app.core.db import get_session
from app.core.logging import configure_logging

health_router = APIRouter(tags=["ops"])


@health_router.get("/healthz", summary="Liveness plus database reachability")
async def healthz(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}


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
    app.include_router(health_router)
    app.include_router(ledger_router)
    return app


app = create_app()
