# Guardian API fixtures

Recorded from `https://api.testnet.wormholescan.io`, which is public, keyless and
read-only. **The test suite never calls it** (SPEC.md §10) — it replays these
through `respx`. Regenerate with `python scripts/record_wormhole_fixtures.py`.

| File | What it is |
| --- | --- |
| `vaa_token_transfer` | a real signed VAA from the Solana devnet Token Bridge emitter — sequence 56910, one guardian signature, guardian set 0 — base64 in a JSON field, **with the explorer's own `digest`** |
| `vaa_not_signed_yet` | the **404** for a sequence the guardians have not signed: `{"code": 5, "message": "NOT FOUND"}` |

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
