"""Provider and webhook errors, mapped to HTTP.

Registered as exception handlers rather than caught per route: every card endpoint
can raise the same three or four failures, and try/except in each one is how a
missing case turns into a 500 that reads like a bug in us.

Each response carries a stable machine-readable `code` alongside the human
message, since the mobile client (phase 8) needs to distinguish "card is frozen"
from "no such card" without parsing prose.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.issuers.base import (
    CardholderNotFoundError,
    CardNotFoundError,
    FundingRejectedError,
    IllegalCardTransitionError,
    IssuerError,
    RevealUnsupported,
)
from app.issuers.registry import UnknownProviderError
from app.webhooks.receiver import SignatureRejected

__all__ = ["install_exception_handlers"]


def _problem(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


async def _unknown_provider(_request: Request, exc: Exception) -> JSONResponse:
    return _problem(404, "unknown_provider", str(exc))


async def _not_found(_request: Request, exc: Exception) -> JSONResponse:
    return _problem(404, "not_found", str(exc))


async def _illegal_transition(_request: Request, exc: Exception) -> JSONResponse:
    # 409, not 400: the request is well-formed, the card is simply not in a state
    # where it can be honoured.
    return _problem(409, "illegal_card_transition", str(exc))


async def _funding_rejected(_request: Request, exc: Exception) -> JSONResponse:
    return _problem(409, "funding_rejected", str(exc))


async def _reveal_unsupported(_request: Request, exc: Exception) -> JSONResponse:
    # 501, not the 502 an `IssuerError` otherwise gets: nothing failed upstream. The
    # provider has no reveal path we use, which is a fact about the integration and
    # not an outage, so a client should stop asking rather than retry.
    return _problem(501, "reveal_unsupported", str(exc))


async def _signature_rejected(_request: Request, exc: Exception) -> JSONResponse:
    # No detail about which part failed: a caller who cannot sign has no business
    # learning how close they got.
    return _problem(401, "signature_rejected", "webhook signature verification failed")


async def _issuer_error(_request: Request, exc: Exception) -> JSONResponse:
    # The catch-all for a provider misbehaving. 502: the failure is upstream of us.
    return _problem(502, "issuer_error", str(exc))


def install_exception_handlers(app: FastAPI) -> None:
    # Order matters only in that the specific subclasses are registered too;
    # Starlette resolves by exact class then walks the MRO.
    app.add_exception_handler(UnknownProviderError, _unknown_provider)
    app.add_exception_handler(CardNotFoundError, _not_found)
    app.add_exception_handler(CardholderNotFoundError, _not_found)
    app.add_exception_handler(IllegalCardTransitionError, _illegal_transition)
    app.add_exception_handler(FundingRejectedError, _funding_rejected)
    app.add_exception_handler(RevealUnsupported, _reveal_unsupported)
    app.add_exception_handler(SignatureRejected, _signature_rejected)
    app.add_exception_handler(IssuerError, _issuer_error)
