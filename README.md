# StableCard Rail

[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
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

> Badge URLs point at `OWNER/REPO` until this is pushed to a GitHub remote.

## Build status

Built in the phase order defined in SPEC.md §12. Each phase ends green: tests
pass, lint and types are clean, and the phase is demo-able.

| # | Phase | Status |
|---|---|---|
| 1 | Skeleton + ledger + state machine | **done** |
| 2 | Issuer abstraction + mock adapter | — |
| 3 | Lithic adapter | — |
| 4 | Stripe Issuing adapter | — |
| 5 | Solana watcher + simulated bridge + auto top-up | — |
| 6 | Real bridge adapter | — |
| 7 | 3DS / OTP service | — |
| 8 | Mobile app | — |
| 9 | Fireblocks signer (stretch) | — |
| 10 | Docs + polish | — |

## Quickstart

```bash
cp .env.example .env
docker compose up --build      # postgres :5442, redis :6389, backend :8000

curl -s localhost:8000/healthz
docker compose exec backend python scripts/demo_phase1.py
```

Full walk-through, including how to run the tests and how to verify the ledger's
append-only guarantee: [`docs/DEMO.md`](docs/DEMO.md).

## What phase 1 contains

```
backend/app/
├── core/       config, engine + session factory, logging, Money
├── funding/    FundingState + transition table, FundingIntent, machine.advance()
├── ledger/     append-only LedgerEvent, event-type constants, writer
├── api/        GET /ledger
└── main.py     app factory, GET /healthz
```

- **`advance()`** is the only writer of funding state. It enforces the legal-transition
  table, takes a row lock, is replay-safe by idempotency key, and writes exactly one
  ledger entry per transition — in the same transaction as the state change. Illegal
  transitions are ledgered *and* raised.
- **The ledger** is append-only by database trigger, not by convention, and uniquely
  keyed so replayed webhook deliveries stay safe after the Redis dedup key expires.
- **Money** is always integer minor units. Non-`int` amounts are rejected at
  construction; there is no float path anywhere in the codebase.
- **Tests**: 324, including all 121 ordered state pairs exercised against a real
  Postgres database, and a check that models have not drifted from migrations.
  Coverage on the gated packages is 100% against SPEC.md §10's 60% floor.

Directories for later phases are absent rather than empty — the tree shows exactly
what has been built.

## Security

No secret, key, or token is ever committed. `.env` is gitignored;
[`.env.example`](.env.example) documents every variable before it is read anywhere
in the code. Tests never call live sandbox APIs.
