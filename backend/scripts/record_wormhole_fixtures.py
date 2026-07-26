"""Record the guardian API's fixtures from Wormholescan's testnet deployment.

    python scripts/record_wormhole_fixtures.py            # write tests/fixtures/wormhole/
    python scripts/record_wormhole_fixtures.py --dry-run  # call, print, write nothing
    python scripts/record_wormhole_fixtures.py --discover  # re-pin the VAA

No credentials, read-only, and public. **The test suite never runs this**
(SPEC.md §10) — it replays what this wrote.

What it records:

* `vaa_token_transfer` — a real signed VAA from the Solana devnet Token Bridge
  emitter, base64 in a JSON field, **with the explorer's own `digest`**. That last
  field is what makes this fixture more than a sample: the digest Wormholescan
  reports for these bytes equals `keccak256(keccak256(body))` computed locally,
  which is the second, independent confirmation of the hash formula in `vaa.py`.
  The first is Wormhole's own `Messages.sol`.
* `vaa_not_signed_yet` — the **404** for a sequence the guardians have not signed.
  Recorded because it is the single most misreadable answer in this integration:
  it is not an error and it is not "no such transfer", it is *not yet*.

The VAA is pinned below so a re-run records the same one; `--discover` picks the
newest from the same emitter and prints it to paste back. Nothing is redacted:
every byte is public, and the transfer belongs to a stranger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from app.chain.bridge.wormhole.config import (
    WORMHOLE_CHAIN_SOLANA,
    WORMHOLE_TESTNET_API_URL,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "wormhole"

#: The Solana devnet Token Bridge's emitter, derived in
#: `app/chain/bridge/wormhole/accounts.py` and confirmed three ways: it is what
#: Wormholescan reports as `emitterNativeAddr` for chain 1, and it is what BSC
#: testnet's Token Bridge answers for `bridgeContracts(1)`.
EMITTER = "3b26409f8aaded3f5ddca184695aa6a0fa829b0c85caf84856324896d214ca98"

#: Pinned so a re-run records the same transfer. A type-1 token transfer out of
#: Solana devnet, one guardian signature, guardian set 0.
RECORDED_SEQUENCE = 56910

#: A sequence far beyond anything the emitter has produced, for the 404.
UNSIGNED_SEQUENCE = 99_999_999


async def get(client: httpx.AsyncClient, path: str) -> tuple[int, Any]:
    response = await client.get(f"{WORMHOLE_TESTNET_API_URL}{path}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def write(name: str, payload: Any, *, dry_run: bool) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if dry_run:
        print(f"--- {name}.json\n{body}")
        return
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(body)
    print(f"wrote {name}.json ({len(body)} bytes)")


async def discover(client: httpx.AsyncClient) -> None:
    _status, payload = await get(
        client, f"/api/v1/vaas/{WORMHOLE_CHAIN_SOLANA}/{EMITTER}?pageSize=1"
    )
    records = payload.get("data") or []
    if not records:
        print("the emitter has no VAAs the explorer knows about; try again later")
        return
    newest = records[0]
    print(f"newest VAA from the Solana devnet Token Bridge, at {newest.get('timestamp')}:")
    print(f"RECORDED_SEQUENCE = {newest['sequence']}")


async def record(*, dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        path = f"/api/v1/vaas/{WORMHOLE_CHAIN_SOLANA}/{EMITTER}/{RECORDED_SEQUENCE}"
        status, payload = await get(client, path)
        if status != 200:
            print(f"{path} answered {status}; re-pin with --discover")
            return
        write("vaa_token_transfer", payload, dry_run=dry_run)

        path = f"/api/v1/vaas/{WORMHOLE_CHAIN_SOLANA}/{EMITTER}/{UNSIGNED_SEQUENCE}"
        status, payload = await get(client, path)
        if status != 404:
            print(f"expected a 404 for sequence {UNSIGNED_SEQUENCE}, got {status}")
            return
        write("vaa_not_signed_yet", payload, dry_run=dry_run)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    parser.add_argument("--discover", action="store_true", help="re-pin the VAA")
    args = parser.parse_args()

    if args.discover:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await discover(client)
        return 0

    await record(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
