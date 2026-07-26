"""Asking the guardian network whether a transfer has been signed yet.

One call, over plain HTTP: `GET /api/v1/vaas/{chain}/{emitter}/{sequence}`. That
this exists at all is what makes a Python integration possible — Wormhole's SDK is
TypeScript only, and the alternative would have been running a guardian's gRPC
protocol by hand.

**A 404 is a normal answer and the most important one.** It means the guardians
have not signed this message yet, which is the ordinary state of a transfer for the
first few seconds of its life. Read as an error it would fail a healthy transfer;
read as "no such transfer" it would strand one. It is neither: it is *not yet*, and
this module returns `None` for it — the same distinction `getTransaction`'s `null`
and `eth_getTransactionReceipt`'s `null` draw on the two chains either side.

The VAA arrives base64-encoded in a JSON field, and the response also carries the
explorer's own `digest`. That field is not used in anger — the digest is computed
locally from the bytes, because trusting a third party's hash of money-moving data
would be silly — but the fixture asserts the two agree, which is how the
double-keccak in `vaa.py` was confirmed independently of Wormhole's source.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.chain.bridge.base import BridgeError
from app.chain.bridge.wormhole.vaa import SignedVaa, parse_signed_vaa

__all__ = ["RETRYABLE_STATUSES", "WormholeApiError", "WormholescanClient"]

logger = logging.getLogger(__name__)

#: Same set as everywhere else in this service: "not now", not "no".
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

RETRY_BACKOFF_SECONDS = (0.5, 2.0, 5.0)

Sleeper = Callable[[float], Awaitable[None]]


class WormholeApiError(BridgeError):
    """The guardian API could not be read, or answered with something unusable."""

    def __init__(self, message: str, *, status: int, retryable: bool = False) -> None:
        super().__init__(f"wormholescan: {message}", retryable=retryable)
        self.status = status


class WormholescanClient:
    """One call: fetch a signed VAA if there is one yet."""

    def __init__(
        self,
        *,
        api_url: str,
        timeout: float = 20.0,
        sleep: Sleeper | None = None,
        backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._backoff = backoff
        if sleep is not None:
            self._sleep: Sleeper = sleep
        else:  # pragma: no cover - the real clock, never exercised by the suite

            async def _real_sleep(seconds: float) -> None:
                import asyncio

                await asyncio.sleep(seconds)

            self._sleep = _real_sleep

    async def fetch_vaa(
        self, *, emitter_chain: int, emitter_address: str, sequence: int
    ) -> SignedVaa | None:
        """The signed VAA, or `None` while the guardians have not signed it.

        Raises:
            WormholeApiError: the API could not be read, or sent something that is
                not a VAA record. Never for a 404.
            MalformedVaaError: the bytes decoded but are not a VAA.
        """
        path = f"/api/v1/vaas/{emitter_chain}/{emitter_address}/{sequence}"
        payload = await self._get(path)
        if payload is None:
            return None

        record = payload.get("data")
        if isinstance(record, list):
            # The collection form of the same endpoint. Taking the first is right
            # because the path names one sequence, and a sequence is unique.
            record = record[0] if record else None
        if not isinstance(record, dict) or "vaa" not in record:
            raise WormholeApiError(f"{path} answered without a VAA", status=200)

        try:
            raw = base64.b64decode(record["vaa"], validate=True)
        except (ValueError, TypeError) as exc:
            raise WormholeApiError(f"{path} answered with unusable base64", status=200) from exc

        vaa = parse_signed_vaa(raw)
        reported = record.get("digest")
        if isinstance(reported, str) and reported.removeprefix("0x") != vaa.digest.hex():
            # Not a trust decision — the digest used everywhere is the locally
            # computed one. A disagreement means the bytes are not the bytes the
            # explorer indexed, which is worth refusing rather than papering over.
            raise WormholeApiError(
                f"{path} reported digest {reported} for bytes that hash to {vaa.digest.hex()}",
                status=200,
            )
        return vaa

    async def _get(self, path: str) -> dict[str, Any] | None:
        url = f"{self._api_url}{path}"

        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(url)
            except httpx.HTTPError as exc:
                if attempt < len(self._backoff):
                    await self._sleep(self._backoff[attempt])
                    attempt += 1
                    continue
                raise WormholeApiError(
                    f"{path} did not complete: {type(exc).__name__}", status=0, retryable=True
                ) from exc

            if response.status_code == 404:
                # Not signed yet. The ordinary state of a young transfer.
                return None

            if response.status_code in RETRYABLE_STATUSES and attempt < len(self._backoff):
                logger.info("wormholescan %s answered %s, retrying", path, response.status_code)
                await self._sleep(self._backoff[attempt])
                attempt += 1
                continue

            if not response.is_success:
                raise WormholeApiError(
                    f"{path} answered {response.status_code}",
                    status=response.status_code,
                    retryable=response.status_code in RETRYABLE_STATUSES,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise WormholeApiError(
                    f"{path} answered with a body that is not JSON", status=response.status_code
                ) from exc
            if not isinstance(payload, dict):
                raise WormholeApiError(
                    f"{path} answered with {type(payload).__name__}, not an object",
                    status=response.status_code,
                )
            return payload
