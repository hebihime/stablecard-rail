# StableCard Rail — Portfolio Demo Spec

A sandbox-scale, end-to-end card funding pipeline: self-custody Solana USDC → cross-chain bridge → auto top-up on a virtual card, with a multi-provider issuer abstraction, signature-verified webhooks, an event ledger, a DB-backed funding state machine, and a thin React Native client.

Everything runs on testnets/sandboxes. This is a demonstration of architecture and financial-flow engineering, clearly labeled as such — no mainnet funds, no production card programs.

---

## 1. Repository layout (monorepo)

```
stablecard-rail/
├── backend/               # Python / FastAPI
│   ├── app/
│   │   ├── api/           # HTTP routes (cards, funding, webhooks, otp)
│   │   ├── issuers/       # Provider abstraction + adapters
│   │   ├── chain/         # Solana watcher, signer interface, bridge providers
│   │   ├── funding/       # State machine + auto top-up engine
│   │   ├── ledger/        # Event ledger models + writers
│   │   ├── webhooks/      # Verification, dedup, dispatch
│   │   └── core/          # config, db, redis, logging
│   ├── tests/
│   ├── alembic/           # migrations
│   └── pyproject.toml
├── mobile/                # React Native (Expo acceptable for the demo)
├── docs/
│   ├── ARCHITECTURE.md    # diagrams + design decisions
│   └── DEMO.md            # how to run the full flow locally
├── docker-compose.yml     # postgres, redis, backend
└── .github/workflows/ci.yml
```

## 2. Tech stack

- Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, Pydantic v2
- PostgreSQL (state machine, ledger), Redis (idempotency keys, OTP delivery pub/sub, queues)
- `solders` / `solana-py` for Solana devnet; USDC devnet mint
- React Native (TypeScript) for the mobile client
- pytest + coverage; GitHub Actions CI with coverage gate
- No secrets in the repo — all keys via `.env` (gitignored), `.env.example` documents every variable

Deliberately out of scope (note in ARCHITECTURE.md as production-path items): Kafka (an `EventBus` interface with a Redis Streams implementation stands in; a Kafka implementation would be a drop-in), real KYC, physical cards.

## 3. Issuer abstraction layer (the centerpiece)

### 3.1 Interface

`issuers/base.py` defines an abstract `CardIssuerAdapter`:

```python
class CardIssuerAdapter(ABC):
    provider_id: str
    funding_model: FundingModel  # FIAT_RAIL | CRYPTO_DEPOSIT

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder: ...
    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card: ...
    async def activate_card(self, card_id: str) -> Card: ...
    async def freeze_card(self, card_id: str) -> Card: ...
    async def cancel_card(self, card_id: str) -> Card: ...
    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult: ...
    async def get_balance(self, card_id: str) -> Money: ...
    async def verify_webhook(self, headers: Mapping, body: bytes) -> bool: ...
    async def parse_webhook(self, body: bytes) -> CardEvent: ...   # normalize to internal event model
```

Design rule (mirrors the target role's requirement): **adding a new issuer = one new adapter file + registry entry. Zero changes to funding, ledger, webhook dispatch, or mobile code.** Enforce with a registry (`issuers/registry.py`) keyed by `provider_id`; all other modules depend only on the base interface and the normalized `CardEvent` model.

### 3.2 Adapters (three)

1. **`lithic.py`** — Lithic sandbox. Real API calls: cardholder, virtual card create/activate/freeze/cancel, simulated authorizations, webhook signature verification (their HMAC scheme).
2. **`stripe_issuing.py`** — Stripe Issuing test mode. Same lifecycle; verify `Stripe-Signature`; use Stripe's test helpers to simulate authorizations and 3DS-style flows where available.
3. **`evm_deposit_mock.py`** — a mock adapter modeling the crypto-funded issuer pattern (provider assigns an EVM/BSC deposit address per program; funding = confirmed token deposit at that address). Implements the same interface; runs a small in-process simulator with its own signed webhooks so the full pipeline works offline. This adapter exists to prove the abstraction covers both `FIAT_RAIL` and `CRYPTO_DEPOSIT` funding models.

### 3.3 Normalized event model

`CardEvent` covers: `authorization`, `authorization_reversal`, `settlement`, `refund`, `chargeback`, `three_ds_challenge`, `card_lifecycle`. Every adapter maps provider payloads into this model. Unknown provider events are ledgered as `unmapped` — never dropped silently.

## 4. Webhook receiver

Single endpoint per provider: `POST /webhooks/{provider_id}`.

Pipeline: raw-body capture → adapter `verify_webhook` (reject 401 on failure) → dedup → parse → ledger write → dispatch.

- **Dedup/idempotency:** Redis `SETNX` on `(provider_id, event_id)` with TTL, backed by a unique constraint on the ledger table so replay after Redis eviction is still safe. Duplicate deliveries return 200 with no side effects.
- **Dispatch:** events published on the `EventBus`; consumers (funding engine, OTP service, ledger projections) subscribe. Handlers must be idempotent — document the idempotency key for each handler in code comments.
- **Failure handling:** handler exceptions never cause a non-2xx to the provider after verification succeeds; failed handling goes to a retry queue with exponential backoff and a dead-letter table.

## 5. Funding state machine + auto top-up engine

### 5.1 States

`FundingIntent` row per top-up: `PENDING → DEPOSIT_CONFIRMED → BRIDGING → BRIDGED → FUNDING → FUNDED → SETTLED`, with `FAILED_<stage>` branches and a `retry_count`. Transitions only via a single `advance()` function that enforces the legal-transition table and writes a ledger entry per transition. Illegal transitions raise and are ledgered.

### 5.2 Flow

1. **Deposit watcher** (`chain/solana_watcher.py`): polls Solana devnet for USDC transfers to the user's designated deposit ATA (websocket subscription if straightforward, else polling with slot cursor persisted in DB). Confirmed deposit (`finalized` commitment) creates a `FundingIntent` → `DEPOSIT_CONFIRMED`.
2. **Bridge step**: `BridgeProvider` interface with two implementations:
   - `debridge.py` (or Wormhole — pick whichever has a working Solana→BSC testnet route at build time; document the choice and why in ARCHITECTURE.md)
   - `simulated.py` — deterministic simulator with configurable latency/failure injection, so the end-to-end demo never depends on third-party testnet uptime.
   The engine treats them identically. `BRIDGING → BRIDGED` on destination confirmation.
3. **Card funding**: engine calls the intent's issuer adapter `fund_card` with an idempotent `funding_ref` (the intent id). `FUNDING → FUNDED`.
4. **Settlement**: `settlement` webhook events reconcile against the intent → `SETTLED`.

### 5.3 Self-healing

A reconciler task scans for stuck intents (state age > threshold), re-queries the relevant system (chain, bridge status API, issuer), and either advances or retries with backoff up to a cap, then marks `FAILED_*` and ledgers the reason. All thresholds config-driven.

## 6. 3DS / OTP flow

1. Provider sends `three_ds_challenge` webhook (Stripe/Lithic simulation, or the mock adapter's simulator).
2. OTP service extracts/derives the code, stores it in Redis with a short TTL keyed to card + challenge id.
3. Delivery to the app via `GET /otp/pending` polling **and** a WebSocket push channel (`/ws/otp`) — polling is the reliable fallback; push is the demo-quality path.
4. Mobile shows an in-app modal with the code and a copy button (explicitly not a push notification).
5. Approve/decline response posted back through the adapter where the sandbox supports it; otherwise ledgered as `responded` with the payload that would be sent.

## 7. Event ledger

Append-only `ledger_events` table: id, occurred_at, provider_id, event_type, entity refs (cardholder/card/intent), state_before/state_after where applicable, raw payload (JSONB), idempotency key (unique). Every card action, webhook, state transition, and OTP delivery writes here. Expose `GET /ledger?card_id=…` for the demo UI and for interview walk-throughs.

## 8. Chain signing

`chain/signer.py` defines a `TransactionSigner` interface with two implementations:

- `LocalKeypairSigner` — devnet keypair from env (default; always works).
- `FireblocksSigner` — Fireblocks sandbox/Embedded Wallet integration behind the same interface. Build it if sandbox access is granted in time; if not, ship the interface + a stub with a README note. The interface existing is the architectural point; the Fireblocks implementation is the differentiator if access allows.

Mobile-side signing of the deposit transaction uses the wallet available in the RN app (see §9); backend never holds user funds beyond the demo deposit keypair.

## 9. Mobile app (React Native, TypeScript)

Three screens + one modal, hitting the backend API:

1. **Card screen** — virtual card visual, masked PAN, balance (from `get_balance`), freeze/unfreeze toggle.
2. **Card detail reveal** — full PAN/CVV fetched via a short-lived, single-use reveal token from the backend; auto-hide after a countdown; screenshot-guard flag on the screen where the platform supports it.
3. **Fund screen** — shows the Solana devnet deposit address (QR + copy), triggers a devnet USDC transfer via the in-app wallet (local keypair for the demo; Fireblocks NCW SDK if §8 stretch lands), then live-renders the funding intent's state machine progress (PENDING → … → FUNDED) by polling the intent endpoint.
4. **3DS OTP modal** — appears on push/poll, shows the code, copy button, approve/decline.

Include at least one small native-module touchpoint (e.g., secure storage via Keychain/Keystore for the reveal token) so "native module experience" is honestly demonstrable.

## 10. Testing & CI

- pytest with async support; **coverage gate ≥ 60% enforced in CI for `funding/`, `webhooks/`, `issuers/`, `ledger/`** (mirrors the role's stated bar; aim higher on the state machine).
- Required test suites:
  - state machine: every legal transition, every illegal transition, retry/reconciler paths, failure injection via the simulated bridge
  - webhook receiver: signature pass/fail per adapter, duplicate delivery, out-of-order events, unmapped events
  - adapters: contract tests run against recorded fixtures (respx/vcr-style), so CI never calls live sandboxes
  - idempotent `fund_card`: same `funding_ref` twice → one funding
- GitHub Actions: lint (ruff), type-check (mypy), tests + coverage badge in README.

## 11. Documentation deliverables

- `README.md` — what this is (sandbox demo mirroring a production card-funding architecture), badge row, quickstart, screen recordings/GIFs of the full flow.
- `docs/ARCHITECTURE.md` — one diagram of the pipeline, the adapter registry design, why CCTP can't serve a Solana→BSC route and what that implies for reconciliation, funding-model taxonomy (fiat-rail vs crypto-deposit issuers), Kafka-vs-Redis-Streams note.
- `docs/DEMO.md` — exact steps to run the end-to-end flow with only free credentials.

## 12. Build phases (each ends green: tests pass, CI passes, demo-able)

1. **Skeleton + ledger + state machine** — repo, docker-compose, models, migrations, `advance()` with full transition tests. No external services yet.
2. **Issuer abstraction + mock adapter** — base interface, registry, `evm_deposit_mock` with simulator; card lifecycle endpoints; webhook receiver with verify/dedup/dispatch against the mock.
3. **Lithic adapter** — real sandbox lifecycle + webhooks + simulated authorizations; contract-test fixtures.
4. **Stripe Issuing adapter** — proves "second provider = one file"; if any core module needed changes, treat that as a design bug and fix the abstraction.
5. **Solana watcher + simulated bridge + auto top-up** — full PENDING→FUNDED loop on devnet with the simulated bridge; reconciler + failure injection.
6. **Real bridge adapter** — deBridge/Wormhole testnet route behind `BridgeProvider`; keep the simulator as default for the recorded demo.
7. **3DS/OTP service** — webhook → Redis → poll + WebSocket delivery; approve/decline path.
8. **Mobile app** — four surfaces in §9 against the running backend.
9. **Fireblocks signer (stretch)** — behind `TransactionSigner`.
10. **Docs + polish** — diagrams, GIFs, coverage badge, final README pass.
