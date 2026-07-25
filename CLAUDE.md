# CLAUDE.md

## Read first

**`WORKLOG.local.md`** (repo root, gitignored) — current state, what is blocked on a
decision, machine-specific ports and databases, and traps already paid for once.
Read it before doing anything else, and update it before finishing a piece of work.

Then, as needed: `SPEC.md` is the source of truth; `docs/ARCHITECTURE.md` records
every design decision and why; `docs/DEMO.md` is how to run it.

## What this is

Sandbox-scale card-funding pipeline (FastAPI + React Native), built as a portfolio
project. Testnets and provider sandboxes only.

## Working agreement

- Build strictly in **SPEC.md §12 phase order**. No scaffolding ahead of the current
  phase — directories for unbuilt phases stay absent, not empty. **Stop for review at
  each phase boundary.**
- **Commit each coherent sub-step as it goes green.** Never wait for a phase boundary
  and never bulk-commit. Run the gate before every commit so each one is green alone.
- **TDD the financial core** — funding state machine, webhook dedup, issuer adapters.
  Tests first, then the implementation.
- Record every decision the spec leaves open in `docs/ARCHITECTURE.md`, with the
  reasoning, not just the outcome.

## Hard rules

- **No secret, key or token in a tracked file.** Everything via env vars, documented
  in `.env.example` *before* it is read anywhere in code.
- **Tests never call a live sandbox API.** Recorded fixtures and mocks only.
- Money is always **integer minor units** — never floats. External identifiers are
  opaque strings. Timestamps are UTC.
- Every module outside `issuers/` imports only `issuers/base.py` and
  `issuers/registry.py`. If an adapter would require a change in `funding/`,
  `ledger/`, `webhooks/` or `api/`, **fix the abstraction** rather than patch around
  it. `backend/tests/test_module_boundaries.py` enforces this.
- Ask before adding a dependency beyond SPEC.md §2.

## The gate

```bash
docker compose up -d postgres redis          # ports 5442 / 6389, not the defaults
cd backend && source .venv/bin/activate
pytest --cov --cov-report=term-missing       # coverage floor 60% per SPEC.md §10
ruff check . && ruff format --check . && mypy
```
