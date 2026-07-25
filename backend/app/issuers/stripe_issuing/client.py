"""A small HTTP client for Stripe's API.

Deliberately not their SDK, for the reasons in docs/ARCHITECTURE.md §4.3: what
this needs is two verbs, one auth header, cursor pagination and error
translation, and writing that is cheaper than taking on a dependency whose types,
retry policy and global configuration we would then have to work around. The
adapter above it stays pure translation.

Four behaviours are load-bearing, and three of them differ from Lithic's client:

**The body is form-encoded, not JSON.** Stripe takes
`application/x-www-form-urlencoded` and expresses nesting with brackets —
`spending_controls[spending_limits][0][amount]=100000`. A JSON body is not
rejected; it is accepted with a 200 and every field ignored, which would create a
card with none of the controls that were asked for. `form_encode` is therefore
covered directly by tests rather than only through the adapter.

**Test mode is a property of the key.** Lithic has a separate sandbox host, so a
misconfigured environment cannot reach production by omission. Stripe has one
host and reads `sk_test_` vs `sk_live_` off the key, so `checked_api_key` rebuilds
that defence as an explicit refusal. This project is sandbox-only (SPEC.md §2).

**Retry only what a retry can fix.** 429, 5xx and transport failures are retried
with a bounded, increasing backoff; every other 4xx is returned immediately.
Stripe's 409 is an idempotency-key mismatch and its 402 a failed request — both
give the same answer on a repeat, and retrying either hides a caller's bug.

**Never repeat the API key.** These messages reach logs and, through the 502
handler, response bodies.

One `httpx.AsyncClient` per request rather than one held for the process — the
same choice Lithic's client made, and deliberately not revisited here: a pooled
client is what would put an `aclose()` on `CardIssuerAdapter`, and this phase's
job is to test the interface, not to widen it (docs/ARCHITECTURE.md §8.6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from app.issuers.base import IssuerError

#: Stripe's maximum page size for list endpoints. Fewer round trips per balance.
DEFAULT_PAGE_SIZE = 100

#: Waits between attempts at a request worth retrying. Its length is the retry cap.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

#: Statuses where trying the same request again can plausibly succeed.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Stripe returns this on every response; their support asks for it first.
REQUEST_ID_HEADER = "request-id"

#: What a live key has in it, in both the secret and restricted-key forms.
LIVE_KEY_MARKER = "_live_"

Sleeper = Callable[[float], Awaitable[None]]

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "RETRYABLE_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "StripeApiError",
    "StripeClient",
    "checked_api_key",
    "form_encode",
]


class StripeApiError(IssuerError):
    """A call to Stripe that did not succeed.

    An `IssuerError`, so everything above `issuers/` already handles it (the API
    answers 502) without knowing Stripe exists.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str | None = None,
        error_type: str | None = None,
        param: str | None = None,
        request_id: str | None = None,
        request_log_url: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"stripe: {message} (status {status})")
        self.message = message
        #: `0` when nothing answered at all — a timeout or a refused connection.
        self.status = status
        #: Stripe's machine-readable `error.code`, e.g. `resource_missing`.
        self.code = code
        #: Stripe's `error.type`, one of their four families.
        self.error_type = error_type
        #: Which parameter the complaint is about, when it is about one.
        self.param = param
        self.request_id = request_id
        #: A Dashboard link straight to the failing request.
        self.request_log_url = request_log_url
        self.retryable = retryable


def checked_api_key(api_key: str) -> str:
    """The key to authenticate with, or a `ValueError` naming what is wrong.

    Raises rather than returning empty, and does so on the request path rather
    than in a constructor: `registry.describe()` builds every registered adapter
    to report its funding model, and `GET /providers` calls it, so an adapter that
    refused to *exist* without credentials would take that route down for the
    providers that are configured.
    """
    if not api_key:
        raise ValueError(
            "stripe issuing api key is empty; set STRIPE_ISSUING_API_KEY (see .env.example)"
        )
    if LIVE_KEY_MARKER in api_key:
        raise ValueError(
            "STRIPE_ISSUING_API_KEY looks like a live key. This project is sandbox-only "
            "(SPEC.md §2) and Stripe picks test-vs-live from the key rather than the host, "
            "so a live key here would move real money. Use an sk_test_ key."
        )
    return api_key


def form_encode(payload: Mapping[str, Any]) -> dict[str, str]:
    """A nested structure flattened into Stripe's bracketed form syntax.

    `{"spending_controls": {"spending_limits": [{"amount": 5000}]}}` becomes
    `{"spending_controls[spending_limits][0][amount]": "5000"}`.

    `None` encodes as the empty string, because that is how Stripe *unsets* a
    field — dropping the key instead would silently leave the old value in place,
    which matters for clearing a metadata marker.
    """
    encoded: dict[str, str] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(f"{prefix}[{key}]" if prefix else str(key), item)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        else:
            encoded[prefix] = _scalar(prefix, value)

    walk("", payload)
    return encoded


def _scalar(field: str, value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    # A float reaching a money field is a bug upstream, and `str(12.99)` would ship
    # it to Stripe as a plausible-looking amount. Money is integer minor units
    # everywhere in this system (SPEC.md §1).
    raise IssuerError(
        f"stripe form field {field!r} cannot carry a {type(value).__name__} "
        f"({value!r}); amounts are integer minor units, never float"
    )


class StripeClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        api_version: str = "",
        sleep: Sleeper | None = None,
        backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        # Deliberately no key validation here: see `checked_api_key`.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._api_version = api_version
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._backoff = backoff

    # ------------------------------------------------------------- verbs ----

    async def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Stripe has no PATCH: an update is a POST to the object's own path."""
        return await self.request("POST", path, body=body, idempotency_key=idempotency_key)

    async def list_all(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every record on a list endpoint, following the cursor to the end.

        Stripe lists newest-first and `starting_after` continues further back, so
        this walks into the past. A balance computed from the first page only would
        be right until a card had more than a hundred transactions.
        """
        collected: list[dict[str, Any]] = []
        query: dict[str, Any] = {**(params or {}), "limit": DEFAULT_PAGE_SIZE}
        while True:
            page = await self.get(path, params=query)
            data = page.get("data")
            if not isinstance(data, list):
                raise StripeApiError(
                    f"{path} list response has no `data` array: got {type(data).__name__}",
                    status=200,
                )
            collected.extend(entry for entry in data if isinstance(entry, dict))
            # An empty page with `has_more` set would otherwise re-request the same
            # cursor forever — a hang rather than a failure.
            if not page.get("has_more") or not data:
                return collected
            cursor = data[-1].get("id") if isinstance(data[-1], Mapping) else None
            if not isinstance(cursor, str) or not cursor:
                raise StripeApiError(
                    f"{path} claims more pages but its last entry has no id to use as a "
                    f"pagination cursor",
                    status=200,
                )
            query["starting_after"] = cursor

    # ----------------------------------------------------------- plumbing ----

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {checked_api_key(self._api_key)}",
            "Accept": "application/json",
        }
        if self._api_version:
            headers["Stripe-Version"] = self._api_version
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        query = form_encode(params) if params else None
        data = form_encode(body) if body else None

        attempt = 0
        while True:
            try:
                response = await self._send(method, path, query, data, headers)
            except httpx.HTTPError as exc:
                # Nothing answered. Safe to retry a GET; for a write this risks a
                # duplicate, which is what `Idempotency-Key` is for — and unlike
                # Lithic, Stripe honours it on every POST.
                if attempt < len(self._backoff):
                    await self._sleep(self._backoff[attempt])
                    attempt += 1
                    continue
                raise StripeApiError(
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
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            return await client.request(
                method, path, params=params, data=data, headers=dict(headers)
            )

    def _read(self, method: str, path: str, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.is_success:
            if not isinstance(payload, dict):
                raise StripeApiError(
                    f"{method} {path} answered {response.status_code} but the body is "
                    f"not a JSON object",
                    status=response.status_code,
                )
            return payload

        error = payload.get("error") if isinstance(payload, dict) else None
        described = error if isinstance(error, Mapping) else {}
        message = described.get("message")
        return self._fail(
            method,
            path,
            response,
            message=message if isinstance(message, str) and message else None,
            described=described,
        )

    def _fail(
        self,
        method: str,
        path: str,
        response: httpx.Response,
        *,
        message: str | None,
        described: Mapping[str, Any],
    ) -> dict[str, Any]:
        raise StripeApiError(
            self._redact(message or f"{method} {path} failed"),
            status=response.status_code,
            code=_optional_str(described.get("code")),
            error_type=_optional_str(described.get("type")),
            param=_optional_str(described.get("param")),
            request_id=response.headers.get(REQUEST_ID_HEADER),
            request_log_url=_optional_str(described.get("request_log_url")),
            retryable=response.status_code in RETRYABLE_STATUSES,
        )

    def _redact(self, message: str) -> str:
        """Stripe echoes an invalid key back in the error message. We do not."""
        if self._api_key and self._api_key in message:
            return message.replace(self._api_key, "[redacted]")
        return message


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
