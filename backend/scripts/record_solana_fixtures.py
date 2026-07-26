"""Record the Solana watcher's fixtures from devnet.

    python scripts/record_solana_fixtures.py            # write tests/fixtures/solana/
    python scripts/record_solana_fixtures.py --dry-run  # call, print, write nothing

No credentials: Solana's devnet RPC is public and everything here is **read-only**.
That is the whole reason these are recorded rather than authored — a JSON-RPC
response is cheap to observe and expensive to guess, and phase 4 was taught that
lesson by a documentation-derived fixture that passed its own test and failed live
(docs/ARCHITECTURE.md §8.10).

**The test suite never runs this** (SPEC.md §10). It replays what this wrote.

What it records, and why each one is here:

* `signatures_for_deposit_account` — `getSignaturesForAddress` for a real devnet
  USDC token account, newest-first, which is the watcher's polling call.
* `transaction_transfer_checked` — the deposit itself: a `transferChecked` of
  1.000000 USDC into that account, in `jsonParsed` encoding.
* `transaction_not_found` — `getTransaction` for a signature that does not exist.
  A `result` of `null`, not an error, which is the trap: the watcher must not read
  it as a failed transfer.
* `signatures_none` — the same call for an address with no history at all.
* `error_rate_limited` — a real 429 body. The public endpoint refuses
  `getTokenLargestAccounts` outright, so one request provokes it honestly; nothing
  here hammers a free endpoint to produce a fixture.
* `error_invalid_address` — what the node says about a malformed pubkey.

**One call here is not reproducible, and it cost a red suite once.**
`signatures_for_deposit_account` asks for the newest five signatures on a real
account, so it answers with whatever is *current* — a re-record moves it, and the
watcher tests read specific entries out of it. Nothing can pin it: `until` and
`before` both slide with new activity. Use `--only <name>` when adding a fixture so
the window stays where it is, and expect to re-check `test_solana_watcher.py` if you
ever move it deliberately.

The account is chosen at record time by walking recent devnet USDC activity, and
pinned in `RECORDED` below so a re-run reproduces the same files. Nothing is
redacted: every byte is public chain data, and the addresses are strangers' devnet
accounts we only ever read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "solana"

DEVNET_RPC = "https://api.devnet.solana.com"

#: Circle's USDC mint on devnet.
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

#: Pinned so a re-run records the same transfer rather than whatever is recent.
#: Found by walking `getSignaturesForAddress` on the mint (see `--discover`).
RECORDED_DEPOSIT_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
RECORDED_DEPOSIT_SIGNATURE = (
    "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
)

#: A devnet USDC transaction that ran and failed, for the `err` shape. Failed
#: transactions still cost a fee and still appear in `getSignaturesForAddress`,
#: which is exactly why the watcher has to look.
RECORDED_FAILED_SIGNATURE = (
    "4JiKtK1zTFPNvvmxAL76YugDSseogWgPnCJvjxHy4DsGYYYTzuTjAFbhfmczbchrksUzLATaDAE7FhtjVAgCTTzL"
)

#: A syntactically valid signature that has never been on chain: 64 zero bytes,
#: which is `Signature.default()`. Recorded rather than invented because the
#: obvious guess — 87 base58 `1`s — is refused as `Invalid param: WrongSize`.
ABSENT_SIGNATURE = "1" * 64

#: A valid pubkey nobody transacts with. The system program's incinerator was the
#: first choice and it has plenty of history, so this is a PDA off the system
#: program with a seed nothing else would pick:
#:     Pubkey.find_program_address([b"stablecard-rail-never-used"], SYSTEM_PROGRAM)
#: Being a PDA it is off the ed25519 curve, so no keypair can ever sign for it.
UNUSED_ADDRESS = "6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG"

#: Wormhole's Token Bridge on devnet — an account that exists and is executable,
#: recorded (with a `dataSlice`) purely for the shape of a present account. Phase
#: 6 uses the program itself; see docs/ARCHITECTURE.md §10.1.
TOKEN_BRIDGE_PROGRAM = "DZnkkTmCiFWfYTfT41X3Rd1kDgozqzxWaHqsw6W4x2oe"

#: The public endpoint rate-limits readily, and politely spacing calls is the
#: difference between recording fixtures and being a nuisance.
PAUSE_SECONDS = 0.7
RATE_LIMIT_BACKOFF_SECONDS = (2.0, 5.0, 15.0)


class Recorder:
    def __init__(
        self, client: httpx.AsyncClient, *, dry_run: bool, only: frozenset[str] | None = None
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        #: Which fixtures to write. `None` means all of them — but see `--only`:
        #: a full re-record moves `signatures_for_deposit_account`, because that
        #: call answers with *current* activity and no parameter can pin it.
        self.only = only
        self.written: list[str] = []

    async def call(
        self, name: str, method: str, params: list[Any], *, allow_error: bool = False
    ) -> dict[str, Any]:
        """One JSON-RPC call, written out verbatim under `name`."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for pause in (*RATE_LIMIT_BACKOFF_SECONDS, None):
            response = await self.client.post("", json=body)
            payload: dict[str, Any] = response.json()
            rate_limited = payload.get("error", {}).get("code") == 429
            if not rate_limited or pause is None or allow_error:
                break
            print(f"  rate limited, retrying in {pause}s")
            await asyncio.sleep(pause)

        if "error" in payload and not allow_error:
            raise SystemExit(f"{method} failed: {payload['error']}")

        self.write(name, payload)
        await asyncio.sleep(PAUSE_SECONDS)
        return payload

    def write(self, name: str, payload: Any) -> None:
        if self.only is not None and name not in self.only:
            print(f"  {name}: skipped (not in --only)")
            return
        summary = json.dumps(payload)[:90]
        print(f"  {name}: {summary}{'…' if len(summary) == 90 else ''}")
        if self.dry_run:
            return
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / f"{name}.json").write_text(json.dumps(payload, indent=2) + "\n")
        self.written.append(name)


async def walk(rec: Recorder) -> None:
    print("the watcher's polling call")
    await rec.call(
        "signatures_for_deposit_account",
        "getSignaturesForAddress",
        [RECORDED_DEPOSIT_ACCOUNT, {"limit": 5, "commitment": "finalized"}],
    )

    print("the deposit itself")
    await rec.call(
        "transaction_transfer_checked",
        "getTransaction",
        [
            RECORDED_DEPOSIT_SIGNATURE,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "finalized",
            },
        ],
    )

    print("an address with no history, and a signature that was never on chain")
    await rec.call(
        "signatures_none",
        "getSignaturesForAddress",
        [UNUSED_ADDRESS, {"limit": 5, "commitment": "finalized"}],
    )
    await rec.call(
        "transaction_not_found",
        "getTransaction",
        [
            ABSENT_SIGNATURE,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "finalized",
            },
        ],
    )

    print("a transaction that ran and failed")
    await rec.call(
        "transaction_failed",
        "getTransaction",
        [
            RECORDED_FAILED_SIGNATURE,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "finalized",
            },
        ],
    )

    print("the three reads phase 6 added, for building and sending a transaction")
    # A program account, which exists and is executable. `dataSlice` keeps the
    # fixture small: nobody needs 300KB of BPF to test a decoder.
    await rec.call(
        "account_info_program",
        "getAccountInfo",
        [
            TOKEN_BRIDGE_PROGRAM,
            {
                "encoding": "base64",
                "commitment": "finalized",
                "dataSlice": {"offset": 0, "length": 8},
            },
        ],
    )
    # An account nobody has created. `value: null` inside a 200 — the answer the
    # bridge adapter reads as "nothing has been submitted for this order yet".
    await rec.call(
        "account_info_missing",
        "getAccountInfo",
        [UNUSED_ADDRESS, {"encoding": "base64", "commitment": "finalized"}],
    )
    await rec.call("latest_blockhash", "getLatestBlockhash", [{"commitment": "finalized"}])

    print("two error shapes")
    await rec.call(
        "error_invalid_address",
        "getSignaturesForAddress",
        ["not-a-pubkey", {"limit": 1}],
        allow_error=True,
    )
    # The public endpoint refuses this method outright rather than under load, so
    # one call produces a genuine 429 body without abusing a free service.
    await rec.call(
        "error_rate_limited",
        "getTokenLargestAccounts",
        [USDC_DEVNET_MINT, {"commitment": "finalized"}],
        allow_error=True,
    )


def derive_variants(rec: Recorder) -> None:
    """Two deposits devnet will not hand over on demand, each one stated mutation
    away from the recorded transfer. Same category as the Stripe fixtures'
    `derive_variants()`: real field names, real nullability, one documented change.

    Waiting for a stranger to make a first deposit into a fresh account, or to
    send a fraction of a cent, would be waiting on somebody else's behaviour to
    test ours.
    """
    recorded = json.loads((FIXTURES / "transaction_transfer_checked.json").read_text())

    # 1. A *first* deposit. A token account that did not exist before the transfer
    #    has no `preTokenBalances` entry at all — not a zero one. The watcher must
    #    read a missing entry as zero, or a card's opening deposit is invisible.
    first = json.loads(json.dumps(recorded))
    meta = first["result"]["meta"]
    credited = {balance["accountIndex"] for balance in meta["postTokenBalances"]}
    meta["preTokenBalances"] = [
        balance for balance in meta["preTokenBalances"] if balance["accountIndex"] not in credited
    ]
    rec.write("transaction_first_deposit", first)

    # 2. A deposit below one cent. USDC has six decimals and a USD card has two,
    #    so 0.000001 USDC rounds to nothing and must not open an intent for zero.
    dust = json.loads(json.dumps(recorded))
    meta = dust["result"]["meta"]
    destination = meta["postTokenBalances"][0]["accountIndex"]
    for balance in meta["preTokenBalances"]:
        if balance["accountIndex"] == destination:
            base_units = int(balance["uiTokenAmount"]["amount"])
    for balance in meta["postTokenBalances"]:
        if balance["accountIndex"] == destination:
            amount = balance["uiTokenAmount"]
            amount["amount"] = str(base_units + 1)
            amount["uiAmount"] = None
            amount["uiAmountString"] = "0.000001"
    rec.write("transaction_dust_deposit", dust)


async def discover(rec: Recorder) -> None:
    """Print recent devnet USDC transfers, to re-pin `RECORDED_*` if they age out."""
    listed = await rec.call(
        "_discover_signatures",
        "getSignaturesForAddress",
        [USDC_DEVNET_MINT, {"limit": 25, "commitment": "finalized"}],
    )
    for entry in listed["result"]:
        if entry["err"]:
            continue
        detail = await rec.call(
            "_discover_transaction",
            "getTransaction",
            [
                entry["signature"],
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "finalized",
                },
            ],
        )
        result = detail.get("result")
        if not result:
            continue
        kinds = {
            instruction.get("parsed", {}).get("type")
            for instruction in result["transaction"]["message"]["instructions"]
            if isinstance(instruction.get("parsed"), dict)
        }
        if {"transfer", "transferChecked"} & kinds and result["meta"]["postTokenBalances"]:
            print(f"  candidate: {entry['signature']} slot={entry['slot']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="call and print, write nothing")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="find a current devnet USDC transfer to pin, instead of recording",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help=(
            "write only these fixtures. Use it to add one without moving "
            "signatures_for_deposit_account, which is a window of live activity"
        ),
    )
    args = parser.parse_args()

    print(f"recording against {DEVNET_RPC} (read-only, no credentials)")
    async with httpx.AsyncClient(base_url=DEVNET_RPC, timeout=30.0) as client:
        rec = Recorder(
            client,
            dry_run=args.dry_run or args.discover,
            only=frozenset(args.only) if args.only else None,
        )
        if args.discover:
            await discover(rec)
            return 0
        await walk(rec)

    if not args.dry_run and not args.only:
        print("derived from the recording")
        derive_variants(rec)

    where = "(dry run, nothing written)" if args.dry_run else str(FIXTURES)
    print(f"\n{len(rec.written)} fixtures {where}")
    for name in rec.written:
        print(f"  {name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
