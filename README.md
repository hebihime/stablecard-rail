# StableCard Rail

[![CI](https://github.com/hebihime/stablecard-rail/actions/workflows/ci.yml/badge.svg)](https://github.com/hebihime/stablecard-rail/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)
![python](https://img.shields.io/badge/python-3.12-blue)

An end-to-end card funding pipeline: self-custody Solana USDC → cross-chain bridge
→ auto top-up on a virtual card, behind a multi-provider issuer abstraction, with
signature-verified webhooks, an append-only event ledger, a database-backed funding
state machine, and a thin React Native client.

> **This is a portfolio demonstration.** Everything runs on testnets and provider
> sandboxes. No mainnet funds, no production card programs, no real cardholder
> data. It exists to show the architecture and the financial-flow engineering.

- [`SPEC.md`](SPEC.md) — the full specification and build phases
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline diagram and design decisions
- [`docs/DEMO.md`](docs/DEMO.md) — run it locally with free credentials only

## Build status

Built in the phase order defined in SPEC.md §12. Each phase ends green: tests
pass, lint and types are clean, and the phase is demo-able.

| # | Phase | Status |
|---|---|---|
| 1 | Skeleton + ledger + state machine | **done** |
| 2 | Issuer abstraction + mock adapter + webhook receiver | **done** |
| 3 | Lithic adapter | **done** |
| 4 | Stripe Issuing adapter | **done** |
| 5 | Solana watcher + simulated bridge + auto top-up | **done** |
| 6 | Real bridge adapter | **done** |
| 7 | 3DS / OTP service | — |
| 8 | Mobile app | — |
| 9 | Fireblocks signer (stretch) | — |
| 10 | Docs + polish | — |

## Quickstart

```bash
cp .env.example .env
docker compose up --build      # postgres :5442, redis :6389, backend :8000

curl -s localhost:8000/healthz
docker compose exec backend python scripts/demo_phase1.py   # the state machine
docker compose exec backend python scripts/demo_phase2.py   # issuers + webhooks

# Phase 3 talks to Lithic's sandbox, so it needs a free sandbox key in .env
cd backend && python scripts/demo_phase3.py                  # a real fiat-rail issuer

cd backend && python scripts/demo_phase5.py                  # the funding pipeline, no network
cd backend && python scripts/demo_phase6.py                  # the real bridge, verified live
```

Full walk-through — card lifecycle over HTTP, signed webhook deliveries, duplicate
suppression, the retry and dead-letter path, and how to verify the ledger's
append-only guarantee: [`docs/DEMO.md`](docs/DEMO.md).

## What exists so far

```
backend/app/
├── core/       config, engine + session factory, redis, logging, Money, UTC time,
│               ExternalError (the retryable marker both subsystems share)
├── chain/      Solana deposit watcher + slot cursor, JSON-RPC client,
│               BridgeProvider + deterministic simulator, TransactionSigner,
│               associated-token-address derivation,
│               bridge/wormhole/ (VAAs, the hand-built transfer instruction,
│               the guardian API, the redeemer) and evm/ (JSON-RPC, ABI, signer)
├── funding/    FundingState + transition table, machine.advance(), deposit intake,
│               the auto top-up engine, the reconciler, deposit routes,
│               the settlement consumer
├── issuers/    CardIssuerAdapter + normalized CardEvent, registry,
│               per-adapter settings, gnosis_pay_mock (crypto deposit),
│               lithic + stripe_issuing (fiat rails)
├── webhooks/   receiver (verify → dedup → parse → ledger → dispatch), EventBus,
│               retry queue with backoff, dead-letter table
├── ledger/     append-only LedgerEvent, event-type constants, writer
├── api/        card lifecycle, POST /webhooks/{provider_id}, GET /ledger
└── main.py     app factory, GET /healthz
```

- **`advance()`** is the only writer of funding state. It enforces the legal-transition
  table, takes a row lock, is replay-safe by idempotency key, and writes exactly one
  ledger entry per transition — in the same transaction as the state change. Illegal
  transitions are ledgered *and* raised.
- **A new issuer is one adapter file plus one registry entry**, and that is checked
  rather than asserted: a test parses the import graph and fails if anything outside
  `issuers/` reaches past `base.py` and the registry, or if an adapter reaches into
  `funding/`, `ledger/`, `webhooks/` or `api/`.
- **The real bridge is Wormhole, Solana devnet → BSC testnet**, behind the same two
  calls the simulator answers — and the engine needed no change to use it, which was
  the interface's whole bet. deBridge, which SPEC.md §5.2 names first, has no testnet
  and says so in its own FAQ; that limitation is structural to every solver-filled
  route, since nobody funds market-maker inventory on a testnet. Lock-and-mint needs
  no funded third party, so it is the kind of protocol a testnet can host.
  `scripts/demo_phase6.py` proves the route exists in six read-only calls against both
  chains, and needs no credentials.
- **The route was established by probing chains, not by reading tables** — because one
  word in those tables means two things. Wormhole marks Solana ✅ under **Testnet**
  (which for Solana is the devnet cluster) and ❌ under **Devnet** (their own Tilt
  local network), so a search for "devnet" finds the wrong answer first. What the RPC
  says: both programs deployed and executable on devnet, an emitter derived locally
  that matches what the explorer reports *and* what BSC testnet's Token Bridge trusts,
  and a wrapped USDC already attested. The Solana instruction is hand-built (no Python
  binding exists) and its bytes are asserted **byte-for-byte** against a real transfer
  the chain accepted.
- **Idempotency is constructed, because Wormhole has none.** A duplicate `submit`
  would lock a second amount, so the transfer's message account is derived from the
  order reference — from a *signature* over it, so the address is reproducible by us
  and unguessable by anyone else — and a retry finds the account already on chain and
  reads its sequence back instead of sending again.
- **`gnosis_pay_mock`** models the Gnosis Pay partner pattern, shaped on their public
  docs: a Safe smart account per user on Gnosis Chain, and funding that is a confirmed
  stablecoin transfer into it rather than an API call — so `fund_card` verifies and
  attributes a deposit it did not cause, and cannot move money. That is what proves
  the abstraction spans both `FIAT_RAIL` and `CRYPTO_DEPOSIT` providers rather than
  one shape twice. It ships a simulator that signs its own webhooks with real Ed25519,
  so the whole pipeline runs offline with no account and no fixtures to re-record.
- **`lithic`** is the first real provider, and phase 3's job was to find out where the
  abstraction did not fit. It found one place: Lithic puts the event id in a
  `webhook-id` header and sends `card.created` payloads with no timestamp at all, so
  `parse_webhook(body)` could not build a `CardEvent`. The interface widened to
  `parse_webhook(headers, body)`; `funding/`, `ledger/` and `api/` were untouched. Its
  contract tests replay fixtures recorded from the sandbox by a script that talks to
  Lithic with plain `httpx`, so the fixtures are evidence about the provider rather
  than a mirror of our own assumptions — and the recorded card response contains a
  PAN, which is why `Card.raw` is an allowlist and not a copy.
- **The webhook receiver** authenticates before it records anything, deduplicates on
  a signature-covered event id (Redis first, the ledger's unique index as the durable
  backstop), and never drops an authentic delivery — one it cannot parse is recorded
  as `unmapped` with the raw bytes attached. Once verification passes the provider
  gets a 200, even if a handler throws; failed handlers retry with backoff and are
  dead-lettered rather than lost.
- **The ledger** is append-only by database trigger, not by convention, and holds
  every card action, provider event and state transition.
- **Money** is always integer minor units. Non-`int` amounts are rejected at
  construction; there is no float path anywhere in the codebase.
- **The funding pipeline runs on its own.** A finalized USDC deposit on Solana devnet
  opens an intent and walks it to `FUNDED` — bridge order, delivery, `fund_card` —
  with `advance()` still the only writer of state and one ledger row per hop. A
  deposit is recognised by the watched account's *balance difference*, which is
  correct for a plain transfer, a `transferChecked`, a mint and a transfer three
  programs deep in a CPI; the alternative, parsing instructions, has to keep up with
  every program that can move tokens. Its fixtures are recorded from devnet, and
  three of them exist because the obvious guess was wrong — a failed transaction
  still carries token balances, a brand-new account has no prior balance entry at
  all, and `uiAmount` is a float sitting next to the integer that is the real amount.
- **Failures split three ways, not two.** A provider or bridge error marked
  retryable retries in place up to a cap; one that is not fails the intent at once;
  anything else — a bug, a database blip — propagates and leaves the intent exactly
  where it was, because `FAILED_*` is terminal and an `AttributeError` is not
  evidence that a funding failed. The reconciler then picks it up: it scans for
  intents whose state has been unchanged too long, and its backoff is elapsed time
  rather than a sleeping worker, so the schedule lives on the row and survives a
  restart.
- **The bridge is an interface with a deterministic simulator behind it**, because a
  demo that fails one run in ten for reasons nobody can see is a demo nobody trusts.
  Latency is a clock the caller supplies and failure is a mode the caller selects —
  including the one that matters, *accepted then silent*, which is the only thing
  that gives the reconciler something real to find.
- **Money that arrives is not money that lands.** The deposited amount and the
  bridged amount are separate columns, so a bridge fee stays visible as the
  difference between two recorded numbers, and the card is funded with what actually
  arrived. USDC's six decimals convert to a card's two by integer division that
  truncates and keeps the remainder; a deposit below one cent funds nothing and is
  recorded as ignored rather than dropped.
- **Tests**: 1252, against real Postgres and real Redis — all 121 ordered state pairs,
  signature pass/fail per failure mode (including Lithic's own documented worked
  example, so the scheme is pinned to a vector we did not compute), duplicate and
  out-of-order deliveries, idempotent `fund_card`, and a check that models have not
  drifted from migrations. No test calls a live provider API.
  Coverage on the five gated packages is 100% against SPEC.md §10's 60% floor — and
  that number is now *measured* rather than lucky: SQLAlchemy runs the sync DBAPI in
  a greenlet, so until `concurrency = ["greenlet", "thread"]` was set, every
  exception handler reached from an awaited query was reported as dead code.

Directories for later phases are absent rather than empty — the tree shows exactly
what has been built.

## Security

No secret, key, or token is ever committed. `.env` is gitignored;
[`.env.example`](.env.example) documents every variable before it is read anywhere
in the code. Tests never call live sandbox APIs.

Webhook signatures are verified over raw bytes with a timestamp tolerance, and the
signed material covers the event id — so the dedup key cannot be rewritten by
whoever replays a captured delivery. Card responses carry only a last-four; full
PAN/CVV reveal is a separate short-lived single-use path arriving with the mobile
client. The one signing key with a value in the repo belongs to the mock provider,
which is a Python object in this process and grants access to nothing.
