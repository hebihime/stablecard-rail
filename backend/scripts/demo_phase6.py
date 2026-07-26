"""Phase 6 demo: the real bridge, proved against the chains themselves.

    python scripts/demo_phase6.py              # verify the route + replay a VAA
    python scripts/demo_phase6.py --transfer   # actually move devnet USDC (spends)

**The default mode reads two chains and needs no credentials.** It asks Solana
devnet and whichever EVM testnet `EVM_*` names — BSC testnet by default — the six
questions that decide whether this route exists at all, and prints what each
answered, which is how the route was chosen in the first place
(docs/ARCHITECTURE.md §10.1). If Wormhole ever retires the devnet
deployment, this is the script that says so, in one screen, instead of a transfer
failing somewhere less obvious.

It then replays the recorded VAA to show the two things the integration turns on:
the **double-keccak digest** the destination identifies a transfer by, and the
**derived message account** that makes `submit` idempotent without any help from
the protocol.

`--transfer` is the real thing and it spends real testnet money: it submits a
Solana devnet transfer through the adapter, waits for the guardians, and redeems on
BSC testnet. It needs `SOLANA_DEPOSIT_KEYPAIR` holding devnet USDC and SOL, and
`EVM_REDEEMER_PRIVATE_KEY` holding test BNB. Without them it says which is missing
and stops — nothing here half-runs.

Note what this demo is *not*: the funding pipeline. That is `demo_phase5.py`, and
SPEC.md §5.2 keeps it on the simulator on purpose, so a walk-through cannot fail
because somebody else's testnet is down.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import uuid
from pathlib import Path

from solders.pubkey import Pubkey

from app.chain.bridge.base import BridgeOrder, BridgeStatus
from app.chain.bridge.build import BridgeChoice, build_bridge
from app.chain.bridge.wormhole.accounts import (
    emitter_address,
    message_keypair_from_signature,
    message_seed_payload,
)
from app.chain.bridge.wormhole.adapter import WormholeBridge
from app.chain.bridge.wormhole.config import get_wormhole_settings
from app.chain.bridge.wormhole.vaa import parse_signed_vaa, parse_token_transfer
from app.chain.config import get_solana_settings
from app.chain.evm.abi import WRAPPED_ASSET, decode_address, encode_uint16_and_bytes32, selector
from app.chain.evm.config import get_evm_settings
from app.chain.evm.rpc import EvmRpcClient
from app.chain.rpc import SolanaRpcClient
from app.chain.signer import LocalKeypairSigner, SignerError
from app.chain.tokens import associated_token_address, base58_to_bytes32
from app.core.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "wormhole"

#: `bridgeContracts(uint16)` — which foreign Token Bridge the destination trusts
#: for a chain. The single most important answer in the whole verification: if the
#: Solana emitter is not registered there, every redemption reverts.
BRIDGE_CONTRACTS = "bridgeContracts(uint16)"

#: `getCurrentGuardianSetIndex()` on the destination core bridge. It has to match
#: the set the guardians are signing with, or a fresh VAA cannot be verified.
GUARDIAN_SET_INDEX = "getCurrentGuardianSetIndex()"

#: `wormhole()` on a Token Bridge — the core bridge it verifies VAAs with. Asked
#: rather than configured, so this script follows `EVM_*` to whatever chain it is
#: pointed at. An earlier version hard-coded BSC testnet's, which would have read
#: an address with no code on any other chain and crashed on the empty answer.
CORE_BRIDGE_OF = "wormhole()"

TICK = "✓"
CROSS = "✗"


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def line(ok: bool, label: str, detail: str) -> None:
    print(f"  {TICK if ok else CROSS} {label:<34} {detail}")


async def verify_route() -> bool:
    """The six questions that decide whether this route exists. All read-only."""
    solana_settings = get_solana_settings()
    wormhole = get_wormhole_settings()
    evm = get_evm_settings()

    solana = SolanaRpcClient(rpc_url=solana_settings.rpc_url)
    destination = EvmRpcClient(rpc_url=evm.rpc_url)

    token_bridge = Pubkey.from_string(wormhole.token_bridge_program)
    emitter = emitter_address(token_bridge)
    all_good = True

    heading(f"the source chain — {solana_settings.rpc_url}")
    for label, address in (
        ("core bridge", wormhole.core_program),
        ("token bridge", wormhole.token_bridge_program),
    ):
        account = await solana.get_account_info(address)
        executable = bool(account and account.get("executable"))
        all_good &= executable
        line(executable, f"{label} deployed", f"{address[:12]}… executable={executable}")

    mint = await solana.get_account_info(solana_settings.usdc_mint)
    all_good &= mint is not None
    line(mint is not None, "USDC mint exists", solana_settings.usdc_mint)
    print(f"    the emitter a VAA will name: {emitter}")

    heading(f"the destination chain — {evm.rpc_url}")
    chain_id = await destination.chain_id()
    matches = chain_id == evm.chain_id
    all_good &= matches
    line(matches, "chain id", f"node says {chain_id}, configured {evm.chain_id}")

    registered = await destination.call_contract(
        to=wormhole.destination_token_bridge,
        data=selector(BRIDGE_CONTRACTS) + (wormhole.source_chain_id).to_bytes(32, "big"),
    )
    trusted = bytes.fromhex(registered.removeprefix("0x"))
    same = trusted == bytes(emitter)
    all_good &= same
    line(
        same,
        f"trusts chain {wormhole.source_chain_id}'s bridge",
        f"{trusted.hex()[:16]}…" + ("" if same else " ← NOT our emitter"),
    )

    wrapped_word = await destination.call_contract(
        to=wormhole.destination_token_bridge,
        data=selector(WRAPPED_ASSET)
        + encode_uint16_and_bytes32(
            wormhole.source_chain_id, base58_to_bytes32(solana_settings.usdc_mint)
        ),
    )
    wrapped = decode_address(wrapped_word)
    attested = int(wrapped, 16) != 0
    all_good &= attested
    line(attested, "wrapped USDC attested", wrapped if attested else "none — needs create_wrapped")

    core = decode_address(
        await destination.call_contract(
            to=wormhole.destination_token_bridge, data=selector(CORE_BRIDGE_OF)
        )
    )
    guardian_word = await destination.call_contract(to=core, data=selector(GUARDIAN_SET_INDEX))
    print(f"    core bridge {core}, guardian set {int(guardian_word, 16)}")

    return bool(all_good)


def replay_vaa() -> None:
    """The two mechanisms, on recorded bytes: the digest and the message account."""
    record = json.loads((FIXTURES / "vaa_token_transfer.json").read_text())["data"]
    vaa = parse_signed_vaa(base64.b64decode(record["vaa"]))
    transfer = parse_token_transfer(vaa.payload)

    heading("a recorded VAA, read the way the adapter reads it")
    print(f"  identity        {vaa.vaa_id}")
    print(f"  signatures      {vaa.signature_count} (guardian set {vaa.guardian_set_index})")
    print(f"  observed at     {vaa.observed_at.isoformat()}")
    print(f"  amount          {transfer.amount} (8-decimal normalized)")
    print(f"  to              {transfer.to_evm_address} on wormhole chain {transfer.to_chain}")
    print(f"  digest          {vaa.digest.hex()}")
    print(f"  explorer digest {record['digest']}")
    line(
        vaa.digest.hex() == record["digest"],
        "double keccak agrees",
        "the destination stores delivered transfers under this",
    )


async def replay_idempotency() -> None:
    """Why a duplicate submit cannot lock a second amount."""
    heading("the message account, which is where idempotency comes from")
    keypair = LocalKeypairSigner.from_env_value(_demo_keypair())
    order_ref = "11111111-2222-3333-4444-555555555555"

    first = message_keypair_from_signature(await keypair.sign(message_seed_payload(order_ref)))
    again = message_keypair_from_signature(await keypair.sign(message_seed_payload(order_ref)))
    other = message_keypair_from_signature(await keypair.sign(message_seed_payload("other-order")))

    print(f"  order {order_ref}")
    print(f"    message account   {first.pubkey()}")
    print(f"    asked again       {again.pubkey()}")
    line(first.pubkey() == again.pubkey(), "same order, same account", "so a retry collides")
    line(first.pubkey() != other.pubkey(), "another order, another", f"{other.pubkey()}")
    print("    a second submit finds that account on chain and reads its sequence back,")
    print("    rather than locking a second amount — the protocol has no idempotency key.")


def _demo_keypair() -> str:
    """A configured keypair if there is one, otherwise a throwaway.

    The derivation is a signature, so it needs *a* key — but not a funded one and
    not necessarily the real one, which is what keeps the default mode
    credential-free while still demonstrating the real mechanism.

    Read through settings rather than `os.getenv`: `.env` is a file
    pydantic-settings reads and the process environment does not, so `os.getenv`
    here would ignore a key that is configured and quietly show a throwaway
    instead. `demo_phase5.py` has that bug; this does not.
    """
    configured = get_solana_settings().deposit_keypair.strip()
    if configured:
        return configured

    from solders.keypair import Keypair

    print("  (no SOLANA_DEPOSIT_KEYPAIR configured — deriving with a throwaway key)")
    return json.dumps(list(bytes(Keypair.from_seed(bytes([21]) * 32))))


async def live_transfer(amount_minor: int) -> int:
    """Submit a real transfer and drive it to delivery."""
    solana_settings = get_solana_settings()
    evm = get_evm_settings()

    if not solana_settings.deposit_keypair.strip():
        print("\n  SOLANA_DEPOSIT_KEYPAIR is not set; --transfer needs it (see .env.example)")
        return 1
    if not evm.redeemer_private_key.strip():
        print("\n  EVM_REDEEMER_PRIVATE_KEY is not set; --transfer needs it to pay gas")
        return 1

    try:
        bridge = build_bridge(BridgeChoice.WORMHOLE)
    except SignerError as exc:
        print(f"\n  cannot build the real bridge: {exc}")
        return 1
    assert isinstance(bridge, WormholeBridge)

    signer = LocalKeypairSigner.from_env_value(solana_settings.deposit_keypair)
    source = associated_token_address(signer.pubkey, Pubkey.from_string(solana_settings.usdc_mint))
    order = BridgeOrder(
        order_ref=str(uuid.uuid4()),
        amount=Money(amount_minor=amount_minor, currency="USD"),
        source_chain="solana-devnet",
        destination_chain="bsc-testnet",
        destination_address=_recipient(),
    )

    heading("submitting a real transfer")
    print(f"  from   {source} (owner {signer.public_key})")
    print(f"  to     {order.destination_address} on BSC testnet")
    print(f"  amount {order.amount}")
    print(f"  order  {order.order_ref}")

    transfer = await bridge.submit(order)
    print(f"\n  accepted as {transfer.bridge_ref}")
    print(f"  source signature {transfer.raw.get('source_signature')}")

    heading("waiting for the guardians, then redeeming")
    for attempt in range(1, 41):
        current = await bridge.status(transfer.bridge_ref)
        stage = current.raw.get("stage", "delivered")
        print(f"  poll {attempt:>2}  {current.status.value:<10} {stage}")
        if current.status is BridgeStatus.COMPLETED:
            print(f"\n  delivered: {current.amount_out} arrived")
            return 0
        if current.status is BridgeStatus.FAILED:
            print(f"\n  failed: {current.failure_reason}")
            return 1
        await asyncio.sleep(15)

    print("\n  still pending after ten minutes; the reference above is all the worker needs")
    return 0


def _recipient() -> str:
    """Where the wrapped USDC should land on BSC testnet.

    The redeemer's own address, because a transfer to an address nobody holds
    proves less than it looks: `completeTransfer` credits whoever the VAA names,
    and watching that balance move is the point. Deliberately not configurable —
    a knob read with `os.getenv` would ignore `.env` and send funds somewhere the
    operator did not choose.
    """
    from app.chain.evm.signer import LocalPrivateKeySigner

    return LocalPrivateKeySigner.from_env_value(get_evm_settings().redeemer_private_key).address


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transfer", action="store_true", help="move real devnet USDC (needs both keys)"
    )
    parser.add_argument(
        "--amount", type=int, default=100_000, help="minor units to move; default 0.100000 USDC"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    print("\033[1mphase 6 — the real bridge: Solana devnet → BSC testnet\033[0m")
    healthy = await verify_route()
    replay_vaa()
    await replay_idempotency()

    if not healthy:
        print("\n  the route did not verify; see the crosses above. docs/ARCHITECTURE.md §10.1")
        print("  says what each check proves and what to do if one of them changes.")
        return 1

    print("\n  the route verifies end to end, against both chains.")
    if not args.transfer:
        print("  `--transfer` moves real devnet USDC through it.")
        return 0

    return await live_transfer(args.amount)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
