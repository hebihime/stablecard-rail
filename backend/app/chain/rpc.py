"""A small async JSON-RPC client for a Solana node.

Deliberately `httpx` and literal method names rather than `solana-py`'s
`AsyncClient`, for the reason docs/ARCHITECTURE.md §4.3 gives about vendor SDKs:
the watcher needs two methods, and what it gains from writing them is that the
whole thing is testable by replaying recorded responses through `respx` — the
same mechanism the issuer contract tests already use. `solders` stays for the
things a wire format cannot do (keys, signing).

**Two answers here are not what they look like**, and both are recorded fixtures
rather than beliefs:

* A JSON-RPC error arrives inside an HTTP 200. A client that trusts the status
  code reads a failure as a success.
* `getTransaction` for an unknown signature returns `result: null` — also inside
  a 200, and *not* an error. The watcher must not mistake it for a transfer that
  failed, nor move a cursor past it.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.core.errors import ExternalError

__all__ = [
    "RETRYABLE_RPC_CODES",
    "RETRYABLE_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "SolanaRpcClient",
    "SolanaRpcError",
]

logger = logging.getLogger(__name__)

#: HTTP statuses another attempt could get past. Same set both issuer clients
#: settled on, and for the same reason: these say "not now", not "no".
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: JSON-RPC error codes worth another attempt. `429` is the public devnet
#: endpoint's own (recorded in `tests/fixtures/solana/error_rate_limited.json`);
#: `-32603` is JSON-RPC's "internal error". Everything else — `-32602` invalid
#: params above all — is a complaint about the request, which will not improve.
RETRYABLE_RPC_CODES = frozenset({429, -32603})

RETRY_BACKOFF_SECONDS = (0.5, 2.0, 5.0)

Sleeper = Callable[[float], Awaitable[None]]


class SolanaRpcError(ExternalError):
    """An RPC call that did not answer, or answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"solana rpc: {message}", retryable=retryable)
        self.message = message
        #: HTTP status. `0` when nothing answered at all — a timeout or a refusal.
        self.status = status
        #: JSON-RPC error code, when the failure came back inside a 200.
        self.code = code


class SolanaRpcClient:
    """Two calls: list an address's signatures, and read one transaction."""

    def __init__(
        self,
        *,
        rpc_url: str,
        timeout: float = 20.0,
        sleep: Sleeper | None = None,
        backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._rpc_url = rpc_url
        self._timeout = timeout
        self._backoff = backoff
        if sleep is not None:
            self._sleep: Sleeper = sleep
        else:  # pragma: no cover - the real clock, never exercised by the suite

            async def _real_sleep(seconds: float) -> None:
                import asyncio

                await asyncio.sleep(seconds)

            self._sleep = _real_sleep

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 25,
        until: str | None = None,
        commitment: str = "finalized",
    ) -> list[dict[str, Any]]:
        """Signatures touching `address`, **newest first**.

        `until` is the exclusive lower bound — the last signature already seen —
        which is how the watcher asks only for what is new. The node stops at it
        rather than paging back through an account's whole history.
        """
        options: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if until is not None:
            options["until"] = until

        result = await self.call("getSignaturesForAddress", [address, options])
        if not isinstance(result, list):
            raise SolanaRpcError(
                f"getSignaturesForAddress returned {type(result).__name__}, not a list",
                status=200,
            )
        return result

    async def get_transaction(
        self, signature: str, *, commitment: str = "finalized"
    ) -> dict[str, Any] | None:
        """One transaction in `jsonParsed` form, or `None` if the node has none.

        `None` is a real answer, not an error: a signature can be known to the
        ledger index before the transaction is available, and an unknown one
        looks identical. Either way there is nothing to credit yet.
        """
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    # Without this the node refuses every versioned transaction,
                    # which is most of them.
                    "maxSupportedTransactionVersion": 0,
                    "commitment": commitment,
                },
            ],
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise SolanaRpcError(
                f"getTransaction returned {type(result).__name__}, not an object", status=200
            )
        return result

    async def get_account_info(
        self, address: str, *, commitment: str = "finalized"
    ) -> dict[str, Any] | None:
        """One account, base64-encoded, or `None` if it does not exist.

        `None` is a real answer again — and here it is the one the bridge adapter
        leans on hardest: "this transfer's message account has not been created,
        so nothing has been submitted for it yet" (docs/ARCHITECTURE.md §10.4).
        """
        result = await self.call(
            "getAccountInfo", [address, {"encoding": "base64", "commitment": commitment}]
        )
        if not isinstance(result, dict):
            raise SolanaRpcError(
                f"getAccountInfo returned {type(result).__name__}, not an object", status=200
            )
        value = result.get("value")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise SolanaRpcError(
                "getAccountInfo returned a value that is not an account", status=200
            )
        return value

    async def get_latest_blockhash(self, *, commitment: str = "finalized") -> str:
        """A recent blockhash, which is what makes a transaction expire.

        `finalized` rather than `processed`: a blockhash from a slot that gets
        dropped takes the transaction with it, and this service would rather wait
        a moment than send something that silently never lands.
        """
        result = await self.call("getLatestBlockhash", [{"commitment": commitment}])
        if not isinstance(result, dict):
            raise SolanaRpcError(
                f"getLatestBlockhash returned {type(result).__name__}, not an object", status=200
            )
        value = result.get("value")
        blockhash = value.get("blockhash") if isinstance(value, dict) else None
        if not isinstance(blockhash, str):
            raise SolanaRpcError("getLatestBlockhash returned no blockhash", status=200)
        return blockhash

    async def send_transaction(self, signed: bytes, *, skip_preflight: bool = False) -> str:
        """Submit a signed transaction; returns its signature.

        The only write in this client, and it is safe to retry for the same reason
        as its EVM counterpart: a signed Solana transaction *is* its signature, so
        a duplicate is rejected as already-processed rather than executed twice.
        That holds only while the blockhash is valid, which is why an expiry is
        surfaced to the caller rather than retried here.
        """
        result = await self.call(
            "sendTransaction",
            [
                base64.b64encode(signed).decode(),
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "preflightCommitment": "finalized",
                    # One retry inside the node is enough; this service does its
                    # own, and a node that retries for a minute hides a failure.
                    "maxRetries": 1,
                },
            ],
        )
        if not isinstance(result, str):
            raise SolanaRpcError(
                f"sendTransaction returned {type(result).__name__}, not a signature", status=200
            )
        return result

    async def simulate_transaction(self, signed: bytes) -> dict[str, Any]:
        """What the node thinks would happen. Used as a pre-flight, never alone.

        A simulation that fails is a reason not to spend a fee; a simulation that
        succeeds is not a promise, because state moves underneath it.
        """
        result = await self.call(
            "simulateTransaction",
            [
                base64.b64encode(signed).decode(),
                {"encoding": "base64", "commitment": "finalized", "replaceRecentBlockhash": True},
            ],
        )
        if not isinstance(result, dict):
            raise SolanaRpcError(
                f"simulateTransaction returned {type(result).__name__}, not an object", status=200
            )
        value = result.get("value")
        if not isinstance(value, dict):
            raise SolanaRpcError("simulateTransaction returned no value", status=200)
        return value

    # ------------------------------------------------------------ plumbing ---

    async def call(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call, with retries for the answers that mean "not now"."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        attempt = 0
        while True:
            try:
                response = await self._post(body)
            except httpx.HTTPError as exc:
                # Nothing answered. Every method here is a read, so retrying is
                # free of side effects.
                if attempt < len(self._backoff):
                    await self._sleep(self._backoff[attempt])
                    attempt += 1
                    continue
                raise SolanaRpcError(
                    f"{method} did not complete: {type(exc).__name__}", status=0, retryable=True
                ) from exc

            if response.status_code in RETRYABLE_STATUSES and attempt < len(self._backoff):
                await self._sleep(self._backoff[attempt])
                attempt += 1
                continue

            payload = self._read(method, response)
            error = payload.get("error")
            if error is None:
                return payload.get("result")

            code = error.get("code") if isinstance(error, dict) else None
            if code in RETRYABLE_RPC_CODES and attempt < len(self._backoff):
                logger.info("solana rpc %s answered %s, retrying", method, code)
                await self._sleep(self._backoff[attempt])
                attempt += 1
                continue
            raise self._as_error(method, response.status_code, error)

    async def _post(self, body: Mapping[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(self._rpc_url, json=dict(body))

    def _read(self, method: str, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if not isinstance(payload, dict):
            raise SolanaRpcError(
                f"{method} answered {response.status_code} with a body that is not JSON-RPC",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUSES,
            )
        if not response.is_success and "error" not in payload:
            raise SolanaRpcError(
                f"{method} failed",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUSES,
            )
        return payload

    def _as_error(self, method: str, status: int, error: Any) -> SolanaRpcError:
        if not isinstance(error, dict):
            return SolanaRpcError(f"{method} failed: {error!r}", status=status)
        code = error.get("code")
        return SolanaRpcError(
            f"{method} failed: {error.get('message', 'no message')}",
            status=status,
            code=code if isinstance(code, int) else None,
            retryable=code in RETRYABLE_RPC_CODES or status in RETRYABLE_STATUSES,
        )
