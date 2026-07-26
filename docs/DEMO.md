# Demo

How to run the pipeline locally with free credentials only. Sections are added as
phases land (SPEC.md §12); **phases 1-3** are what exist today.

Prerequisites: Docker (with Compose) and Python 3.12. Phases 1-2 need no provider
account, no testnet funds and no secrets at all. Phase 3 needs a free Lithic sandbox
key; nothing else in the service depends on it.

---

## Phase 1 — ledger + funding state machine

### Everything in Docker

```bash
cp .env.example .env          # defaults work as-is; nothing secret in phase 1
docker compose up --build     # postgres :5442, redis :6389, backend :8000
```

The backend container runs `alembic upgrade head` before serving, so the schema is
current on first boot.

```bash
curl -s localhost:8000/healthz     # {"status":"ok","database":"ok","redis":"ok"}
curl -s localhost:8000/ledger | jq # {"count":0,"events":[]}
open http://localhost:8000/docs    # OpenAPI
```

### Walk an intent through the machine

```bash
docker compose exec backend python scripts/demo_phase1.py
```

This creates a funding intent, walks the full happy path with one retry, attempts
an illegal transition from a terminal state, and prints the resulting ledger:

```
created intent 6f1e… in PENDING for 2500 USD (minor units)
  -> DEPOSIT_CONFIRMED
  -> BRIDGING
  retried BRIDGING (retry_count now 1)
  -> BRIDGED
  -> FUNDING
  -> FUNDED
  -> SETTLED
rejected as expected: illegal funding transition SETTLED -> FUNDING for intent 6f1e…;
legal targets: none (terminal state)

ledger:
  #1   funding_intent.created                            - -> PENDING
  #2   funding_intent.transitioned                 PENDING -> DEPOSIT_CONFIRMED
  #3   funding_intent.transitioned       DEPOSIT_CONFIRMED -> BRIDGING
  #4   funding_intent.retried                     BRIDGING -> BRIDGING
  #5   funding_intent.transitioned                BRIDGING -> BRIDGED
  #6   funding_intent.transitioned                 BRIDGED -> FUNDING
  #7   funding_intent.transitioned                 FUNDING -> FUNDED
  #8   funding_intent.transitioned                  FUNDED -> SETTLED
  #9   funding_intent.illegal_transition           SETTLED -> FUNDING
```

Note event `#9`: the rejected transition raised **and** was recorded. Then read the
same history back over HTTP — this is the endpoint the demo UI and interview
walk-throughs use:

```bash
curl -s "localhost:8000/ledger?card_id=card_demo_1" | jq '.events[] | {id, event_type, state_before, state_after}'
```

### Prove the ledger is append-only

The guarantee is enforced by the database, not by the application:

```bash
docker compose exec postgres psql -U stablecard -d stablecard \
  -c "UPDATE ledger_events SET event_type = 'tampered' WHERE id = 1;"
# ERROR:  ledger_events is append-only: UPDATE is not permitted on this table
```

---

## Phase 2 — issuer abstraction + mock adapter + webhook receiver

Nothing new to configure: the mock provider runs in-process and signs its own
webhooks, so the whole pipeline works offline.

### The whole thing in one command

```bash
docker compose exec backend python scripts/demo_phase2.py
```

It walks: the registered issuers and their funding models, a card at a
crypto-deposit issuer (with the Safe address its funds must be sent to), a
`fund_card` that answers `pending` because no deposit has confirmed yet, the same
call succeeding once one has — and being idempotent under one `funding_ref` — a
signed delivery accepted, the same delivery ignored as a duplicate, a tampered
delivery rejected, an unmodelled event type recorded as `unmapped`, and a failing
handler retried and then dead-lettered. It ends with the ledger and a
ready-to-paste `curl` for the HTTP endpoint.

The funding sequence is the part worth watching. At this provider money arrives
on-chain, so the demo makes the deposit happen on the simulated chain and
`fund_card` only attributes it — the balance is created by the transfer, never by
the call. Ask before the deposit confirms and the honest answer is `pending` with
no issuer reference at all.

### Card lifecycle over HTTP

```bash
export P=localhost:8000/providers/gnosis_pay_mock

curl -s localhost:8000/providers | jq            # who is registered, and how they fund

CH=$(curl -s -X POST $P/cardholders -H 'content-type: application/json' \
  -d '{"email":"ada@example.test","first_name":"Ada","last_name":"Lovelace"}' \
  | jq -r .cardholder_id)

CARD=$(curl -s -X POST $P/cardholders/$CH/cards -H 'content-type: application/json' \
  -d '{"currency":"USD","spend_limit_minor":50000}')
echo "$CARD" | jq '{card_id, state, last_four, deposit_address}'
ID=$(echo "$CARD" | jq -r .card_id)

curl -s -X POST $P/cards/$ID/activate | jq -r .state   # active
curl -s -X POST $P/cards/$ID/freeze   | jq -r .state   # frozen
curl -s -X POST $P/cards/$ID/activate | jq -r .state   # active again — unfreeze
curl -s -X POST $P/cards/$ID/cancel   | jq -r .state   # canceled
curl -s -X POST $P/cards/$ID/activate | jq             # 409: canceled is terminal
curl -s $P/cards/$ID/balance | jq                      # integer minor units
```

Every action above is in the ledger, with `state_before` and `state_after`:

```bash
curl -s "localhost:8000/ledger?card_id=$ID" \
  | jq '.events[] | {event_type, state_before, state_after}'
```

Note `deposit_address`: this provider is `CRYPTO_DEPOSIT`, so funding it means a
token transfer to that address rather than a debit from a fiat balance. There is
deliberately **no** `POST /cards/{id}/fund` route — money reaches a card through the
funding state machine (phase 5), never through an HTTP call to an issuer.

### Webhooks

The endpoint needs a real signature, so the easiest source is the simulator. The
demo script prints a ready-to-paste `curl`; or generate one directly:

```bash
docker compose exec backend python - <<'PY'
import asyncio, shlex
from app.core.money import Money
from app.issuers import registry
from app.issuers.base import CreateCardholderRequest, CreateCardRequest

async def main():
    adapter = registry.get_adapter("gnosis_pay_mock")
    holder = await adapter.create_cardholder(CreateCardholderRequest(
        email="ada@example.test", first_name="Ada", last_name="Lovelace"))
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest())
    await adapter.activate_card(card.card_id)
    # An approved authorization needs a funded Safe, and money arrives on-chain here.
    adapter.simulator.receive_onchain_deposit(card.deposit_address, Money(5000, "USD"))
    adapter.simulator.authorize(card.card_id, Money(1299, "USD"))
    d = adapter.simulator.deliveries[-1]
    parts = ["curl", "-s", "-X", "POST", "http://localhost:8000/webhooks/gnosis_pay_mock"]
    for k, v in d.headers.items():
        parts += ["-H", f"{k}: {v}"]
    parts += ["--data-binary", d.body.decode()]
    print(" ".join(shlex.quote(p) for p in parts))

asyncio.run(main())
PY
```

Paste the result twice and watch the pipeline behave:

```
{"received":true,"duplicate":false, ... "event_type":"authorization","ledger_event_id":14, ...}
{"received":true,"duplicate":true,  ... "ledger_event_id":14, ...}
```

The second call writes nothing, publishes nothing, and re-runs no handlers — it
just points back at the row it matched. Then try to get something past the door:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/webhooks/gnosis_pay_mock -d '{}'
# 401 — unsigned
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/webhooks/wells_fargo -d '{}'
# 404 — no such provider
```

Neither writes to the ledger: it only records authenticated events.

> The mock provider's state lives in the process that created it. A card made via
> the API exists in the server's simulator; the demo script has its own. Deliveries
> verify either way — the receiver authenticates and records, it does not second-guess
> the provider about which cards exist.

### Inspect the moving parts

```bash
# The EventBus stream — Redis Streams standing in for Kafka
docker compose exec redis redis-cli xlen stablecard:card_events
docker compose exec redis redis-cli xrange stablecard:card_events - + COUNT 1

# Dedup claims, one per (provider, event id), with a TTL
docker compose exec redis redis-cli --scan --pattern 'webhook:dedup:*'

# Deliveries we gave up on, after every retry failed
docker compose exec postgres psql -U stablecard -d stablecard \
  -c "select handler, attempts, last_error from webhook_dead_letters;"
```

### Drain handler retries

A failed handler is queued with exponential backoff; the drain is its own process:

```bash
docker compose exec backend python scripts/drain_webhook_retries.py --once
docker compose exec backend python scripts/drain_webhook_retries.py --interval 5
```

Phase 2 registers no business handlers — the funding engine subscribes in phase 5
and the OTP service in phase 7 — so on a clean database there is nothing to drain.
`scripts/demo_phase2.py` registers a deliberately broken one to show the path.

---

## Phase 3 — the Lithic adapter

A real `FIAT_RAIL` issuer, against Lithic's sandbox. Everything below makes real API
calls; **the test suite never does** (SPEC.md §10).

### Credentials

Sign up at [lithic.com/signup](https://lithic.com/signup) — self-serve, free, no sales
call. The sandbox key is issued with the account, at `app.lithic.com/settings`. Then:

```bash
# in .env (gitignored)
LITHIC_API_BASE_URL=https://sandbox.lithic.com/v1
LITHIC_API_KEY=<your sandbox key>
```

`LITHIC_WEBHOOK_SECRET` is optional and not needed for any of this: it only matters
for accepting genuine inbound deliveries, which needs an event subscription pointing
at a public URL. With it unset, `verify_webhook` fails closed and says why.

Nothing else in the service depends on these. With no key at all, the mock provider
and phases 1–2 work exactly as before — the adapter is registered by factory, so only
a call that needs a key fails, and the error names the variable.

### The whole thing in one command

```bash
docker compose up -d postgres redis
cd backend && source .venv/bin/activate
python scripts/demo_phase3.py
```

It creates a cardholder and a virtual card, freezes and unfreezes it, funds it twice
under one `funding_ref`, reads the balance, simulates an authorization and its
clearing, then takes the event payloads Lithic actually recorded for those calls and
runs them through our own webhook pipeline into the ledger. It closes the card on the
way out.

Two things to watch for:

- **the balance moves with the hold, not just the settlement.** A pending
  authorization is money the cardholder cannot spend twice;
- **the second `fund_card` call changes nothing.** It reports
  `replayed=True` and the same `issuer_funding_ref`, because the funding ref is
  recorded on the card itself (docs/ARCHITECTURE.md §4.5).

### Over HTTP

The same endpoints as phase 2, with `lithic` in place of `gnosis_pay_mock`:

```bash
curl -s localhost:8000/providers | jq
# [{"provider_id":"gnosis_pay_mock","funding_model":"crypto_deposit"},
#  {"provider_id":"lithic","funding_model":"fiat_rail"}]

curl -s -X POST localhost:8000/providers/lithic/cardholders \
  -H 'content-type: application/json' \
  -d '{"email":"ada@example.test","first_name":"Ada","last_name":"Lovelace"}' | jq

curl -s -X POST localhost:8000/providers/lithic/cardholders/<account_token>/cards \
  -H 'content-type: application/json' \
  -d '{"currency":"USD","spend_limit_minor":50000}' | jq
```

The card lifecycle, balance and webhook routes are unchanged and were not touched to
add this provider. That is the claim phase 3 exists to test, and
`backend/tests/test_module_boundaries.py` enforces it.

### Re-recording the contract fixtures

```bash
python scripts/record_lithic_fixtures.py            # writes tests/fixtures/lithic/
python scripts/record_lithic_fixtures.py --dry-run  # walk the API, write nothing
```

This is the only thing in the repo that calls Lithic outside the demo. It walks the
sandbox with plain `httpx` and literal paths — deliberately not through the adapter,
so the fixtures are evidence about the provider rather than a mirror of our own
assumptions — and redacts PAN and CVV before writing. Re-run it when Lithic changes a
payload: the diff is the change, and the contract tests either still pass or tell you
what moved.

---

## Phase 5 — Solana watcher, simulated bridge, auto top-up

The whole funding pipeline: a USDC deposit on Solana devnet drives a funding intent
from `PENDING` to `FUNDED`, with every transition written to the ledger.

```bash
cd backend
python scripts/demo_phase5.py                 # replay recorded devnet data — no network
python scripts/demo_phase5.py --live          # poll devnet for real, read-only
python scripts/demo_phase5.py --fee 15        # a bridge that charges a fee
python scripts/demo_phase5.py --inject stuck  # a bridge that goes quiet, and the reconciler
```

**The default mode needs no credentials and no network.** It replays the responses in
`backend/tests/fixtures/solana/` — a real 1.000000 USDC `transferChecked`, recorded
from devnet — so the walk-through cannot fail because a public RPC endpoint is
rate-limiting. What you should see:

```
  DEPOSIT_CONFIRMED -> BRIDGING          submitted to simulated
           BRIDGING -> BRIDGED           bridge delivered 100 USD (minor units)
                       (bridge delivery reflected into the Safe)
            BRIDGED -> FUNDED            card funded with 100 USD (minor units)
```

followed by the ledger for that intent, one row per hop, and the card's balance read
back from the provider.

`--live` points the same watcher at `api.devnet.solana.com` and polls **read-only**.
Real transfers to the watched account become real intents. Nothing sends a
transaction: submitting one belongs to the wallet that owns the money (SPEC.md §9.3,
phase 8).

### Watching your own address

Optional, and the only part that needs anything from you:

```bash
# in .env (gitignored)
SOLANA_DEPOSIT_KEYPAIR=<base58 secret, or the JSON array solana-keygen writes>
```

The demo then derives that wallet's USDC token account — the address USDC must
actually be sent to, since a wallet address does not hold tokens itself — and prints
it. Devnet USDC is free from [faucet.circle.com](https://faucet.circle.com), which
needs a browser. Send some to the printed address, then run `--live` again.

### Failure injection, and the reconciler

`--inject` makes the bridge misbehave in one of the four ways a real one does:

| Mode | What the pipeline sees | Where the intent ends |
| --- | --- | --- |
| `submit_unavailable` | a retryable error; nothing accepted | retried, then `FAILED_BRIDGE` |
| `submit_rejected` | a permanent refusal | `FAILED_BRIDGE`, at once |
| `transfer_failed` | accepted, then failed on a later poll | `FAILED_BRIDGE` |
| `stuck` | accepted, then silence forever | the reconciler retries, then `FAILED_BRIDGE` |

With a failure injected the engine takes one step and the **reconciler** takes over,
which is how the two divide the work in production. Its thresholds are cut to
seconds for the demo (the defaults are minutes) and the backoff doubles per retry,
so the output shows the waiting windows as well as the attempts:

```
           BRIDGING -> BRIDGING          retrying (1): the bridge has not delivered yet
           BRIDGING    waiting until 00:56:06
           BRIDGING -> BRIDGING          retrying (2): the bridge has not delivered yet
           BRIDGING -> FAILED_BRIDGE     gave up after 2 attempts: ...
```

### Re-recording the devnet fixtures

```bash
python scripts/record_solana_fixtures.py            # writes tests/fixtures/solana/
python scripts/record_solana_fixtures.py --dry-run  # call, print, write nothing
python scripts/record_solana_fixtures.py --discover # find a current transfer to pin
```

Read-only and free — no credentials at all. `backend/tests/fixtures/solana/README.md`
says which files are recorded and which are derived, and which of them exist because
the obvious guess turned out to be wrong.

---

## Running the backend on the host instead

Useful for the test suite and for iterating without rebuilds.

```bash
docker compose up -d postgres redis     # dependencies only

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

alembic upgrade head
uvicorn app.main:app --reload --port 8000
python scripts/demo_phase1.py
python scripts/demo_phase2.py
python scripts/demo_phase3.py     # needs LITHIC_API_KEY
python scripts/demo_phase4.py     # needs STRIPE_ISSUING_API_KEY
python scripts/demo_phase5.py     # needs nothing
```

## Tests

```bash
cd backend
pytest                                   # 1252 tests
pytest --cov --cov-report=term-missing   # coverage gate, SPEC.md §10
ruff check . && ruff format --check . && mypy
```

The suite needs the Postgres and Redis containers running
(`docker compose up -d postgres redis`) and nothing else. It creates and migrates
its own `stablecard_test` database, uses Redis database 15 (which it flushes), and
never contacts an external service. To point either elsewhere, set
`TEST_DATABASE_URL` / `TEST_REDIS_URL`.

## Reset

```bash
docker compose down -v      # also drops the postgres and redis volumes
```
