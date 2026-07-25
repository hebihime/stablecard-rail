# Demo

How to run the pipeline locally with free credentials only. Sections are added as
phases land (SPEC.md §12); **phase 1** is what exists today.

Prerequisites: Docker (with Compose) and Python 3.12. No provider accounts, no
testnet funds, and no secrets are needed for phases 1-2.

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
crypto-deposit issuer (with its assigned EVM deposit address), funding that is
idempotent under one `funding_ref`, a signed delivery accepted, the same delivery
ignored as a duplicate, a tampered delivery rejected, an unmodelled event type
recorded as `unmapped`, and a failing handler retried and then dead-lettered. It
ends with the ledger and a ready-to-paste `curl` for the HTTP endpoint.

### Card lifecycle over HTTP

```bash
export P=localhost:8000/providers/evm_deposit_mock

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
    adapter = registry.get_adapter("evm_deposit_mock")
    holder = await adapter.create_cardholder(CreateCardholderRequest(
        email="ada@example.test", first_name="Ada", last_name="Lovelace"))
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest())
    d = adapter.simulator.emit_authorization(card.card_id, Money(1299, "USD"))
    parts = ["curl", "-s", "-X", "POST", "http://localhost:8000/webhooks/evm_deposit_mock"]
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
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/webhooks/evm_deposit_mock -d '{}'
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
```

## Tests

```bash
cd backend
pytest                                   # 558 tests
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
