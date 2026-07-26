# BSC testnet fixtures

The destination half of the phase-6 route (docs/ARCHITECTURE.md §10.1). **Every
file here is recorded** — captured verbatim from
`https://data-seed-prebsc-1-s1.bnbchain.org:8545`, which is public and needs no
key. Nothing in this directory was authored from documentation, and where one of
these disagrees with the client, **the fixture is right and the client is what
changes.**

Regenerate with `python scripts/record_evm_fixtures.py`. Every call it makes is a
read; it never writes to the chain and needs no key. **The test suite never calls
the node** (SPEC.md §10) — it replays these through `respx`.

Nothing is redacted. Every byte is public chain data, and `receipt_success` belongs
to a stranger's testnet transaction we only ever read.

| File | What it is |
| --- | --- |
| `chain_id` | `0x61` — 97, BSC testnet. Checked against configuration before anything is signed, because a chain-id mismatch is reported as a signature problem and sends you looking at the key |
| `gas_price` | the node's suggestion, in wei. BSC testnet is priced as a legacy chain in practice, which is what the redeemer builds for |
| `transaction_count` | the nonce call in its `pending` form, for an address with no history |
| `call_wrapped_asset` | `wrappedAsset(1, <devnet USDC>)` → `0x51a3cc54…ac08`. The proof the destination side of the route is already open: the asset is attested, so this phase runs no `create_wrapped` |
| `call_wrapped_asset_unattested` | the same call for a token nobody attested. **A zero inside a 200** — not an error and not a null. Read as an address it is `0x0`, which on this bridge is a burn |
| `call_transfer_not_completed` | `isTransferCompleted(<unknown hash>)` → false. The answer for every transfer that has not been redeemed, and the call that makes `status()` restartable |
| `error_execution_reverted` | a real revert: code **3**, with the reason in the `message` twice — once as text, once ABI-encoded — plus a `data` field. A refusal, never a "try again" |
| `error_method_not_found` | `-32601`. Also a complaint about the request, and also not retried |
| `receipt_success` | a real successful receipt: `status: "0x1"`, `gasUsed`, and four logs |
| `receipt_missing` | `eth_getTransactionReceipt` for a hash the node has never seen: **`result: null` inside a 200**, exactly like Solana's `getTransaction`. It means "not mined yet" — reading it as a failure fails a redemption that was about to succeed |

## The one that is not a fixture

`error_execution_reverted` was provoked honestly: `completeTransfer` called with
four bytes that are not a VAA, which the core bridge rejects with *VM version
incompatible*. That is a genuine refusal from the genuine contract, and it is the
shape a redemption that cannot succeed arrives in — but note that it is **not** the
"already redeemed" revert. That one needs a VAA that really was delivered, and it
arrives in §10 as part of the live verification rather than as a guess here.
