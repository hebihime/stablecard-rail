# Solana devnet fixtures

Two kinds of file live here, and the difference is the difference between evidence
and a reading of documentation. Do not blur them.

Regenerate with `python scripts/record_solana_fixtures.py`. **No credentials**:
Solana's devnet RPC is public and every call the script makes is read-only, which
is why almost everything here is recorded rather than authored. **The test suite
never calls the RPC** (SPEC.md §10) — it replays these through `respx`.

Nothing is redacted. Every byte is public chain data and the addresses belong to
strangers' devnet accounts we only ever read.

## 1. Recorded — the node's own words

Captured verbatim from `https://api.devnet.solana.com`. Where one of these
disagrees with the watcher, **the fixture is right and the watcher is what
changes.**

| File | What it is |
| --- | --- |
| `signatures_for_deposit_account` | `getSignaturesForAddress` for a real USDC token account: five entries, newest first, with `slot`, `blockTime`, `err` and `transactionIndex` |
| `transaction_transfer_checked` | the deposit — a `transferChecked` of 1.000000 USDC, `jsonParsed`, with `createIdempotent` ahead of it because the sender paid for the recipient's account |
| `transaction_failed` | a transaction that ran and failed: `err` is `{"InstructionError": [0, {"Custom": 1}]}` **and it still carries `postTokenBalances`** |
| `signatures_none` | the same polling call against an address with no history |
| `transaction_not_found` | `getTransaction` for a signature never on chain — `result: null`, *not* an error |
| `error_invalid_address` | a malformed pubkey: `-32602 Invalid param: Invalid` |
| `error_rate_limited` | a genuine `429 Too many requests for a specific RPC call` |

Three of these exist because the obvious guess was wrong when tried:

- **`transaction_failed` still has `postTokenBalances`.** A watcher that reads
  balances without checking `err` credits deposits that never happened. The
  balances are the *simulated* ones from before the failure.
- **`transaction_not_found` is `result: null` inside a 200.** Nothing about the
  HTTP status or the JSON-RPC envelope says "missing", so an unwary client reads a
  successful empty response and moves its cursor past a transaction it never saw.
- **`error_rate_limited` is a 429 inside a 200-shaped body**, from a public
  endpoint that refuses some methods outright. It is recorded from
  `getTokenLargestAccounts`, which that endpoint declines on the first call —
  nothing here hammers a free service to manufacture a fixture.

The signature list is worth reading once: it is newest-first, and
`transaction_transfer_checked` is its *oldest* entry. That ordering is the reason
the watcher reverses a page before processing it — a deposit must be observed in
the order the chain applied it.

## 2. Derived — the recording with one stated mutation

Deposits devnet will not produce on demand, made by `derive_variants()` from
`transaction_transfer_checked`: real field names, real nullability, one documented
change each. Waiting for a stranger to make a first deposit into a fresh account,
or to send a fraction of a cent, would be waiting on somebody else's behaviour to
test ours.

| File | Mutation | What it pins |
| --- | --- | --- |
| `transaction_first_deposit` | the destination's `preTokenBalances` entry removed | a token account created *by* the transfer has no prior entry at all, not a zero one — read as zero, or a card's opening deposit is invisible |
| `transaction_dust_deposit` | credited amount reduced to a single base unit | USDC has six decimals and a USD card has two, so 0.000001 USDC rounds to nothing and must not open an intent for zero |

## What no fixture can settle

**Whether devnet is up**, and how it behaves under a real backlog. The retry and
backoff path is tested against `error_rate_limited`, but the *timing* of a real
rate limit is not something a fixture holds.

**Finality.** These are all `commitment: finalized` responses, which is what the
watcher asks for (SPEC.md §5.2). A fixture cannot demonstrate that a `confirmed`
transaction can still be dropped — that is why the commitment is not configurable
down.
