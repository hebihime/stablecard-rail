"""A small async JSON-RPC client for an EVM node.

The Solana client's twin (`app/chain/rpc.py`), written for the same reason and with
the same shape: `httpx` and literal method names rather than `web3.py`, because the
surface needed is five methods and writing them keeps the whole thing testable by
replaying recorded responses through `respx`.

**Three answers here are not what they look like**, and all three are recorded
fixtures rather than beliefs:

* An RPC error arrives inside an HTTP 200, exactly as on Solana.
* `eth_getTransactionReceipt` for a transaction the node has not mined returns
  `result: null` — inside a 200, and not an error. It means "not yet", and reading
  it as a failure fails a redemption that was about to succeed.
* A **revert** is error code `3`, and the reason is inside the `message` string
  twice: once in plain text and once ABI-encoded. It is a refusal, never a "not
  now", so it must not be retried — `error_execution_reverted.json` pins it.

**Retrying a send is safe here, which is unusual.** A signed transaction fixes its
own nonce and therefore its own hash, so resubmitting the same bytes cannot produce
a second transaction: the node either accepts it or says it already knows it. That
is what makes `send_raw_transaction` retryable in the same breath as the reads.
What is *not* safe is signing a second transaction with a second nonce, and that
decision belongs to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.core.errors import ExternalError

__all__ = [
    "RETRYABLE_RPC_CODES",
    "RETRYABLE_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "REVERTED",
    "EvmRpcClient",
    "EvmRpcError",
]

logger = logging.getLogger(__name__)

#: Same set as every other client in this service: these say "not now", not "no".
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: `-32603` is JSON-RPC's internal error, `-32005` is the de-facto "limit
#: exceeded", `429` is what some nodes put in the body instead of the status line.
#: Notably absent: `3` (reverted) and `-32000` (the generic node complaint —
#: "nonce too low", "insufficient funds", "already known"). Those are answers about
#: *this* request, and the caller has to read them rather than repeat them.
RETRYABLE_RPC_CODES = frozenset({429, -32005, -32603})

#: A revert. Not retryable, and worth naming because the redeemer branches on it.
REVERTED = 3

RETRY_BACKOFF_SECONDS = (0.5, 2.0, 5.0)

Sleeper = Callable[[float], Awaitable[None]]


class EvmRpcError(ExternalError):
    """An RPC call that did not answer, or answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: int | None = None,
        retryable: bool = False,
        revert_reason: str | None = None,
    ) -> None:
        super().__init__(f"evm rpc: {message}", retryable=retryable)
        self.message = message
        #: HTTP status. `0` when nothing answered at all.
        self.status = status
        #: JSON-RPC error code, when the failure came back inside a 200.
        self.code = code
        #: The contract's own words, when the failure was a revert. Ledgered as the
        #: reason a transfer could not be delivered, which is the difference
        #: between "the bridge failed" and "the bridge said why".
        self.revert_reason = revert_reason

    @property
    def reverted(self) -> bool:
        return self.code == REVERTED


class EvmRpcClient:
    """Five calls: read a chain id, a gas price, a nonce, a contract, and send."""

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

    async def chain_id(self) -> int:
        """The node's chain id. Checked against configuration before signing.

        Cheap insurance against the failure that is hardest to read: a transaction
        signed for one chain is rejected by another with a message about the
        signature, which sends you looking at the key.
        """
        return self._as_int("eth_chainId", await self.call("eth_chainId", []))

    async def gas_price(self) -> int:
        """Wei per unit of gas, as the node currently suggests."""
        return self._as_int("eth_gasPrice", await self.call("eth_gasPrice", []))

    async def transaction_count(self, address: str, *, block: str = "pending") -> int:
        """The next nonce for `address`.

        `pending` and not `latest`: `latest` ignores this sender's transactions
        that are already in the pool, so two redemptions in quick succession would
        be signed with the same nonce and one would replace the other.
        """
        result = await self.call("eth_getTransactionCount", [address, block])
        return self._as_int("eth_getTransactionCount", result)

    async def call_contract(self, *, to: str, data: bytes, block: str = "latest") -> str:
        """A read-only contract call, returning the raw hex word(s)."""
        result = await self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
        if not isinstance(result, str):
            raise EvmRpcError(
                f"eth_call returned {type(result).__name__}, not hex data", status=200
            )
        return result

    async def estimate_gas(self, *, to: str, data: bytes, sender: str) -> int:
        """What the node thinks this call will cost, or a revert saying it cannot.

        Used as a pre-flight: a redemption that would revert says so here, for
        free, instead of burning gas to fail on-chain.
        """
        result = await self.call(
            "eth_estimateGas", [{"to": to, "from": sender, "data": "0x" + data.hex()}]
        )
        return self._as_int("eth_estimateGas", result)

    async def send_raw_transaction(self, signed: bytes) -> str:
        """Submit signed bytes; returns the transaction hash."""
        result = await self.call("eth_sendRawTransaction", ["0x" + signed.hex()])
        if not isinstance(result, str):
            raise EvmRpcError(
                f"eth_sendRawTransaction returned {type(result).__name__}, not a hash",
                status=200,
            )
        return result

    async def transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """One receipt, or `None` if the node has not mined the transaction.

        `None` is a real answer and not an error — the same shape, and the same
        trap, as `getTransaction` on Solana.
        """
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        if result is None:
            return None
        if not isinstance(result, dict):
            raise EvmRpcError(
                f"eth_getTransactionReceipt returned {type(result).__name__}, not an object",
                status=200,
            )
        return result

    # ------------------------------------------------------------ plumbing ---

    async def call(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call, with retries for the answers that mean "not now"."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        attempt = 0
        while True:
            try:
                response = await self._post(body)
            except httpx.HTTPError as exc:
                # Nothing answered. Safe to repeat even for a send: the bytes are
                # signed, so the hash is fixed and a duplicate cannot double-spend.
                if attempt < len(self._backoff):
                    await self._sleep(self._backoff[attempt])
                    attempt += 1
                    continue
                raise EvmRpcError(
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
                logger.info("evm rpc %s answered %s, retrying", method, code)
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
            raise EvmRpcError(
                f"{method} answered {response.status_code} with a body that is not JSON-RPC",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUSES,
            )
        if not response.is_success and "error" not in payload:
            raise EvmRpcError(
                f"{method} failed",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUSES,
            )
        return payload

    def _as_error(self, method: str, status: int, error: Any) -> EvmRpcError:
        if not isinstance(error, dict):
            return EvmRpcError(f"{method} failed: {error!r}", status=status)
        code = error.get("code")
        message = str(error.get("message", "no message"))
        return EvmRpcError(
            f"{method} failed: {message}",
            status=status,
            code=code if isinstance(code, int) else None,
            retryable=code in RETRYABLE_RPC_CODES or status in RETRYABLE_STATUSES,
            revert_reason=_revert_reason(message) if code == REVERTED else None,
        )

    def _as_int(self, method: str, result: Any) -> int:
        if not isinstance(result, str):
            raise EvmRpcError(
                f"{method} returned {type(result).__name__}, not a quantity", status=200
            )
        try:
            return int(result, 16)
        except ValueError as exc:
            raise EvmRpcError(f"{method} returned {result!r}, not a quantity", status=200) from exc


def _revert_reason(message: str) -> str:
    """The human half of a revert message.

    A node puts both readings in one string: `execution reverted: <reason>: 0x…`,
    where the hex is the same reason ABI-encoded. The text is what a person needs
    and what gets ledgered; the hex is dropped rather than decoded, because a
    custom error has no text at all and inventing one from four bytes would be
    guessing.
    """
    reason = message.removeprefix("execution reverted").lstrip(":").strip()
    head, _, tail = reason.partition(": 0x")
    return (head if tail else reason).strip() or message
