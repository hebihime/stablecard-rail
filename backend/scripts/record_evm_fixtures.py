"""Record the destination chain's fixtures from BSC testnet.

    python scripts/record_evm_fixtures.py            # write tests/fixtures/evm/
    python scripts/record_evm_fixtures.py --dry-run  # call, print, write nothing
    python scripts/record_evm_fixtures.py --discover  # re-pin the receipt's tx

No credentials and **no writes to the chain**: every call here is a read, and the
public BSC testnet endpoint needs no key. Recorded rather than authored for the
reason docs/ARCHITECTURE.md §8.10 paid for once — a fixture derived from
documentation passes its own test and fails live.

**The test suite never runs this** (SPEC.md §10). It replays what this wrote.

What it records, and why each one is here:

* `chain_id` / `gas_price` — the two the redeemer needs before it can build a
  transaction. `gas_price` is also a reminder that BSC testnet is not EIP-1559
  priced in practice; a legacy transaction is what gets mined.
* `transaction_count` — the nonce call, in its `pending` form.
* `call_wrapped_asset` — `wrappedAsset(1, <devnet USDC>)` on the Token Bridge: the
  attested wrapped token, and the proof the destination side of the route is open.
* `call_wrapped_asset_unattested` — the same call for a token nobody attested.
  **A zero inside a 200**, not an error. A client that reads it as an address
  sends a transfer to `0x0`.
* `call_transfer_not_completed` — `isTransferCompleted(<unknown hash>)`, i.e. the
  answer for every transfer that has not been redeemed yet.
* `error_execution_reverted` — a real revert: code **3**, with the reason both in
  plain text and ABI-encoded in the same `message` string. This is what a
  redemption that cannot succeed looks like, and it must never be retried.
* `error_method_not_found` — `-32601`, for the same reason: a complaint about the
  request never improves on a second attempt.
* `receipt_success` — a real successful receipt, `status: "0x1"`, with logs.
* `receipt_missing` — `eth_getTransactionReceipt` for a hash the node has never
  seen: **`result: null` inside a 200**, exactly like Solana's `getTransaction`.
  It means "not mined yet", and reading it as a failure fails a live transfer.

The receipt's transaction is pinned below so a re-run records the same one;
`--discover` picks a fresh one out of a recent block and prints it to paste back.
Nothing is redacted — every byte is public chain data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from app.chain.bridge.wormhole.config import BSC_TESTNET_TOKEN_BRIDGE, WORMHOLE_CHAIN_SOLANA
from app.chain.config import USDC_DEVNET_MINT
from app.chain.evm.abi import encode_bytes32_arg, encode_uint16_and_bytes32, selector
from app.chain.evm.config import BSC_TESTNET_RPC_URL
from app.chain.tokens import base58_to_bytes32

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "evm"

#: Pinned so a re-run records the same receipt rather than whatever is recent.
#: Any successful transaction does the job; this one was taken out of a recent
#: block with `--discover`.
RECORDED_RECEIPT_TX = "0xdc78690041d509a4f1593d6a4d50f046aba5cbb65610eda66253bcc60af6df40"

#: An address with no history, for the nonce call. Not a wallet anybody owns.
NONCE_ADDRESS = "0x0000000000000000000000000000000000000001"


async def eth_call(client: httpx.AsyncClient, data: str) -> dict[str, Any]:
    """One read against the Token Bridge — three of the fixtures are this call."""
    return await rpc(client, "eth_call", [{"to": BSC_TESTNET_TOKEN_BRIDGE, "data": data}, "latest"])


async def rpc(client: httpx.AsyncClient, method: str, params: list[Any]) -> dict[str, Any]:
    response = await client.post(
        BSC_TESTNET_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    payload: dict[str, Any] = response.json()
    return payload


def write(name: str, payload: dict[str, Any], *, dry_run: bool) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if dry_run:
        print(f"--- {name}.json\n{body}")
        return
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(body)
    print(f"wrote {name}.json ({len(body)} bytes)")


async def discover(client: httpx.AsyncClient) -> None:
    block = (await rpc(client, "eth_getBlockByNumber", ["latest", False]))["result"]
    for tx_hash in block["transactions"]:
        receipt = (await rpc(client, "eth_getTransactionReceipt", [tx_hash]))["result"]
        if receipt is not None and receipt["status"] == "0x1" and receipt["logs"]:
            print(f"a successful transaction with logs, in block {int(block['number'], 16)}:")
            print(f'RECORDED_RECEIPT_TX = "{tx_hash}"')
            return
    print("no successful transaction with logs in the latest block; run it again")


async def record(*, dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        write("chain_id", await rpc(client, "eth_chainId", []), dry_run=dry_run)
        write("gas_price", await rpc(client, "eth_gasPrice", []), dry_run=dry_run)
        write(
            "transaction_count",
            await rpc(client, "eth_getTransactionCount", [NONCE_ADDRESS, "pending"]),
            dry_run=dry_run,
        )

        wrapped = "0x" + selector("wrappedAsset(uint16,bytes32)").hex()
        attested = (
            wrapped
            + encode_uint16_and_bytes32(
                WORMHOLE_CHAIN_SOLANA, base58_to_bytes32(USDC_DEVNET_MINT)
            ).hex()
        )
        write(
            "call_wrapped_asset",
            await eth_call(client, attested),
            dry_run=dry_run,
        )
        unattested = wrapped + encode_uint16_and_bytes32(WORMHOLE_CHAIN_SOLANA, b"\x11" * 32).hex()
        write(
            "call_wrapped_asset_unattested",
            await eth_call(client, unattested),
            dry_run=dry_run,
        )

        completed = "0x" + selector("isTransferCompleted(bytes32)").hex()
        never = completed + encode_bytes32_arg(b"\x22" * 32).hex()
        write(
            "call_transfer_not_completed",
            await eth_call(client, never),
            dry_run=dry_run,
        )

        # A redemption the chain refuses. `completeTransfer` with bytes that are
        # not a VAA reverts inside the core bridge, which is exactly the shape a
        # genuinely un-redeemable transfer produces.
        garbage = (
            "0x"
            + selector("completeTransfer(bytes)").hex()
            + (32).to_bytes(32, "big").hex()
            + (4).to_bytes(32, "big").hex()
            + "deadbeef".ljust(64, "0")
        )
        write(
            "error_execution_reverted",
            await eth_call(client, garbage),
            dry_run=dry_run,
        )
        write("error_method_not_found", await rpc(client, "eth_nonsense", []), dry_run=dry_run)

        write(
            "receipt_success",
            await rpc(client, "eth_getTransactionReceipt", [RECORDED_RECEIPT_TX]),
            dry_run=dry_run,
        )
        write(
            "receipt_missing",
            await rpc(client, "eth_getTransactionReceipt", ["0x" + "ab" * 32]),
            dry_run=dry_run,
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    parser.add_argument("--discover", action="store_true", help="re-pin the receipt's transaction")
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=30.0) as client:
        if args.discover:
            await discover(client)
            return 0

    await record(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
