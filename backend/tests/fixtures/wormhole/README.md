# Guardian API fixtures

Recorded from `https://api.testnet.wormholescan.io`, which is public, keyless and
read-only. **The test suite never calls it** (SPEC.md §10) — it replays these
through `respx`. Regenerate with `python scripts/record_wormhole_fixtures.py`.

| File | What it is |
| --- | --- |
| `vaa_token_transfer` | a real signed VAA from the Solana devnet Token Bridge emitter — sequence 56910, one guardian signature, guardian set 0 — base64 in a JSON field, **with the explorer's own `digest`** |
| `vaa_not_signed_yet` | the **404** for a sequence the guardians have not signed: `{"code": 5, "message": "NOT FOUND"}` |
| `transfer_native_transaction` | the devnet transaction that *produced* that VAA, in `json` encoding — seventeen accounts in order and 55 bytes of Borsh. This is the instruction's ABI as the chain accepted it |
| `posted_message_account` | the message account that transfer created, base64. Every field agrees with the VAA and with the instruction that wrote it |

## Why each one is more than a sample

**`vaa_token_transfer` carries a hash this repository did not compute.** The
destination Token Bridge stores delivered transfers under
`keccak256(keccak256(body))` — a *double* keccak — and getting that wrong makes
every delivered transfer look undelivered and every redemption look like a
duplicate. Wormhole's `Messages.sol` says it, under a comment reading *"SECURITY:
Do not change the way the hash of a VM is computed!"*, and the `digest` field here
says it independently. `test_wormhole_vaa.py` asserts both, and asserts that the
single keccak is *not* equal to it.

It also pins two things a mainnet assumption would get wrong: the testnet guardian
set has **one** signature where mainnet has nineteen (a length assumption from
mainnet misplaces the body by 1188 bytes), and the payload is type **1**, whose
last field is a relayer fee — type 3 replaces that field with a sender, so reading
a fee off one would read the first bytes of somebody's message as money.

**`vaa_not_signed_yet` is the most misreadable answer in the integration.** Every
transfer looks like this for its first seconds. It is not an error and it is not "no
such transfer": it is *not yet*. Read as an error it fails healthy transfers; read
as absence it strands them. The client returns `None` and does not retry — the same
distinction Solana's `getTransaction` null and `eth_getTransactionReceipt`'s null
draw on the chains either side of this one.

## Why the transaction is recorded in `json` and not `jsonParsed`

`jsonParsed` is friendlier and drops the one thing that matters: the per-instruction
**account indices**. The order of those accounts *is* the instruction's ABI, and a
hand-built instruction with two accounts transposed fails with a program error that
says nothing about ordering. So `test_wormhole_accounts.py` derives all eight PDAs
and asserts the whole seventeen-account list against this fixture, and
`test_wormhole_instructions.py` asserts the bytes.

Reading this transaction is also how two things were found that no documentation
mentioned: a transfer needs an SPL **`approve`** to the `authority_signer` PDA
first (instruction 3 in the recorded transaction, for exactly the transfer amount),
and the amount in the instruction is the token's own — `625600000` at nine
decimals — while the VAA carries `62560000` at eight. That is Wormhole's
normalization, visible in a single pair of recorded numbers.
