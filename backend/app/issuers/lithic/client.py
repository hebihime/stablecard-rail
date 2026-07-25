"""A small HTTP client for Lithic's API.

Deliberately not their SDK. What this needs is four verbs, one auth header, cursor
pagination and error translation, and writing that is cheaper than taking on a
dependency whose types and retry policy we would then have to work around. The
adapter above it stays pure translation.

Three behaviours are load-bearing:

**Retry only what a retry can fix.** 429, 5xx and timeouts are retried with a
bounded, increasing backoff; every 4xx is returned immediately. Retrying a 404 adds
latency and hides the error, and hammering a rate limiter makes the rate limit worse.

**Paginate to the end.** Lithic pages newest-first and `starting_after` walks
backwards in time — which reads like the opposite of what it does. A balance
computed from the first page only would be right until a card had more than a
hundred transactions.

**Never repeat the API key.** These errors reach logs and, through the 502 handler,
response bodies.

One `httpx.AsyncClient` is created per request rather than held for the process.
That costs a connection setup per call, and buys not needing a shutdown hook on the
issuer interface for a resource only one adapter has (docs/ARCHITECTURE.md §4.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.issuers.base import IssuerError

#: Lithic's maximum page size for list endpoints. Fewer round trips per balance.
DEFAULT_PAGE_SIZE = 100

#: Waits between attempts at a request worth retrying. Its length is the retry cap.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

#: Statuses where trying the same request again can plausibly succeed.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

Sleeper = Callable[[float], Awaitable[None]]

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "RETRYABLE_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "LithicApiError",
    "LithicClient",
]


class LithicApiError(IssuerError):
    """A call to Lithic that did not succeed.

    An `IssuerError`, so everything above `issuers/` already handles it (the API
    answers 502) without knowing Lithic exists.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"lithic: {message} (status {status})")
        self.message = message
        #: `0` when nothing answered at all — a timeout or a refused connection.
        self.status = status
        #: Lithic's `debugging_request_id`, which is the first thing their support asks for.
        self.request_id = request_id
        self.retryable = retryable


class LithicClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        sleep: Sleeper | None = None,
        backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("lithic api key is empty; set LITHIC_API_KEY (see .env.example)")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._backoff = backoff

    # ------------------------------------------------------------- verbs ----

    async def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST", path, json_body=json_body, idempotency_key=idempotency_key
        )

    async def patch(
        self, path: str, *, json_body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("PATCH", path, json_body=json_body)

    async def list_all(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every record on a list endpoint, following the cursor to the end."""
        collected: list[dict[str, Any]] = []
        query: dict[str, Any] = {**(params or {}), "page_size": DEFAULT_PAGE_SIZE}
        while True:
            page = await self.get(path, params=query)
            data = page.get("data")
            if not isinstance(data, list):
                raise LithicApiError(
                    f"list response has no `data` array: got {type(data).__name__}",
                    status=200,
                )
            collected.extend(data)
            # An empty page with `has_more` set would otherwise re-request the same
            # cursor forever — a hang rather than a failure.
            if not page.get("has_more") or not data:
                return collected
            query["starting_after"] = data[-1]["token"]

    # ----------------------------------------------------------- plumbing ----

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": self._api_key, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0
        while True:
            try:
                response = await self._send(method, path, params, json_body, headers)
            except httpx.HTTPError as exc:
                # Nothing answered. Safe to retry a GET; for a write this risks a
                # duplicate, which is what `Idempotency-Key` is for on the one write
                # that matters (card creation).
                if attempt < len(self._backoff):
                    await self._sleep(self._backoff[attempt])
                    attempt += 1
                    continue
                raise LithicApiError(
                    f"{method} {path} did not complete: {type(exc).__name__}",
                    status=0,
                    retryable=True,
                ) from exc

            if response.status_code in RETRYABLE_STATUSES and attempt < len(self._backoff):
                await self._sleep(self._backoff[attempt])
                attempt += 1
                continue
            return self._read(method, path, response)

    async def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            return await client.request(
                method, path, params=params, json=json_body, headers=dict(headers)
            )

    def _read(self, method: str, path: str, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.is_success:
            if not isinstance(payload, dict):
                raise LithicApiError(
                    f"{method} {path} answered {response.status_code} but the body is not JSON",
                    status=response.status_code,
                )
            return payload

        message, request_id = _describe(payload) if isinstance(payload, dict) else (None, None)
        raise LithicApiError(
            message or f"{method} {path} failed",
            status=response.status_code,
            request_id=request_id,
            retryable=response.status_code in RETRYABLE_STATUSES,
        )


def _describe(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Lithic's error shape: a `message`, sometimes a nested one, and a request id."""
    message = payload.get("message")
    detail = payload.get("error_data")
    if isinstance(detail, Mapping) and isinstance(detail.get("message"), str):
        message = (
            f"{message}: {detail['message']}" if isinstance(message, str) else detail["message"]
        )
    request_id = payload.get("debugging_request_id")
    return (
        message if isinstance(message, str) else None,
        request_id if isinstance(request_id, str) else None,
    )
