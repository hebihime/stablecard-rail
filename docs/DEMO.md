# Demo

How to run the pipeline locally with free credentials only. Sections are added as
phases land (SPEC.md §12); **phase 1** is what exists today.

Prerequisites: Docker (with Compose) and Python 3.12. No provider accounts, no
testnet funds, and no secrets are needed for phase 1.

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
curl -s localhost:8000/healthz            # {"status":"ok","database":"ok"}
curl -s localhost:8000/ledger | jq        # {"count":0,"events":[]}
open http://localhost:8000/docs           # OpenAPI, showing GET /ledger
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

### Running the backend on the host instead

Useful for the test suite and for iterating without rebuilds.

```bash
docker compose up -d postgres redis     # dependencies only

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

alembic upgrade head
uvicorn app.main:app --reload --port 8000
python scripts/demo_phase1.py
```

### Tests

```bash
cd backend
pytest                                   # 324 tests
pytest --cov --cov-report=term-missing   # coverage gate, SPEC.md §10
ruff check . && ruff format --check . && mypy
```

The suite needs the Postgres container running (`docker compose up -d postgres`)
and nothing else — it creates and migrates its own `stablecard_test` database, and
never contacts an external service. To point it elsewhere, set `TEST_DATABASE_URL`.

### Reset

```bash
docker compose down -v      # also drops the postgres volume
```
