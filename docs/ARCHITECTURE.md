# Architecture

Sandbox demonstration of a card-funding pipeline. Testnets and provider sandboxes
only — no mainnet funds, no production card programs.

This document is the running record of design decisions. Sections marked
**deferred** land with the phase that builds them (see SPEC.md §12); everything
else is decided and implemented.

Sections are grouped by when they were decided: §2 phase 1, §3 phase 2, §4 phase 3,
§7 the refactor that followed SPEC.md's revision to name Gnosis Pay as the target
provider, §8 phase 4. §5 (deferred) and §6 (the module rule) are standing sections.
Earlier sections are not rewritten when a later one changes something; they point
forward.

§8 is the one to read first if you want to know whether the adapter abstraction
works: phase 4 exists to test it with a second real provider, and §8 records what
that cost.

---

## 1. Target pipeline

```mermaid
flowchart LR
    subgraph mobile["Mobile (RN) — phase 8"]
        FUND[Fund screen]
        CARD[Card screen]
        OTP[3DS OTP modal]
    end

    subgraph chain["chain/ — phases 5, 6, 9"]
        WATCH[Solana watcher<br/>finalized USDC deposits]
        SIGN[TransactionSigner]
        BRIDGE[BridgeProvider<br/>simulated · deBridge/Wormhole]
    end

    subgraph core["Backend core"]
        MACHINE[funding/machine.advance<br/>state machine]
        RECON[reconciler — phase 5]
        BUS[EventBus<br/>Redis Streams — phase 2]
        LEDGER[(ledger_events<br/>append-only)]
        INTENTS[(funding_intents)]
    end

    subgraph issuers["issuers/ — phases 2, 3, 4"]
        REG[registry]
        LITHIC[lithic]
        STRIPE[stripe_issuing]
        MOCK[gnosis_pay_mock]
    end

    WH[/"POST /webhooks/{provider_id}"<br/>verify → dedup → parse → ledger → dispatch/]

    FUND --> WATCH --> MACHINE
    SIGN -.-> WATCH
    MACHINE --> BRIDGE --> MACHINE
    MACHINE --> REG --> LITHIC & STRIPE & MOCK
    LITHIC & STRIPE & MOCK --> WH --> BUS --> MACHINE
    BUS --> OTP
    MACHINE --> INTENTS
    MACHINE --> LEDGER
    WH --> LEDGER
    RECON --> MACHINE
    CARD --> REG
```

Built so far: the core (`funding_intents`, `ledger_events`, `advance()`), the
issuer abstraction with the `gnosis_pay_mock` adapter, the card lifecycle
endpoints, and the webhook receiver with its `EventBus`, retry queue and
dead-letter table. Everything else arrives in a later phase behind an interface
that already exists or is specified.

---

## 2. Decisions recorded in phase 1

### 2.1 The transition table

SPEC.md §5.1 names the states and says "`FAILED_<stage>` branches"; it does not
enumerate them. Chosen set, one per stage that can fail:

| From | To |
|---|---|
| `PENDING` | `PENDING` (retry) · `DEPOSIT_CONFIRMED` · `FAILED_DEPOSIT` |
| `DEPOSIT_CONFIRMED` | `BRIDGING` · `FAILED_BRIDGE` |
| `BRIDGING` | `BRIDGING` (retry) · `BRIDGED` · `FAILED_BRIDGE` |
| `BRIDGED` | `FUNDING` · `FAILED_FUNDING` |
| `FUNDING` | `FUNDING` (retry) · `FUNDED` · `FAILED_FUNDING` |
| `FUNDED` | `SETTLED` · `FAILED_SETTLEMENT` |
| `SETTLED`, `FAILED_*` | — terminal |

15 legal edges out of 121 ordered pairs. `tests/test_transition_table.py` asserts
this as a golden matrix, so widening the machine is never accidental, and
`tests/test_state_machine.py` exercises all 121 pairs against the database.

Two conventions follow from §5.3 ("retries with backoff up to a cap, **then**
marks `FAILED_*`"):

- **Retries are self-transitions.** A retry does not leave the state being
  retried: `BRIDGING → BRIDGING` bumps `retry_count`, stores the reason on the
  intent, and ledgers a `funding_intent.retried` event. Only the three states that
  wait on an external system (`PENDING`, `BRIDGING`, `FUNDING`) are retryable —
  the others are instantaneous hand-offs with nothing to re-poll.
- **`FAILED_*` is terminal.** A failed intent is a closed record; recovery means
  opening a new intent. Reopening a failure would let the ledger overwrite its own
  account of what happened, which defeats the point of SPEC.md §7.

`DEPOSIT_CONFIRMED → FAILED_BRIDGE` and `BRIDGED → FAILED_FUNDING` exist so a
submission that fails before the work starts still lands in the right bucket.

### 2.2 `advance()` owns its transaction

`advance()` and `create_intent()` commit before returning. The alternative —
caller-owned commits — cannot satisfy "illegal transitions raise **and are
ledgered**": an exception propagating out of an uncommitted transaction takes the
audit record with it. So the illegal-transition path writes its
`funding_intent.illegal_transition` entry, commits it, and only then raises. No
state was modified, so committing is safe.

Consequence, stated in the module docstring: a transition is the unit of work.
Callers should not batch unrelated writes into the same session.

The successful path writes the state change and its ledger entry in **one**
transaction, so you can never have one without the other.

### 2.3 Replay wins over legality

The idempotency-key lookup runs *before* the transition-table check. Under
at-least-once webhook delivery (SPEC.md §4) the second copy of an already-applied
event would otherwise look like an illegal repeat of a completed hop and turn into
a spurious 500 back to the provider. A known key means "already done" — a success
returning current state.

### 2.4 Dedup has two layers, and the durable one is the database

`idempotency_key` on `ledger_events` is `NULL`-able and unique. Postgres permits
many `NULL`s, so dedup is opt-in per call: chain-driven transitions pass no key,
webhook-driven ones pass the provider's event id.

`advance()` pre-checks the key, but the unique index is what actually enforces
uniqueness — the pre-check is only a fast path. If two workers race past it, the
insert fails and the loser collapses into a no-op. That path is asserted directly
(`test_unique_constraint_is_the_backstop_when_the_precheck_misses`) by blinding
the pre-check, which is what Redis eviction looks like in production. The handler
matches on the constraint name, so an unrelated `IntegrityError` is never
swallowed as a replay.

Redis (§4) becomes the first-line dedup cache in phase 2; this layer stands
without it.

### 2.5 Concurrency: row lock, and no stale reads

`advance()` loads the intent `FOR UPDATE`, so two workers cannot both apply the
same hop; the loser re-reads the new state and is rejected by the table. The load
also sets `populate_existing=True` — a row lock is worthless if the code then acts
on attribute values cached in the session's identity map from before the lock.

### 2.6 The ledger is append-only in the database, not by convention

A `BEFORE UPDATE OR DELETE` trigger raises `restrict_violation`. Application
discipline is not evidence; a trigger is. `TRUNCATE` is deliberately still
permitted (row triggers do not fire on it), which is what lets the test suite
reset cheaply between tests.

### 2.7 Identifier and ordering choices

- **`ledger_events.id` is a `BIGINT` identity column.** `occurred_at` can tie, and
  provider clocks are not ours to trust; the ledger needs a total order for
  projections and for reading a card's history as a narrative.
- **`funding_intents.id` is a UUID.** It doubles as the idempotent `funding_ref`
  passed to issuer adapters (§5.2 step 3), so it must be opaque, client-safe, and
  generated before any external call.
- **No foreign key from `ledger_events` to `funding_intents`.** An audit record
  must be writable for entities this service has never seen — an `unmapped`
  provider event (§3.3) — and must outlive whatever it refers to. Entity
  references are stored as opaque values, consistent with treating all external
  identifiers as opaque strings.

### 2.8 `Money` lives in `core/`, not `issuers/`

SPEC.md §3.1 first mentions `Money` in the issuer interface, but `ledger/` and
`funding/` need it too, and no module outside `issuers/` may import from
`issuers/`. So it is `app/core/money.py`: a frozen dataclass of
`(amount_minor: int, currency: str)` that rejects non-`int` amounts at
construction — including `bool`, which is an `int` subclass and would silently
mean "one cent". Negative amounts are legal (reversals, refunds). There is no
float constructor anywhere in the codebase.

### 2.9 Funding state is `VARCHAR` + `CHECK`, not a native Postgres enum

`native_enum=False`. Adding a state stays an ordinary migration instead of an
`ALTER TYPE ... ADD VALUE`, which cannot run inside a transaction block. The
machine is expected to grow; the schema should not fight that.

`ledger_events.state_before/state_after` are plain `VARCHAR(32)` rather than the
funding enum, because the same columns record card-lifecycle states from issuer
adapters.

### 2.10 Ledger event types are string constants, not an enum

`app/ledger/event_types.py` holds module-level constants. A closed enum would
force every new issuer adapter to edit a core module to add its event types —
exactly what the §3.2 abstraction rule forbids. Convention:
`<entity>.<past-tense-event>`.

### 2.11 Tests run against real Postgres

The suite creates its own `*_test` database if missing and migrates it with
Alembic, so every run also exercises the migration path (and
`tests/test_migrations.py` asserts models and migrations have not drifted, via
`compare_metadata`).

SQLite would be faster but models none of what this schema relies on: `JSONB`,
identity columns, `NULL`-permitting unique constraints, and the append-only
trigger. For a financial ledger, testing against a database that cannot express
its own invariants is not a saving.

Isolation is `TRUNCATE ... RESTART IDENTITY` before each test rather than a
rolled-back transaction, because `advance()` commits. The engine uses `NullPool`:
pytest-asyncio gives each test its own event loop, and asyncpg connections are
bound to the loop that opened them.

### 2.12 Local host ports are non-default

Postgres publishes on **5442** and Redis on **6389**, because 5432/6379 are
commonly already occupied. Container-internal ports are unchanged; CI uses the
defaults.

### 2.13 Dependency added beyond SPEC.md §2

`pydantic-settings` — the Pydantic v2 way to do env-var settings, split out of
Pydantic 1. Everything else is on the approved list. `httpx` is pulled in as a dev
dependency only, as FastAPI's own test transport.

### 2.14 Coverage scope grows per phase

SPEC.md §10 gates `funding/`, `webhooks/`, `issuers/`, `ledger/` at ≥60%.
`[tool.coverage.run] source` lists only the packages that exist in the current
phase, and each phase adds its own — otherwise the gate would measure absent code.
All four now exist and measure **100%** against the 60% floor.

### 2.15 Repository root

SPEC.md §1 names the root `stablecard-rail/`; this checkout is `crypto-card/`.
Everything inside matches the specified layout. Directories for unbuilt phases
(`app/chain/`, `mobile/`) are absent rather than empty, to keep "no scaffolding
ahead of the phase" visible in the tree.

---

## 3. Decisions recorded in phase 2

### 3.1 The registry holds factories, not instances

`issuers/registry.py` maps `provider_id` → a zero-argument factory, and memoizes
the first result. Two reasons it is not a dict of instances:

- Adapters read settings and (from phase 3) open HTTP clients. Doing that at import
  time makes configuration errors surface as import errors, in the wrong place.
- The memoized instance means a provider holding state — the mock's in-process
  simulator — is a singleton per process. Otherwise a card created through one call
  site would be invisible to the next.

Re-registering a `provider_id` raises unless `replace=True` is passed. A silent
collision would reroute money, because `provider_id` is what a funding intent
stores to decide who to pay.

**Adding an issuer is one adapter file plus one `register()` line** in
`app/issuers/__init__.py`. `tests/test_module_boundaries.py` reads the import graph
and fails if any module outside `issuers/` imports anything but `base` and
`registry`, or if any adapter imports `funding/`, `ledger/`, `webhooks/` or `api/`.
That is the difference between a design rule and a design aspiration — and it is
what phase 4 will demonstrate rather than discover.

### 3.2 Funding-model taxonomy

SPEC.md §3.1 puts `funding_model` on the interface; §3.2 asks the mock to prove the
abstraction spans both. The distinction is *what `fund_card` means*:

| | `FIAT_RAIL` (Lithic, Stripe) | `CRYPTO_DEPOSIT` (`gnosis_pay_mock`) |
|---|---|---|
| Where money comes from | a program balance funded by bank transfer | a token transfer to a provider-assigned address |
| `fund_card` | debits the program balance | attributes an observed deposit to the card |
| `Card.deposit_address` | `None` | the address the bridge must send to |

Only two things in the shared models exist for the crypto side —
`Card.deposit_address` and the taxonomy enum itself — and both are optional or
inspectable, so a fiat adapter never invents a value it does not have. Everything
else provider-specific lives in `raw`.

Concretely, the mock's `fund_card` **cannot move money**: the deposit is what
creates a balance, and `fund_card` answers `PENDING` until a confirmed,
unattributed one covers the amount. Phase 5's engine calls it only from `BRIDGED`,
i.e. once funds have actually reached the deposit address — and `PENDING` is the
answer when the provider has not seen them yet. §7.1 records why this replaced an
earlier version that credited a balance.

### 3.3 `webhook_event_id` and `get_card` are additions to §3.1

Two methods not in the spec's list:

- **`webhook_event_id(headers, body)`** — non-abstract, defaulting to `None`. SPEC.md
  §4 puts dedup *before* parse, which means the dedup key must be readable without
  trusting the body to be well-formed. That ordering is what makes an
  authentic-but-unreadable delivery safe: it is recorded once as `unmapped` instead
  of failing to parse on every redelivery, forever. Adapters whose provider has no
  envelope id inherit the default and the receiver falls back to `sha256(body)`.
  Whatever an adapter returns must be **covered by the signature** — otherwise the
  dedup key is forgeable, and a legitimate body can be replayed under a fresh id.
- **`get_card(card_id)`** — the freeze/unfreeze toggle and card screen (§9.1) need to
  read state without mutating it, and the ledger's `state_before` needs it too.

Both are asserted in `tests/test_issuer_interface.py`, which also fails if the
interface grows a third addition without this section being updated.

### 3.4 No local card table

Card routes are `/providers/{provider_id}/cards/{card_id}`: the provider is named
in the path rather than looked up from a local table, because there is no local
table. The provider owns card state, and a second copy here would be a cache that
can silently disagree with the thing it caches — exactly the class of bug that ends
with a frozen card that our API reports as active.

The cost is that a client must remember which provider issued a card. That is
acceptable now (a funding intent already stores `provider_id`) and will be revisited
in phase 5, which needs a `deposit_address → card` index for the chain watcher. That
index will be a projection of the ledger's `card.created` events, which already
record the deposit address for exactly this reason — not a second source of truth
about card state.

### 3.5 The ledger only records authenticated events

`POST /webhooks/{provider_id}` is open to the internet. A failed signature gets a
401, a log line, and nothing else: ledgering rejected traffic would let anyone write
to the audit log and exhaust the keyspace its unique index depends on. Signature
failures are a metrics-and-logs concern, not an evidence concern.

### 3.6 Dedup claims are released on failure

SPEC.md §4 specifies `SETNX` *before* the work, which is the right way round for
concurrency — two simultaneous deliveries cannot both pass. It opens one window:
claim, then die before the ledger write, and the provider's redelivery looks like a
duplicate for the life of the TTL (a day). So every failure path before commit calls
`DedupGate.release()`. `tests/test_webhook_receiver.py` asserts both halves — that
the claim is given back, and that the redelivery is then processed.

The durable layer is unchanged from phase 1: `ledger_events.idempotency_key` is
unique, and the receiver treats a violation on that specific constraint as "already
recorded". Any other `IntegrityError` propagates, so a real schema violation is
never mistaken for a duplicate.

### 3.7 Handler failure is our problem, not the provider's

Once verification succeeds the provider gets a 200 — including when a handler
throws. Answering 5xx would make the provider retry the whole delivery (re-running
handlers that already succeeded) and, with exponential backoff, eventually give up
on us. So a failed handler is queued individually:

- A Redis sorted set scored by "next due", claimed on read so two workers cannot
  run the same handler twice.
- Items are self-contained — the whole normalized event travels with the retry — so a
  retry works in another process, after a restart, or by hand from a dead-letter row.
- Backoff is config-driven (`WEBHOOK_RETRY_BACKOFF_SECONDS`, default
  `[2,8,32,128,512]`). Its **length is the retry cap**: one inline attempt plus one
  retry per step, then the delivery is dead-lettered.
- A handler whose subscription no longer exists — a deploy removed it — is
  dead-lettered immediately rather than cycling against a name nothing answers to.

Dead letters go to a table, not a Redis key, because giving up on a provider event
is an operational fact someone has to find later. One row per
`(provider_id, event_id, handler)` via `ON CONFLICT DO NOTHING`, so a second worker
reaching the same conclusion does not double-report. First arrivals are ledgered as
`webhook.dead_lettered`; suppressed duplicates are not.

Draining is `scripts/drain_webhook_retries.py`, a separate process. A retry worker
inside the web process is one that dies with it and that nobody can run by hand.

### 3.8 Kafka vs Redis Streams

The `EventBus` interface is the architectural point; `RedisStreamsEventBus` is the
implementation. Redis Streams is a real fit rather than a fudge — ordered entries,
monotonic ids, and a consumer can resume from the last id it processed, which is
what a replayable log needs. What it lacks against Kafka is partitioning, retention
policy, and multi-broker durability. A Kafka implementation is a drop-in: consumers
receive `CardEvent`s and never learn what carried them.

Two operational details: publishing happens before handlers run and unconditionally,
so a consumer added in phase 5 can replay the stream from the beginning; and the
stream is length-capped, because Redis has no per-entry TTL and an uncapped stream
is an unbounded memory leak.

### 3.9 Ledger event naming for provider events

Provider events are recorded as `provider.<normalized_type>` —
`provider.authorization`, `provider.settlement`, `provider.unmapped` — via
`event_types.provider_event()`. One namespace for "things a provider told us", as
against `card.*` for things we did and `funding_intent.*` for state changes. It is a
function rather than a constant per case so `ledger/` never has to import the
`CardEventType` vocabulary.

### 3.10 No consumers are subscribed yet

`dispatch.subscribe()` exists and is fully tested; phase 2 registers nothing. The
funding engine subscribes in phase 5 and the OTP service in phase 7. Each handler
must document its idempotency key at the subscription site, per SPEC.md §4 — a
handler can be re-run by a retry, by a drain in another process, or by hand from a
dead-letter row. `tests/test_webhook_dispatch.py` asserts that booting the app
registers no handlers, so a consumer cannot appear ahead of its phase by accident.

### 3.11 The mock provider is a package, and its key is not a credential

`issuers/gnosis_pay_mock/` is a directory rather than one file because it ships the
*provider* as well as the adapter: `adapter.py` is the one file a real issuer needs,
while `simulator.py`, `signing.py` and `config.py` stand in for servers — and for a
chain — that, for Lithic and Stripe, belong to someone else.

The simulator signs its own webhooks, and because Gnosis Pay's scheme is asymmetric
there is no shared secret to hold: the simulator derives an Ed25519 private key from
a seed in `signing.py` and the adapter verifies with the public half. A seed in the
repo is not a violation of "no secrets in a tracked file" — the party it
authenticates is a Python object in this process, so it grants access to nothing.
Real adapter credentials have no defaults and no example values. §7.3 records why
the key is derived rather than generated.

Everything the simulator produces is deterministic — per-kind counters for ids,
hash-derived deposit addresses — so a demo run twice produces the same output and a
test never has to know a random value. `last_four` is a repeating synthetic pattern
that could not be mistaken for card-number material, and there is no PAN or CVV
anywhere: reveal is a separate short-lived single-use path in phase 8.

---

## 4. Decisions recorded in phase 3

### 4.1 `parse_webhook` takes the headers as well as the body

SPEC.md §3.1 sketches `parse_webhook(self, body: bytes)`. Lithic does not fit that
signature, and the mismatch is not cosmetic:

- the event id is in the **`webhook-id` header**, not the body. Lithic's delivered
  body is the event *payload* only — no envelope, no id, no delivery timestamp. The
  documented worked example makes this explicit: the signed body in their own
  verification example is `{"acquirer_fee":0,"amount":2000,"authorization_amount":2000}`;
- `card.created` payloads are `{card_token, event_type, replacement_for}` — there is
  **no timestamp anywhere in the body**, so `CardEvent.occurred_at` has to come from
  the `webhook-timestamp` header.

Both are required fields on `CardEvent`, so a body-only `parse_webhook` cannot
produce one. The three ways out were: smuggle the headers in through adapter state,
have the receiver pass a pre-resolved id, or widen the interface. The first is
invisible coupling; the second still leaves no timestamp. So the interface widened
to `parse_webhook(headers, body)`, matching `verify_webhook(headers, body)`, and the
asymmetry between the two webhook methods is gone.

Per the working agreement this is the outcome the rule wants: an adapter that does
not fit means the abstraction is wrong, not that the adapter should be bent. It cost
one signature, one call site in `webhooks/receiver.py`, and no change to `funding/`,
`ledger/` or `api/`. Phase 4's Stripe adapter reads its id from the body and will
simply ignore the argument.

A second provider has since vindicated it: Gnosis Pay's envelope carries no
timestamp *and* no event id, so `gnosis_pay_mock` reads `occurred_at` from
`x-webhook-timestamp` and would be unimplementable body-only (§7.2).

Asserted structurally by `tests/test_issuer_interface.py`, so it cannot silently
narrow again.

### 4.2 Adapters share `base.py` and `core/`, and nothing else

`lithic/signing.py` and `gnosis_pay_mock/signing.py` both contain a six-line
case-insensitive header lookup. That duplication is deliberate.

The rule being protected is "adding an issuer is one adapter file plus one registry
entry". A helper extracted because two adapters wanted it becomes a module the third
adapter has to be written against, and then a module that cannot change without
touching every adapter — which is the same coupling the abstraction exists to
prevent, moved one level down. Six duplicated lines cost less than that, and the two
schemes are genuinely different: Lithic's is a base64 HMAC with a `v1,` prefix and
rotation support over `"{id}.{timestamp}.{body}"`; Gnosis Pay's is a base64 Ed25519
signature with no prefix and no key id over `"{timestamp}.{body}"`, verified with a
published public key. §7.4 records what the same argument then said about
configuration, which was the harder case.

`tests/test_module_boundaries.py` now enforces this in both directions: no module
outside `issuers/` imports an adapter, no adapter imports the pipeline, and no
adapter imports another adapter. `issuers/__init__.py` is exempt from the last one —
naming every adapter is precisely its job.

What adapters *may* share is `app/core/` (`Money`, settings) and `issuers/base.py`.
Both are stable by construction: `base.py` is the contract, and a change to it is
already a breaking change that `tests/test_issuer_interface.py` will report.

### 4.3 An HTTP client, and not Lithic's SDK

SPEC.md §2 names no HTTP client, because phases 1 and 2 needed none. §3.2 requires
real provider calls, so phase 3 does. `httpx` moves from a dev dependency to a
runtime one and `respx` joins the dev ones — `respx` was pre-approved and is
`httpx`-specific, so this is the same decision arriving in two files.

Not the vendor SDK, for three reasons. It brings its own retry policy and its own
models, which would then have to be translated twice — once from their types into
ours, and once around whatever they retry. Its surface is far larger than the eight
endpoints this adapter touches. And phase 4 adds Stripe: two vendor SDKs with two
opinions about connection handling is more code than two small clients, not less.
What the adapter actually needs is four verbs, one auth header, cursor pagination and
error translation, and that is `client.py` — 85 statements, tested to 100%.

`client.py` opens one `httpx.AsyncClient` per request rather than holding one for the
process. A pooled client would be faster, but it is a resource with a lifetime, and
the registry hands out memoized adapters with no `aclose()` in the interface. Adding
a shutdown hook to `CardIssuerAdapter` for something only one adapter has is the kind
of leak §4.1 exists to prevent, and per-request setup is invisible at sandbox scale.
Recorded as a production-path item in §5.

### 4.4 Funding a Lithic card raises its spend limit

**This program has no ledger.** `POST /v1/financial_accounts` answers 403 and
`GET /v1/balances?account_token=…` answers 400 "Account does not support this
operation". So the documented production path — a book transfer into the account's
`ISSUING` financial account, with a client-supplied `token` that is idempotent by
construction — cannot be built or recorded here.

What the program does expose per card is `spend_limit`. So:

- **funding** = `PATCH /v1/cards/{token}` raising `spend_limit` by the amount;
- **balance** = that limit, minus the sum of `amounts.hold` and `amounts.cardholder`
  over the card's transactions.

Three things had to be learned by walking the API rather than reading about it:

1. **`spend_limit: 0` means *unlimited*, not "nothing".** So a zero-limit card cannot
   be expressed at all, and both `create_card` and `fund_card` refuse rather than
   quietly hand out an unlimited card.
2. **`spend_limit_duration` is forced to `TRANSACTION` when the limit is 0**, and a
   later `PATCH` of the limit alone does not change it back. Every write therefore
   restates `FOREVER` — on any other window the limit resets, and funding would leak
   away with the calendar.
3. **The `hold` + `cardholder` sum covers every transaction state** with no status
   table: a pending authorization is a negative `hold`, a settled one a negative
   `cardholder`, a refund positive, and a declined or voided transaction zero on
   both.

### 4.5 The funding idempotency record lives in the card's memo

`fund_card`'s contract is that two calls with one `funding_ref` fund once (SPEC.md
§10). Lithic supports `Idempotency-Key` on `POST /v1/cards` and
`POST /v1/financial_accounts` only — not on the `PATCH` that funding uses — and a
limit raise is a read-add-write, which is exactly the shape that double-applies.

An adapter is stateless with respect to our database (it may not import `funding/` or
`ledger/`), so the record has to live at the provider. The only writable free-text
field a card has is `memo`, so a funding writes
`"<label> [fund:<ref>:<amount_minor>]"` alongside the cardholder's own label, and
`fund_card` reads it back before doing anything. That the *provider* holds it is the
point: it survives our process dying mid-call, which is the failure this contract
exists for.

Bought:

- an immediately retried funding is applied once and returns the same result;
- a ref replayed with **different terms** is refused rather than re-applied, so a
  caller bug is a loud error instead of a double-funded card;
- the tag replaces rather than accumulates, and truncates the label rather than the
  tag if the memo runs long.

Not bought: **only the most recent ref is remembered.** Replaying a *stale* ref after
a newer funding has landed would re-apply it. That is bounded by how the funding
engine works — one intent per card at a time, `PENDING → FUNDED`, retrying the
current intent — and by phase 1's ledger, which dedups on the intent's idempotency
key before an adapter is called at all. It is stated here rather than hidden because
it is a real limit of the scheme, and on a ledger-enabled program the book-transfer
path replaces it with provider-side idempotency and no tag at all.

### 4.6 A Lithic transaction event is keyed on its newest entry, not its status

Lithic sends one event type — `card_transaction.updated` — for the whole life of a
card transaction, re-delivering the entire transaction object each time it changes.
The same event type is therefore an authorization, then its clearing, then possibly a
reversal.

The `status` field is the tempting thing to key on and the wrong one. A voided
authorization arrives with `status: VOIDED`, which says what the transaction *is*, not
what happened — the reversal that caused this delivery would be lost, and `VOIDED` has
no normalized equivalent anyway. So the mapping keys on the newest entry in `events[]`
(by its own `created`, falling back to array order), and `provider_event_type` becomes
a compound label: `card_transaction.updated:CLEARING`.

Consequences worth stating:

- every flavour of authorization (`AUTHORIZATION`, `FINANCIAL_AUTHORIZATION`,
  `AUTHORIZATION_ADVICE`, and the three credit variants) maps to `authorization`;
  those distinctions are network mechanics, not different things to a ledger;
- `BALANCE_INQUIRY` and `RETURN_REVERSAL` are deliberately *not* mapped. Neither has a
  normalized equivalent, and a near-miss is worse than `unmapped` — which keeps the
  full label and the raw payload (SPEC.md §3.3);
- a **declined** authorization is still an `authorization`. There is no normalized
  field for the result, and adding one for a provider detail is what `raw` is for.

### 4.7 `CardEvent.amount` is a magnitude; the type carries the direction

Lithic signs its event amounts: a reversal is `-500`, a refund `-250`, a purchase
`+1234`. `CardEventType` already distinguishes `refund` from `settlement` and
`authorization_reversal` from `authorization`, so a sign in the amount would be the
same information twice — and the second copy is where double-negation bugs live. The
mapping stores `abs()` and leaves `effective_polarity` in `raw`.

This also keeps the two adapters agreeing: `gnosis_pay_mock` emits positive amounts
for refunds, and if Lithic reported `-250` for the same event, no consumer could sum a
ledger without knowing which provider wrote each row.

Two related decisions in the same place:

- **A dispute has no card.** `dispute.updated` identifies a `transaction_token`, not a
  card. Resolving it would take an API call, and `parse_webhook` is pure — the
  receiver calls it a second time for duplicate deliveries. So a chargeback has
  `card_id = None`, with the transaction token in `raw`.
- **A 3DS challenge amount is not normalized at all.** Lithic states it as a decimal
  number plus a `currency_exponent`. Money here is integer minor units only, the
  conversion is exact but only via `Decimal`, and there is no recorded delivery to
  check it against — so `amount` is `None` and the provider's own numbers stay in
  `raw` for phase 7 to convert where it displays them.

Phase 3 ends here. What SPEC.md's revision then changed — and the abstraction defect
it exposed, which phase 4 would otherwise have failed on — is §7.

---

## 5. Deferred decisions

Recorded here when their phase lands, per SPEC.md §11:

- **Why CCTP cannot serve a Solana→Gnosis Chain route**, and what that implies for
  reconciliation — phase 6, with the bridge adapter. (SPEC.md §5.2 names Gnosis
  Chain as the destination, because that is where a Gnosis Pay Safe lives.)
- **deBridge vs Wormhole**, chosen on which has a working Solana→Gnosis Chain
  testnet route at build time — phase 6. Gnosis Pay's own third-party-bridge page
  lists deBridge for "Solana USDC → Gnosis", alongside NEAR Intents, Bungee, LiFi
  and CoW Swap, so the mainnet route exists; whether a Chiado testnet route does is
  the open question.
- **`deposit_address → card` index** for the chain watcher, projected from the
  ledger's `card.created` events — phase 5 (see §3.4).
- **Reconciler thresholds** for stuck intents — phase 5.
- **A retryable marker on issuer errors.** Both `lithic/client.py` and
  `stripe_issuing/client.py` know which failures are worth retrying (429, 5xx,
  timeouts) and retry them themselves, but that knowledge cannot cross
  `issuers/base.py` — so the funding engine cannot yet tell "the provider is busy"
  from "the provider refused". Phase 5 needs the distinction to decide between a retry
  and a `FAILED` intent; adding it before there is a consumer would be guessing at the
  shape. **Phase 4 raised the evidence rather than settling it:** two adapters now
  carry an unused `retryable` flag on their own error types, and they agree on the
  status set, so the shape is no longer a guess — it is waiting on phase 5's consumer.
- **A pooled HTTP client with a shutdown hook.** Both clients open a connection per
  request to avoid putting a resource lifetime on the issuer interface (§4.3). A
  production path pools connections and closes them from a FastAPI lifespan, which
  needs an `aclose()` on `CardIssuerAdapter` that every adapter would inherit for one
  adapter's benefit. §4.3 named Stripe as the trigger for reconsidering; phase 4
  reconsidered and **deliberately did not take it** (§8.6), because widening the
  interface to save a connection setup would be answering a performance question by
  spending the thing being measured. Still deferred, now with a second adapter's worth
  of evidence that it is only a performance question.
- **Verifying Stripe's webhook signature scheme against a real delivery** (§8.2). The
  one thing in §8 a live API key could not settle: it needs
  `STRIPE_ISSUING_WEBHOOK_SECRET` and an actual inbound webhook, which means a
  Dashboard endpoint or `stripe listen` with a tunnel. Until then
  `tests/test_stripe_signing.py` proves we are self-consistent, not that Stripe
  agrees, and the `whsec_`-prefix question is the single open assumption.
- **A real-time authorization endpoint**, if the demo should stop seeing occasional
  `cardholder_verification_required` declines (§8.10). Stripe wants a decision inside
  two seconds; this pipeline verifies, dedups and queues (SPEC.md §4), so serving it
  would be a second, synchronous webhook path — a production-path feature, not a gap.
- **`disable_existing_loggers=False` in `alembic/env.py`.** Running migrations
  currently switches off every logger the app created at import time, so no adapter's
  fail-closed webhook warning is observable from a test (§8.9). One line, found in
  phase 4, outside phase 4's permitted diff.
- **Book-transfer funding** for a ledger-enabled Lithic program, replacing the memo
  tag with provider-side idempotency (§4.4, §4.5). Needs a program this sandbox does
  not have.
- **Signer**: `LocalKeypairSigner` (default) and `FireblocksSigner` behind one
  `TransactionSigner` interface — phases 5 and 9.
- **Resolving a deposit to a card.** One Safe per user means `deposit_address → card`
  is many-to-one (§7.6), so phase 5's watcher needs a second discriminator — most
  likely the funding intent's own amount and card, since the deposit itself carries
  neither.
- **A `reveal` on the issuer interface**, once phase 8 has a caller for it (§7.7).

Deliberately out of scope for the whole project, as production-path items:
Kafka itself, real KYC, physical cards.

---

## 6. Module dependency rule

Every module outside `app/issuers/` may import only `issuers/base.py` and
`issuers/registry.py`; no adapter may import `funding/`, `ledger/`, `webhooks/` or
`api/`. If adding a second adapter required a change to any of those, that is a
design bug in the abstraction, not something to patch around (SPEC.md §12 phase 4
exists to test exactly this).

Enforced by `tests/test_module_boundaries.py`, which parses the import graph rather
than trusting review — and, since §7.4, the settings graph too.

**Tested for real in phase 4**, which added a second `FIAT_RAIL` provider as one
package plus one `register()` line, with zero changes in `funding/`, `ledger/`,
`webhooks/`, `api/` or `core/` and no existing test modified. §8 records what the
adapter had to absorb to make that true, and the two places the interface was
reconsidered and deliberately left alone.

---

## 7. Decisions recorded in the SPEC-revision refactor

SPEC.md was revised after phase 3: §3.2's third adapter is `gnosis_pay_mock`, shaped
on Gnosis Pay's public documentation, in place of a generic `evm_deposit_mock`, and
§5.2's bridge destination is Gnosis Chain rather than BSC. Naming a real target
provider turned the mock from an invention into a rehearsal, and that is what made
the following visible. Sections 1–4 above are left as the record of what was decided
when; where they described behaviour this refactor changed, they now point here.

### 7.1 Funding at a crypto-deposit issuer is an observation, not an action

The old mock's `fund_card` credited a balance and emitted a settlement webhook. That
is a fiat rail with a crypto-sounding label, and it was quietly holding the
`CRYPTO_DEPOSIT` half of the taxonomy up with nothing underneath: a provider whose
`fund_card` can create money is not testing the abstraction against a second shape,
it is testing it against the first shape twice.

At a real crypto-deposit issuer money arrives by an on-chain transfer to an address
the provider controls, and the provider's API cannot make one happen. So:

- `receive_onchain_deposit(safe_address, amount, confirmed=)` is **the chain**, not
  an endpoint. Nothing in `app/` calls it; the bridge (phase 6) is what will make it
  happen for real, and until then a demo or a test plays that part.
- `fund_card` verifies and attributes. It looks for the oldest confirmed,
  unattributed deposit that covers the amount, marks it with our `funding_ref`, and
  returns `SUCCEEDED` with the transaction hash as the issuer's reference.
- With no such deposit the answer is `PENDING`, which the funding engine already
  treats as "not yet funded" and waits on. Nothing is refused and nothing is
  invented.
- `test_funding_does_not_move_money` asserts the balance is unchanged across a
  funding call, so the property cannot quietly stop holding.

Whole-deposit attribution, not partial: a larger deposit is consumed entirely rather
than split. Netting a bridged amount against fees and splitting the remainder is
phase 6's reconciliation problem (SPEC.md §11), and guessing at it now would bake in
an answer before the question exists.

The consequence worth flagging for phase 5: `PENDING` is a *normal* answer here, not
an error, so the engine's `FUNDING` state has to be able to sit in it and retry
rather than treating a non-success as a failure.

### 7.2 A provider with no event id in its envelope

Gnosis Pay's webhook envelope is `{eventType, data}`. There is no event id and no
timestamp anywhere in the body; the timestamp lives only in the signed
`x-webhook-timestamp` header. Two things follow.

**`parse_webhook(headers, body)` is vindicated.** Phase 3 widened the interface for
Lithic (§4.1) and treated it as the abstraction's one allowed change. A second
provider needing the same thing for a different reason is the strongest evidence the
change was structural rather than a concession: this adapter could not be written
against a body-only signature at all.

**Dedup keys on a digest of the body.** `webhook_event_id` returns
`sha256(body).hexdigest()` — the receiver's own documented fallback, returned
explicitly rather than by inheriting `None`, so the ledger's `event_id` and the Redis
dedup key are provably the same value. The body alone, deliberately: their retries
(1, 5 and 15 minutes) are re-signed with a fresh timestamp, so including the
timestamp would make every retry look like a new event. The trade is that two
genuinely distinct events with byte-identical payloads would collide — acceptable
because their payloads carry `threadId` and timestamps, and far better than the
alternative, which double-counts money.

One thing to expect when this becomes a real integration: their webhooks name a card
by `cardToken` while every REST path names it by `cardId`, so the adapter has to
resolve one to the other. In-process that is a dictionary lookup; against a real API
it is an HTTP call, which would make `parse_webhook` fail for network reasons — and
the receiver calls it a second time for duplicate deliveries, so it must stay cheap.
A cache keyed on the token, populated at card creation, is the likely answer.
`gnosis_pay_mock` leaves `card_id` unresolved rather than guessing when the token is
unknown, and keeps the token in `raw` either way.

### 7.3 Real Ed25519, and a derived keypair

Gnosis Pay signs webhooks with Ed25519 over `"{timestamp}.{body}"`, base64 in
`x-webhook-signature`, and publishes the verifying key at
`webhooks.gnosispay.com/api/v1/public-key`. The mock first approximated this with
HMAC-SHA256, keeping the headers, encoding, signed material and window and swapping
only the primitive, because the standard library has no Ed25519.

That approximation lost the property worth modelling. An asymmetric scheme means a
partner holds **no signing secret at all** — nothing to leak, rotate or
misconfigure. Reproducing that is the difference between rehearsing the integration
and rehearsing its shape, so `cryptography` was added (SPEC.md §2 dependency,
approved) and the scheme implemented as documented. The simulator holds the private
key; the adapter is handed the public half and verifies with it, and a test asserts
the adapter has no private key anywhere in its instance state.

`simulator.public_key_endpoint()` reproduces the published response field for field
(`{"success": true, "publicKey": "<SPKI PEM>", "algorithm": "ed25519"}`, checked
against the live endpoint on 2026-07-25), so the shape is written down where the real
integration will need it.

**The keypair is derived from a seed, not generated.** This is the one place the mock
is deliberately unfaithful. `scripts/demo_phase2.py` signs a delivery in one process
for a running server to verify in another; a fresh keypair per process would break
exactly the demo the mock exists for. The seed is a constant in `signing.py` and is
not a credential — it authenticates a Python object in this process to itself.

Two smaller decisions in the same place: `base64.b64decode(validate=True)`, because
`b64decode` silently drops characters outside the alphabet and would otherwise accept
a signature with junk spliced into it; and `load_public_key` *raises* on a
non-Ed25519 PEM rather than returning False, because a wrong key type is a
misconfiguration to fix, not a delivery to reject.

### 7.4 Adapter-owned configuration — the coupling the import graph could not see

`app/core/config.py` carried `lithic_api_key`, `lithic_webhook_secret`,
`lithic_api_base_url`, `lithic_request_timeout_seconds` and
`evm_deposit_mock_webhook_secret`. So "adding an issuer is one adapter file plus one
registry entry" had never been literally true: every adapter cost a change to
`core/`, and phase 4 would have cost two more before Stripe wrote a line.

None of the boundary tests could see it. The coupling ran adapter →
`get_settings()` → a field named after the adapter, and never as an import of the
adapter — so an import-graph test looking for `app.issuers.lithic` in `core/` found
nothing. The proof that it was invisible rather than merely tolerated:
`evm_deposit_mock_webhook_secret` outlived the adapter it was named for, sitting in
`config.py`, `.env.example` and `docker-compose.yml` read by nobody.

The fix is that each adapter package declares its own `BaseSettings` under its own
env prefix (`app/issuers/lithic/config.py`). Variable names are unchanged —
`LITHIC_API_KEY` is still `LITHIC_API_KEY`, because the prefix carries the provider's
name — so this is an ownership change, not a migration for anyone deploying.

Two new guards close the class from both ends:

- **No field in `core/config.py` may be named after an adapter package.** Catches the
  leftover-dead-field case directly.
- **No module under `issuers/` may import `app.core.config` at all.** The structural
  half: an adapter that cannot see the app's settings cannot add a field to them.
  `app.core.money` stays available, because `Money` is shared vocabulary whereas
  settings are ownership.

Both were checked by reintroducing the defect and watching them fail, which is the
only way to know a guard guards anything.

The signature receiving window moved with them. Only adapters ever read it, and it
has to suit one provider's clock skew and retry schedule at a time, so
`WEBHOOK_SIGNATURE_TOLERANCE_SECONDS` became `LITHIC_SIGNATURE_TOLERANCE_SECONDS` and
`GNOSIS_PAY_MOCK_SIGNATURE_TOLERANCE_SECONDS`. The cost is a duplicated default of
300 per adapter, and the `.env` locations repeated in each settings class rather than
imported from `core/` — cheaper than a shared config module every adapter depends on,
which is §4.2's argument applied to configuration.

`LITHIC_API_KEY` stays documented in `.env.example`. The rule that every variable
read by code is documented there outranks tidiness; the file now records which
package declares each block.

### 7.5 `FundingResult.issuer_funding_ref` is optional

A crypto-deposit provider asked to fund before a deposit confirms has no
provider-side object to name, and the mock was returning `""` — a string claiming a
reference exists and is nameless. The field is now `str | None`, and `None` means
nothing has been observed yet.

`FundingIntent.issuer_funding_ref` has been nullable since phase 1 for exactly this
reason, so the DTO and the column now agree instead of routing every `PENDING` result
through a sentinel on its way to a NULL. This is the interface's one breaking change
in this pass; Lithic always has a real reference and was unaffected.

### 7.6 What Gnosis Pay does not publish

The mock is shaped on public documentation, and where that documentation stops the
gap is marked rather than filled in quietly. Anyone integrating for real should treat
each of these as a question for the provider:

- **The `statusCode` ↔ `statusName` pairing.** They publish both sets but only the
  pair `1000 = Active`. The mock assigns the rest, and the *adapter* therefore reads
  the boolean flags (`isFrozen`, `isLost`, `isStolen`, `isVoid`) and never the number
  — so a wrong guess cannot mislabel whether a card can spend money.
- **Token contract addresses.** Omitted entirely rather than invented. `decimals`
  (EURe/GBPe 18, USDCe 6) are labelled representative, not verified.
- **3DS and chargeback webhooks.** They document a dispute *endpoint* but no matching
  webhook, and publish no 3DS event at all — yet SPEC.md §3.3 requires both types and
  §6 needs the 3DS path in phase 7. Both are emitted by the simulator under
  `EXTENSION_EVENT_TYPES`, labelled as ours.
- **Physical card ordering** is deliberately not modelled: SPEC.md §2 excludes
  physical cards, and `POST /api/v1/cards/virtual` bypasses the order flow anyway.

Three provider facts that do reach the rest of the system, and will matter later:

- **One Safe per *user*, not per card.** `Card.deposit_address` repeats across a
  user's cards, so phase 5's `deposit_address → card` index (§3.4) is many-to-one and
  cannot resolve a deposit to a single card by address alone.
- **No per-card spend limit.** `spend_limit_minor` becomes the Safe's on-chain daily
  limit, which every card of that user shares. Tested, so the sharing is visible
  rather than surprising.
- **Amounts are BigInt token units with a per-currency `decimals`**, not minor units.
  `to_money` refuses a value finer than a minor unit rather than rounding — a
  rounding rule here would be a silent, permanent leak. USDCe is the default so the
  demo stays USD-denominated end to end, matching Solana USDC's 6 decimals.

### 7.7 The PSE reveal lives on the simulator only

Gnosis Pay's reveal is a Payment Service Element: the partner backend calls
`POST /api/v1/ephemeral-token` under mTLS, gets a 60-second single-use token, and the
client renders card data in an isolated component. SPEC.md §9.1 now asks for the
demo's reveal to be architecturally identical.

The simulator implements it — TTL, single use, and a distinct error for "already
redeemed" versus "never existed", because those are different incidents. There is no
`reveal` method on `CardIssuerAdapter`, because there is no caller until phase 8 and
adding one now would mean guessing at its shape while every other adapter inherits
the guess.

---

## 8. Decisions recorded in phase 4

Phase 4's purpose is not to add a provider. It is to **test** the abstraction with a
second real one: SPEC.md §12.4 says that if any core module needed changing, that is
a design bug to fix rather than a step to take. So the useful record here is what the
adapter had to absorb, and what it did *not* have to change.

**The result.** The whole phase is one new package plus one `register()` line:

```
backend/app/issuers/stripe_issuing/   __init__ adapter client config signing
backend/app/issuers/__init__.py       +1 register() call (+ the comment explaining it)
```

Zero changes in `funding/`, `ledger/`, `webhooks/`, `api/` or `core/`; no route,
handler, migration, `docker-compose.yml` entry, dependency or mobile screen. **No
existing test was modified**, which is the part worth dwelling on: `GET /providers`
started listing a third issuer, `registry.describe()` started reporting a third
funding model, and every assertion already in the suite kept passing because they
were written as membership rather than equality.

What the phase did add outside `issuers/`, all of it required by rules that predate
it: six new test files and their fixtures (TDD the financial core), and a
`STRIPE_ISSUING_*` block in `.env.example` (every variable documented before it is
read). Neither is a design smell; both would be true of any new adapter.

### 8.1 What Stripe pushed on, and where it stopped

Five things about Stripe do not fit the shape of the interface. All five were
absorbed inside the package.

| Stripe | Where it stopped |
| --- | --- |
| Form-encoded bodies with bracketed nesting, not JSON | `client.form_encode` |
| `Authorization: Bearer`, and test-vs-live decided by the key | `client.checked_api_key` |
| `inactive` means both "never activated" and "frozen" | a metadata marker (§8.3) |
| Cardholders need a billing address and a 24-character name | in-adapter sandbox placeholders |
| Card and event payloads embed the whole cardholder | `raw` allowlists (§8.5) |

The one that could have gone the other way is the cardholder identity data. §5 had
flagged `CreateCardholderRequest` as a likely pressure point, and it was: Stripe
requires `billing[address]` and a display name Lithic does not ask for. It stayed
inside the adapter for the same reason Lithic's `SANDBOX_PHONE_NUMBER` and
`SANDBOX_ADDRESS` did — a sandbox program with KYC out of scope (SPEC.md §2) can
supply obvious placeholders, and a field added to the DTO for one provider is what
`raw` and in-adapter constants exist to avoid. **A real KYC flow would change this
answer**, and that is a production-path item, not an abstraction leak.

### 8.2 The signature scheme, and what we could not verify

`Stripe-Signature: t=<unix>,v1=<hex>`, HMAC-SHA256 over `"{t}.{raw body}"`, several
`v1` values while a secret is rotating, and a `v0` on test events that is
deliberately not valid. Three divergences from Lithic, each with a plausible wrong
answer:

- **The key is the secret exactly as issued**, `whsec_` prefix included. Lithic
  documents base64-decoding what follows its prefix; Stripe documents "use the
  signing secret as the key" and never mentions decoding. Same-looking secret, two
  different keys — and the wrong choice fails only on genuine deliveries, never on a
  test that signs with itself. `test_a_signature_keyed_the_lithic_way_is_rejected`
  pins both directions so the divergence cannot be tidied into agreement.
- **The digest is hex**, not base64.
- **`v0` is never accepted.** Trusting it would make every Dashboard "send test
  webhook" click a way to write to the ledger.

**This is the one thing in §8 still unverified, and it stayed unverified even after
the fixtures were recorded (§8.10).** Lithic publishes a worked example, so
`test_lithic_signing.py` pins our implementation against *their* numbers. Stripe
publishes no vector, so ours were computed independently of the implementation and
are regression pins only. They prove we are self-consistent; they cannot prove
Stripe agrees.

An API key does not settle it: the question is how Stripe *signs an outbound
delivery*, so it needs `STRIPE_ISSUING_WEBHOOK_SECRET` and a real inbound webhook —
a Dashboard endpoint, or `stripe listen` with a tunnel. Everything else in this
section has since been checked against a live account; this has not, and the
`whsec_` prefix is the single assumption one genuine delivery would resolve.

### 8.3 `inactive` means two things, so the adapter keeps the missing bit

Stripe's card statuses are `active`, `inactive`, `canceled`. `CardState`
distinguishes a card that has never been activated from one that was and is now
blocked — and Stripe does not, while defaulting new cards to `inactive`, so both
readings are live at once.

Neither simple mapping works. `inactive → FROZEN` reports a brand-new card as
blocked; `inactive → UNACTIVATED` reports a frozen card as never used. Either way
SPEC.md §9.1's freeze/unfreeze toggle shows the wrong thing.

So the adapter stores the bit itself, as `stablecard_activated_at` in the card's own
`metadata`: present means this card has been activated at least once, therefore
`inactive` now means frozen. This is the same move as §4.5's funding tag in a Lithic
memo — provider-side storage, so it survives our crashing — and it is legitimately
our data, in that Stripe never had it.

Note what it did *not* require: no new `CardState` member, and no provider-shaped
field on `Card`. The alternative considered and rejected was creating cards `active`
so that `UNACTIVATED` never arises, as Lithic's virtual cards genuinely never do
(§4.1's third note). Rejected because Stripe's `inactive` card really does decline,
so calling it active would be a claim about whether it can spend.

**The metadata assumption, now confirmed.** Stripe documents `metadata` as merged
key-by-key; if it ever *replaced* the map, an unfreeze or a funding call would erase
the funding idempotency record. So every write that touches metadata restates the
`stablecard_*` keys it owns, which is correct under either behaviour and costs one
read on `activate_card`. `freeze_card` and `cancel_card` send no metadata parameters
at all, so there is nothing to merge or replace either way.

The live recording settled it: the walk in `scripts/record_stripe_fixtures.py`
freezes the card with a body containing no metadata, then asserts the activation
marker is still there — and it is (`_check_marker_survived`, which prints "§8.3
confirmed"). So the restating in `activate_card` is now insurance against a change
in Stripe's behaviour rather than against our ignorance of it. Kept, because it is
correct either way and one read on a lifecycle call is not worth optimising; the
recorded `card_frozen` fixture is what would catch a regression.

### 8.4 Funding is a spending-limit raise, again — and better

Stripe cards spend from the *account's* Issuing balance. There is no per-card balance
to move money into, so funding is a raise of the card's own `all_time` spending
limit: the same answer as §4.4 reached at Lithic, arrived at for a different reason
(Lithic's program has no ledger; Stripe's model has no per-card one). `all_time` is
the only interval that behaves like a balance — the rest reset, and funding raised
into a monthly limit would leak away at the start of the month, so a card limited on
another interval is reported as having no limit and refused for funding.

Two ways Stripe is materially better, both of which change what phase 5 can assume:

- **`Idempotency-Key` works on every POST**, not only on creation. So a retry inside
  Stripe's 24-hour window replays *their* record of the funding, where Lithic's
  funding idempotency rests entirely on our own marker having been written (§4.5).
  The metadata marker is still there as the backstop past that window. The amount is
  part of the key's material on purpose: two calls with one ref and different amounts
  must reach the check in `fund_card` and be refused, not be silently collapsed by
  Stripe's idempotency layer.
- **"Unlimited" is the absence of a limit, not a zero.** §4.4 records Lithic's
  footgun — `spend_limit: 0` means *unlimited* there, so a card cannot start unable
  to spend and be funded up. At Stripe a zero `all_time` limit is a real "cannot
  spend yet" and is the natural base for a funding raise.

What is *not* better, deliberately: one marker slot, so a card remembers only its
most recent funding ref. That is exactly Lithic's limitation and is kept identical.
The engine advances one intent at a time, so both adapters offer it the same
guarantee — and **the abstraction is tested by the guarantees matching, not by the
implementations matching**. A per-ref scheme was considered (Stripe metadata keys cap
at 40 characters, so a UUID ref would need hashing) and rejected as buying a
capability nothing needs at the price of a collision surface.

`get_balance` is derived, as Lithic's is, because Stripe exposes no per-card balance
(`GET /v1/balance` is the whole account's Issuing funds). Two endpoints rather than
one: settled transactions sum straight in because Stripe signs them (capture
negative, refund positive), and approved authorizations still `pending` come off as
holds. Nothing double-counts, because an authorization leaves `pending` precisely
when it becomes a transaction or releases its hold — and the authorization list is
filtered `status=pending` provider-side so that reasoning is explicit rather than
incidental.

### 8.5 `raw` is an allowlist on both paths, and expansions are collapsed

`raw` reaches the ledger's append-only payload column, and the project's standing
line is that names and addresses do not go there (`cardholder.created` ledgers an
email domain and nothing else).

On the REST path this is the same allowlist discipline §4.1 describes for Lithic,
with one extra reason: Stripe's card object *embeds the whole cardholder*, and can
carry `number` and `cvc` when expanded. Tests assert the personal data is absent
rather than trusting the list to be complete.

On the webhook path it is a **deliberate divergence from Lithic's adapter**, which
keeps its delivered payloads untouched on the grounds that normalizing loses nothing.
That reasoning holds only because Lithic's payloads are flat. Stripe's expand — and
this is now confirmed against real deliveries, not inferred: the recorded
`event_card_created` really does carry the whole cardholder object, name, phone and
postal address included — so nested API objects are collapsed back to the ids they
came from. The
rule is narrow on purpose: only a mapping carrying both `object` and `id` is
collapsed, because only a nested Stripe object can carry somebody's name. So
`merchant_data`, `verification_data` and `request_history` survive whole, and a
reconciler loses nothing it needs.

The envelope's `request.idempotency_key` is kept: it is our own key coming back,
which is how a handler can tie an event to the call that caused it.

### 8.6 An adapter must be constructible without its credentials

`registry.describe()` builds every registered adapter in order to report its funding
model, and `GET /providers` calls it (§3.1's factory decision is what makes the build
lazy in the first place). So an adapter that refuses to *exist* without a key takes
that endpoint down for the providers that do have one.

`StripeIssuingAdapter.from_settings()` therefore validates nothing, and
`checked_api_key` runs on the request path, where the failure can name
`STRIPE_ISSUING_API_KEY`.

**This is a finding about phase 3, not just a decision about phase 4.**
`lithic/client.py` validates its key in the constructor, so `registry.describe()` —
and `test_registry.py`'s test of it, and `GET /providers` — already depend on
`LITHIC_API_KEY` being set. That passes locally because a key is configured; it
would fail in CI. Left alone rather than fixed here: it is phase 3's behaviour and
changing it is not what this phase is for.

The lazy build is also why `config.py` defaults credentials to empty rather than to a
value, and why `client.py` still opens one `httpx.AsyncClient` per request. §5 listed
Stripe as the trigger for reconsidering a pooled client and its `aclose()` on
`CardIssuerAdapter` (§4.3). Reconsidered, and deliberately not taken: this phase's
job is to test the interface, and widening it to avoid a connection setup would be
answering a performance question by spending the thing being measured. It stays
deferred, now with a second adapter's worth of evidence that it is only a performance
question.

### 8.7 Two mapping traps in the event table

Most of `parse_webhook` is a lookup. Two entries are not, and both would be silent
if wrong:

**A captured purchase must not settle twice.** Stripe sends
`issuing_transaction.created` *and* an `issuing_authorization.updated` moving the
authorization to `closed`, for one purchase. Only the first is a `SETTLEMENT`; the
close is recorded as `issuing_authorization.updated:closed` under `UNMAPPED`. Mapping
both would double-count every card payment in the ledger — and nothing would look
wrong until a balance was reconciled.

**A reversal's amount is zero by the time we see it.** Stripe zeroes `amount` when an
authorization is voided, so the magnitude that was actually released survives only in
`data.previous_attributes` — which is exactly what that field is for. Absent it, the
adapter reports the zero rather than inventing a figure. `reversed` and `expired` are
one fact spelled two ways depending on API version, and mapping both is what makes
leaving `Stripe-Version` unpinned safe (`config.api_version` explains why pinning to
a version string this suite cannot check would be worse).

`issuing_authorization.request` is `UNMAPPED` on purpose. It is a two-second request
for an authorization *decision*, not a record that money moved, and this pipeline
verifies, dedups and queues (SPEC.md §4) — so it is structurally not the thing that
answers one. Real-time authorization control is a production-path feature.

Phase 3's one interface change pays off here. `parse_webhook(headers, body)` was
widened because Lithic's event id is in a header and its `card.created` payload has
no timestamp (§4.1). Stripe is the mirror image: id, type and timestamp all sit in an
Event envelope in the body, and the adapter does not read the headers at all. The
widened signature cost Stripe nothing and a body-only signature would have cost
Lithic everything — which is the argument for having changed the interface rather
than smuggling headers past it.

### 8.8 No 3DS challenge from Stripe

This adapter never produces `THREE_DS_CHALLENGE`. Stripe delivers a cardholder's
verification code itself and publishes no issuer-facing challenge webhook, so there
is nothing to normalize — a finding rather than a gap, and the interface accommodates
it without comment because an adapter is free never to emit an event type.

Phase 7 gets the OTP path from the mock adapter's simulator, which SPEC.md §6 already
allows for. If Stripe adds an issuer-facing challenge, `CardEventType` already has
the member.

### 8.9 What the abstraction test actually established

Phase 4 was built with **no Stripe credentials**, which bounded what it proved: every
call shape, signature computation and event mapping was exercised against fixtures
derived from Stripe's documentation, and none had been seen to work against Stripe. So
the honest summary at the end of the phase was that it proved the *abstraction* held
and left the *integration* unproven.

A test-mode key arrived afterwards. §8.10 records what happened when the fixtures were
re-recorded from a live account — three real adapter bugs, and confirmation of three
things that had been assumptions. The claim in §8's preamble is unaffected: the
adapter absorbed everything, and the fixes were all inside its own package.

A defect found on the way, in test infrastructure rather than in the adapter:
`alembic/env.py` calls `fileConfig(config.config_file_name)` with the default
`disable_existing_loggers=True`. The session-scoped migration fixture therefore
switches off every logger the app created at import time, so **both adapters'
fail-closed webhook warnings are invisible to the entire suite** — which is why
`lithic/adapter.py`'s equivalent warning has never been observed by a test.
`test_an_unconfigured_endpoint_fails_closed` re-enables its own logger and says why.
The fix is `disable_existing_loggers=False`, one line in `alembic/env.py`; not taken
here because it is outside this phase's diff.

### 8.10 What a live account changed

`scripts/record_stripe_fixtures.py` re-records the fixtures from a real test-mode
account. `tests/fixtures/stripe_issuing/README.md` says which files are recorded,
which are derived from a recorded card by a stated mutation, and which are still
authored from documentation.

**Three bugs in the adapter, all of which documentation-derived fixtures could not
have caught, and two of which would have broken the financial core.**

1. **`spending_controls[spending_limits_currency]` cannot be sent for a card.** It is
   on the card *object*, and it is a writable parameter on a *cardholder* — which is
   how it got in. On a card, Stripe answers `400 Received unknown parameter`, because
   it is derived from the card's own currency. The adapter sent it in both
   `create_card` and `fund_card`, so **every spend-limited card creation and every
   funding call would have failed.** This is the sharpest illustration of §8.2's point:
   the field is genuinely in the published object, so no amount of careful reading
   would have found it. Only a request would.
2. **A US program requires card-issuing terms acceptance.** Without
   `individual[card_issuing][user_terms_acceptance][ip]` and `[date]`, the cardholder
   sits at `requirements.past_due` and **every `activate_card` fails.** Sandbox
   placeholders now go in alongside the address ones, and `SANDBOX_TERMS_IP` carries
   the caveat that this placeholder attests to something a person did — the one field
   a real program would genuinely need `CreateCardholderRequest` to grow (§8.1).
3. **`issuing_authorization.created` is also sent for a *declined* attempt**, with
   `approved: false` and a reason in `request_history`. Mapping it to `AUTHORIZATION`
   regardless put an amount in the ledger for money that was never held. A decline is
   now `UNMAPPED` under `issuing_authorization.created:declined`. Not hypothetical: on
   a program with no real-time authorization endpoint Stripe declines some attempts
   with `cardholder_verification_required`, because it wants the issuer to decide
   inside a two-second window and nobody is listening. `get_balance` already refused
   to count an unapproved hold; this is the same fact on the event path.

**Three assumptions confirmed**, each of which had been carrying a hedge:

- **Metadata is merged, not replaced** (§8.3). The recording walk freezes a card with a
  body containing no metadata and asserts the activation marker survives. It does.
- **Webhook payloads really do expand nested objects** (§8.5). The recorded
  `event_card_created` carries the whole cardholder — name, phone, postal address — so
  collapsing expansions before `raw` reaches the ledger is load-bearing, not
  theoretical.
- **A reversal really does zero `amount`** and put the released figure in
  `previous_attributes` (§8.7). The recorded `issuing_authorization.updated` has
  `amount: 0` and `previous_attributes: {"amount": 500, ...}`, exactly as designed for.

**And one thing still unverified:** the signature scheme (§8.2). An API key cannot
settle how Stripe signs an outbound delivery.

Three smaller facts worth keeping:

- `request-id` is the header carrying Stripe's request id, as assumed.
- This account's default API version is `2026-06-24.dahlia`, which spells a lapsed
  authorization `reversed` and never `expired`. Mapping both is why leaving
  `Stripe-Version` unpinned costs nothing (§8.7).
- Stripe's own published example objects contain merchant categories that are **not in
  their enum** — `hotels_motels_resorts` is rejected. Another reason a recorded fixture
  outranks a documented one.

**On funding a test-mode Issuing balance**, since the demo needs one and the answer is
not where the docs suggest: `POST /v1/test_helpers/issuing/fund_balance` exists but is
for funding-instruction (UK/EU) accounts and answers "A Funding Instruction is not
currently supported for US". `POST /v1/topups` works with no source and accepts
`destination_balance=issuing`, but the top-up stays `pending` in test mode. The
Dashboard (Issuing → Balance → Add funds) credits instantly. With a zero balance every
simulated authorization is declined `insufficient_funds` and cannot be captured, so the
recorder treats an empty balance as a warning that skips the purchase walk and names
the fixtures it left alone — a partial recording beats a recording of declines that
looks like a recording of purchases.

**Three bugs in the recorder itself**, kept here because the third is a trap worth not
repeating: it sent the same bad `spending_limits_currency`; it omitted the same terms
acceptance; and it **wrote fixtures from responses it had not asked for**, so an
unexpected 400 replaced a good fixture with an error body — after which the suite would
have passed against a recording of our own bug. `Recorder.call` now writes only when the
status matches `expect`, and says out loud when it does not.
