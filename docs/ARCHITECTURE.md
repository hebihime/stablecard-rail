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
- ~~**A retryable marker on issuer errors.**~~ **Settled in phase 5 — see §9.1.**
  `IssuerError.retryable` now carries what both clients already knew privately, and
  the funding engine can tell "the provider is busy" from "the provider refused"
  without knowing which provider it is. Deferring it until there was a consumer was
  the right call: the consumer is what settled it as a per-raise flag rather than an
  exception subclass.
- **A pooled HTTP client with a shutdown hook.** Both clients open a connection per
  request to avoid putting a resource lifetime on the issuer interface (§4.3). A
  production path pools connections and closes them from a FastAPI lifespan, which
  needs an `aclose()` on `CardIssuerAdapter` that every adapter would inherit for one
  adapter's benefit. §4.3 named Stripe as the trigger for reconsidering; phase 4
  reconsidered and **deliberately did not take it** (§8.6), because widening the
  interface to save a connection setup would be answering a performance question by
  spending the thing being measured. Still deferred, now with a second adapter's worth
  of evidence that it is only a performance question.
- **A real-time authorization endpoint**, if the demo should stop seeing occasional
  `cardholder_verification_required` declines (§8.10). Stripe wants a decision inside
  two seconds; this pipeline verifies, dedups and queues (SPEC.md §4), so serving it
  would be a second, synchronous webhook path — a production-path feature, not a gap.
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

**Confirmed by a live delivery on 2026-07-26.** It was the last open question in the
phase, and it could not be answered by an API key: the question is how Stripe *signs
an outbound delivery*, so it needed `STRIPE_ISSUING_WEBHOOK_SECRET` and a real inbound
webhook.

Method, and the negative control that makes it mean something:

    stripe listen --forward-to 127.0.0.1:8000/webhooks/stripe_issuing
    stripe trigger issuing_card.created

With the listener's secret in `.env`, three genuine deliveries came back **`200`** and
appear in `ledger_events` under `webhook:stripe_issuing:evt_…`, mapped as
`issuing_cardholder.created → unmapped` and `issuing_card.created → card_lifecycle`.
Restarting the app with a wrong-but-well-formed secret and repeating the trigger gave
**`401`** on all three. So the 200s were verification passing, not verification being
skipped, and **the `whsec_` prefix is part of the HMAC key.**

The hex-shaped secret is why this mattered more than it looks. `stripe listen` issues
`whsec_` followed by 64 hex characters, and hex digits are a subset of the base64
alphabet — so Lithic's rule (strip the prefix, base64-decode the rest) *succeeds* on a
Stripe secret and silently yields a different key. There is no error to notice; every
genuine delivery would simply 401. That is the failure mode
`test_a_signature_keyed_the_lithic_way_is_rejected` exists to prevent.

**What is deliberately not tested.** The strongest possible test would pin a real
captured delivery — raw body plus its `Stripe-Signature` — against our verifier in the
suite. Verifying it requires the endpoint secret, and committing a webhook secret to a
tracked file breaks a hard rule of this project that outranks the value of the test. So
the live check is recorded here, with its method and date, and the unit vectors remain
what they always were: self-consistency pins computed independently of the
implementation. Lithic still has the stronger position, because Stripe publishes no
worked example to pin against.

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

**This started as a finding about phase 3.** `lithic/client.py` validated its key in
the constructor, so `registry.describe()` — and `test_registry.py`'s test of it, and
`GET /providers` — depended on `LITHIC_API_KEY` being set: green locally, red in CI.
Reported rather than fixed during phase 4, because `lithic/` was outside the permitted
diff and was itself under review. **Fixed afterwards** — see §8.11.

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
`alembic/env.py` called `fileConfig(config.config_file_name)` with the default
`disable_existing_loggers=True`. The session-scoped migration fixture therefore
switched off every logger the app created at import time, so **both adapters'
fail-closed webhook warnings were invisible to the entire suite** — which is why
`lithic/adapter.py`'s equivalent warning had never been observed by a test. Reported
rather than fixed during the phase, because `alembic/` is phase 1's; **fixed
afterwards** — see §8.11.

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

### 8.11 The two findings phase 4 reported, fixed afterwards

Phase 4 ran under a hard "no diffs outside `issuers/` and the registry" constraint, so
two defects it found in *other* phases' code were written down rather than repaired
(§8.6, §8.9). Both are now fixed, each in its own commit, once that constraint was
lifted. Recording them here because the reasoning in each case is about *where* a check
belongs, which is a decision the next adapter will face too.

**`alembic/env.py` disabled every app logger.** `fileConfig(config.config_file_name)`
defaults to `disable_existing_loggers=True`, and the session-scoped `migrated_database`
fixture runs migrations *after* `app.*` is imported — so every logger the app had
created was switched off for the rest of the session. The symptom was specific and
misleading: `caplog.text` came back empty while the same call logged correctly outside
pytest.

What it hid: `verify_webhook` returns a bare `False`, so the warning naming the missing
secret is the **only** thing separating "this delivery was forged" from "you never
configured an endpoint secret". Neither adapter's version of that warning had ever been
observed by a test. Both now are, and `test_stripe_webhooks.py` losing its
`monkeypatch.setattr(logger, "disabled", False)` workaround is the regression test —
if migrations ever switch app loggers off again, that test fails.

**`lithic/client.py` validated its API key in the constructor**, which made
`registry.describe()` — and therefore `GET /providers` — depend on `LITHIC_API_KEY`
being present. Green on a machine with credentials, red in CI. Demonstrated by putting
the old check back with `.env` moved aside: three tests fail, one of them the route
test itself.

The interesting part is *why* it was written that way, because it was a deliberate
choice rather than an oversight: by analogy with the webhook-secret check in
`lithic/adapter.py`, which really should be eager. **The analogy does not hold, and the
distinction is worth keeping:**

| | Empty webhook secret | Empty API key |
| --- | --- | --- |
| How it fails without an early check | silently, as "invalid signature" on every genuine delivery | loudly, as a 401 saying "Please provide a valid API key" |
| Where it sends the person debugging | Lithic's dashboard — the wrong system | `.env` — the right one |
| So the early check buys | a correctly attributed error | nothing |
| And costs | nothing | `GET /providers`, for every provider |

So: **validate eagerly when the failure would otherwise be silent or misattributed;
validate on the request path when it would not.** The Stripe adapter was written to that
rule from the start (§8.6), which is how the Lithic version came to light at all —
building the second adapter is what made the first one's placement visible as a choice.

---

## 9. Decisions recorded in phase 5

Phase 5 is where the pipeline stops being a set of parts. Everything before it was
driven by a caller — an HTTP request, a webhook, a demo script. This phase adds the
two things that move money on their own: a watcher that turns an on-chain event into
an intent, and an engine that walks that intent to `FUNDED` without anyone asking.

The rule that shapes all of it: **`advance()` remains the only writer of funding
state.** The engine decides *what* hop to attempt and *whether* a failure is worth
another go; the transition table decides whether the hop is legal, and the ledger
records it either way. An engine that could write `intent.state` directly would make
the phase-1 guarantee decorative.

### 9.1 A retryable marker on issuer errors

Deferred since phase 3 and listed in §5, on the explicit ground that adding it before
there was a consumer would be guessing at the shape. Phase 5 is the consumer.

The engine catches `IssuerError` — it never learns which provider raised, by the
module rule (§6) — and has to choose between two very different actions:

| The provider said | The engine should |
| --- | --- |
| 429, 503, or nothing at all (timeout) | try the same call again; the funding may yet land |
| no such card, canceled card, amount refused | stop, and mark the intent `FAILED_FUNDING` |

Without a marker on the shared base class, that choice cannot be made from above
`issuers/`. Both HTTP clients already *knew* the answer — each carried a private
`retryable` flag on its own error type, and the two independently arrived at the same
status set (`429, 500, 502, 503, 504`, plus a status of `0` for "nothing answered") —
but the knowledge stopped at the package boundary. So this is not a new idea, it is
the same idea moved to where the caller can reach it: `IssuerError.retryable`, with
`LithicApiError` and `StripeApiError` passing their existing determination through.

Three things it deliberately is not:

- **Not an instruction.** It says a retry *could* work, not that one should happen,
  how many times, or how far apart. Retry policy is the engine's, and it belongs
  next to `retry_count` and the transition table rather than in an adapter. Both
  clients have already exhausted their own internal HTTP retries before raising.
- **Not an exception hierarchy.** A `TransientIssuerError` subclass would have made
  retryability a property of the error's *type*, and the same Stripe error type is
  transient at 503 and permanent at 404. The flag varies per raise because the
  reality does.
- **Not true by default.** A wrongly-retried permanent refusal spends the retry
  budget a genuinely transient failure needed and delays the `FAILED_*` an operator
  has to see; a wrongly-failed transient error costs one re-opened intent. The
  cheaper mistake is the default, so `retryable` is opt-in per failure.

It is a class attribute with a keyword override rather than a plain constructor
argument, so an adapter whose failures are *always* transient can say so once by
subclassing instead of restating it at every raise site.
`tests/test_issuer_interface.py` pins that, along with the default being `False` for
every error the base module defines.

**What this does not cover, and what the engine does about it.** An exception that is
not an `IssuerError` at all — a bug in our code, a database blip — has no marker and
deserves no guess. The engine treats those as a third case: it leaves the intent in
its current state rather than failing it, and the reconciler (§5.3) picks it up. That
keeps a deploy-time `AttributeError` from permanently killing every in-flight intent,
which is what "unknown means failed" would have done.

### 9.2 The bridge interface is two calls, and three states

`BridgeProvider` is `submit(order)` and `status(bridge_ref)`. That is the shape
every bridge and aggregator actually offers, because a cross-chain transfer cannot
be synchronous: the source chain has to finalize before the destination chain can
be told anything at all.

What was deliberately left out is more interesting than what is in:

- **No quote or route-selection call.** LiFi and deBridge both expose one, and a
  production integration would use it — but the funding engine has no decision to
  make with a quote. It has one route and one destination, and an amount it did not
  choose. Adding the call now would mean designing a fee-and-route model against a
  provider we have not integrated yet (phase 6), which is how an interface ends up
  shaped like the first implementation.
- **No milestone states.** `BridgeStatus` is `PENDING`, `COMPLETED`, `FAILED` and
  nothing else. Real bridges report source-confirmed, relayer-picked-up,
  destination-submitted — different sets, in different orders, per protocol. The
  funding machine's own `BRIDGING` covers all of them, and a status enum that
  mirrors one provider's milestones cannot hold the next one's.
- **No chain taxonomy.** `source_chain` and `destination_chain` are opaque strings
  (`"solana-devnet"`, `"gnosis-chiado"`), like provider ids. A route is something a
  provider either supports or does not; enumerating chains before phase 6 has to
  negotiate a real one would be enumerating them blind.

**`amount_in` and `amount_out` are separate fields**, and `amount_out` is `None`
until the transfer completes. SPEC.md §11 names "bridged amounts net of fees" as a
reconciliation concern, and this is where it becomes concrete: the engine funds the
card with what *arrived*, never with what was deposited. One amount that quietly
changes meaning between submit and completion would make the fee invisible exactly
where it matters. The simulator can charge a fee (`SIMULATED_BRIDGE_FEE_MINOR`) so
that path is exercised before phase 6 makes it real.

### 9.3 No bridge registry, and who the destination is

`issuers/` has a registry; `chain/bridge/` does not, and the difference is what the
identifier is *for*. A `provider_id` is written onto every funding intent and every
ledger row, so it must be resolvable from stored data years later — that is what
`registry.get_adapter()` exists to do. Which bridge this process uses is a
deployment choice made once at startup. So the engine takes a `BridgeProvider` as
an argument and the composition root picks one; phase 6 adds an implementation and
changes one line in a demo script rather than adding a second lookup table.

**The destination address depends on the funding model** (§3.2), which is the point
where the bridge and the issuer taxonomy meet:

| Issuer's funding model | Bridge delivers to | Then funding is |
| --- | --- | --- |
| `CRYPTO_DEPOSIT` (Gnosis Pay) | the card's own deposit address | arrival itself — `fund_card` observes it (§7.1) |
| `FIAT_RAIL` (Lithic, Stripe) | our own settlement address | a separate API call the engine makes |

Both paths run the same four transitions, which is why the engine does not branch
on the taxonomy: it asks the adapter to fund and lets the adapter decide what that
means. The difference shows up only in which address goes on the order.

### 9.4 The simulator is deterministic, not random

A failure rate would have been fewer lines. It would also mean a demo that fails
one run in ten for reasons the person watching cannot see, and a test suite that
goes red on someone else's machine. So: latency is measured against a clock the
caller supplies, and failure is a **mode** the caller selects. The same order
produces the same references, the same amounts and the same sequence of states
every time, and a test can move an hour in one line instead of sleeping.

The four modes are the four shapes a real bridge fails in, and `STUCK` is the one
worth having — accepted, then silence. Nothing distinguishes a stuck transfer from
a slow one except elapsed time, which is the entire argument for SPEC.md §5.3's
reconciler, and this is what lets the reconciler be tested rather than asserted.

### 9.5 Polling with a cursor, not a websocket subscription

SPEC.md §5.2 allows either and prefers a websocket subscription "if
straightforward". It is not, and the reason is worth stating because it looks like
the modern choice.

`logsSubscribe` delivers at most once, over a connection that can drop without
saying so, and offers no way to ask what was missed while it was down. Every
production deployment of it ends up with a polling reconciler behind it anyway — at
which point the subscription is a latency optimisation on top of the thing that
actually guarantees delivery. A poll with a persisted cursor answers "what happened
since X?" every time it runs, which is the property a funding pipeline needs. And
the latency argument is weak here specifically: this watcher waits for `finalized`
commitment (SPEC.md §5.2), so it is already seconds behind by design.

**The cursor moves after the work, never before.** The caller creates the funding
intents, then advances the cursor, in that order — so a crash in between re-observes
deposits rather than losing them. Re-observing is safe because
`funding_intents.deposit_tx_ref` is unique: the second attempt to open an intent for
the same signature is refused by the database. At-least-once plus a unique key is
exactly-once effect, the same bargain §2.4 makes for webhook deliveries.

Two smaller decisions fall out of that:

- **A cursor cannot rewind.** Nothing in the loop should try, but a cursor that can
  move backwards is a cursor that can re-credit a card, and making it impossible is
  cheaper than auditing every caller.
- **An unreadable transaction stops the page** rather than being skipped. A
  signature appears in the index before the transaction is fetchable; skipping it
  would step the cursor past a deposit that is seconds from appearing, and since the
  cursor is passed back as `until`, the node would never be asked about it again.

### 9.6 A deposit is a balance difference, not a parsed instruction

The watcher does not read instructions. It finds the watched account in the
transaction's account list and subtracts its `preTokenBalances` entry from its
`postTokenBalances` entry.

That is correct for a plain `transfer`, a `transferChecked`, a `mintTo`, and a
transfer made three programs deep inside a CPI — none of which look alike to a
parser, and all of which are money arriving. An instruction parser would have to
keep up with every program that can move tokens; a balance diff does not care which
one did.

Three things the recorded devnet fixtures settled, each of which is a bug if guessed
(`backend/tests/fixtures/solana/README.md` has the evidence):

| The trap | What actually happens |
| --- | --- |
| A failed transaction carries no balances | It carries `postTokenBalances` — the simulated ones from before it failed. Check `err` first, or credit money that never moved. |
| A new account has a zero `pre` balance | It has **no `preTokenBalances` entry at all**. Missing must read as zero, or every card's opening deposit is invisible. |
| `uiAmount` is the amount | `uiAmount` is a **float**. The integer string beside it is the amount, and nothing here ever reads the float (§2.8). |

### 9.7 Six decimals into two, and what is left over

USDC has six decimals; a USD card has two. So 1.000000 USDC is 1_000_000 base units
and 100 minor units, and the conversion is an integer division by 10^(6−2) — never a
float, never a `Decimal` rounding mode.

It **truncates**, and the remainder is kept. Crediting a cent that did not arrive is
how a funding pipeline goes short, so a deposit of 1.234567 USDC funds 123 cents and
records 4_567 base units of dust on the intent. A deposit below one cent funds
nothing at all and is reported as ignored rather than dropped: opening an intent for
zero would fail the `amount_minor > 0` constraint the state machine has had since
phase 1, and failing it quietly at the database is worse than refusing it out loud
in the watcher.

**Everything that touches the address and is not a deposit is still accounted for.**
`DepositPage` carries an `ignored` list with a reason per signature — failed on
chain, wrong mint, outgoing, below a cent, not ours. It is the same principle as
ledgering `unmapped` webhooks (§3.9): "nothing happened" and "we decided nothing
happened" are different, and only one of them can be audited.

### 9.8 The deposit index is a row, not a projection — and §3.4 named the wrong address

§3.4 deferred a `deposit_address → card` index to phase 5 and predicted its shape:
a projection of the ledger's `card.created` events, which already record a
`deposit_address` for exactly this purpose. Building the watcher showed the
prediction was wrong, and the reason is worth keeping because it is a distinction
the whole pipeline turns on.

**There are two deposit addresses, and they belong to different parties.**

| | Whose | What it is | Who knows it |
| --- | --- | --- | --- |
| **Source** | ours | the Solana account the user sends USDC to | this service assigns it (SPEC.md §8's demo deposit keypair) |
| **Destination** | the card's | the Safe a `CRYPTO_DEPOSIT` issuer credits | the provider, via `Card.deposit_address` |

`card.created` records the *destination*. The watcher polls the *source*, and no
provider has ever heard of it — so there is nothing to project it from. Hence
`deposit_routes`: one row per watched address, naming the card it funds.

**What is deliberately not in that row**: the destination address, the card's state,
its balance. §3.4's argument against a local card table is unchanged — the provider
owns that, and a second copy is a cache that can silently disagree. The engine asks
the adapter for the destination when it submits the bridge order.

**`(chain, deposit_address)` is the primary key**, which settles the other question
§5 left open. It worried that one Safe per user makes `deposit_address → card`
many-to-one (§7.6) and that the watcher would need a second discriminator — most
likely a per-intent expected amount. It does not, because that many-to-one mapping
is about the *destination* address. One source address funds one card, and making a
second card unrepresentable is a cheaper answer than a tie-break rule that has to be
right about money. The reverse direction is one-to-many and indexed: a card funded
from a second chain has a second address, and the fund screen (SPEC.md §9.3) lists
them.

**Re-pointing an address at a different card is refused**, while re-registering the
same pair is a no-op. The fund screen will register on every open, so the no-op has
to be free; and a transfer already in flight to that address was sent for the card
it pointed at when the sender read it, so silently re-pointing would credit the
wrong card on arrival.

### 9.9 Two states learned to retry, and one deliberately did not

Phase 1 gave the self-transition — the retry — to `PENDING`, `BRIDGING` and
`FUNDING`, on the rule "only the states that wait on an external system are
retryable". Building the engine showed that rule was one word too narrow.

`DEPOSIT_CONFIRMED` does not *wait* on anything: it is about to submit a bridge
order. `BRIDGED` is about to call `fund_card`. Both of those calls can fail
transiently — a 503, a timeout, a rate limit — and with no self-loop there was
nowhere to record the attempt. An intent would sit in `DEPOSIT_CONFIRMED` while the
reconciler re-tried it forever, because SPEC.md §5.3's cap counts `retry_count`, and
`retry_count` can only move through `advance()`.

So the rule is now: **a state retries in place if, and only if, leaving it requires
an outbound call.** That is `PENDING` (ask the chain), `DEPOSIT_CONFIRMED` (submit),
`BRIDGING` (poll), `BRIDGED` (fund), `FUNDING` (fund or poll). `FUNDED` is the one
non-terminal state left without a loop, and that is not an oversight: it waits for a
settlement webhook to *arrive*, and nothing we do makes that happen sooner. A retry
there would be a busy-wait with a counter on it.

The golden matrix in `test_transition_table.py` exists to make exactly this change
deliberate, and it did its job — the edit failed the suite until the expectation was
updated by hand.

**One existing test changed meaning rather than merely breaking**, which is the more
interesting half. `test_concurrent_advances_are_serialised_by_row_lock` raced two
workers on `PENDING -> DEPOSIT_CONFIRMED` and asserted that the loser was rejected.
With the self-loop, the loser now finds `DEPOSIT_CONFIRMED` and records a legal
retry — it tests the counter, not the lock. It now races `FUNDED -> SETTLED`, where
the target is terminal, so "applied twice" stays unambiguously illegal and the test
goes on testing what it was written to test.

### 9.10 The deposited amount and the bridged amount are different columns

`funding_intents` gained `bridged_amount_minor`, nullable, set by the
`BRIDGING -> BRIDGED` transition that learns it.

It could have overwritten `amount_minor`. That would have been wrong in a way that
only shows up during an investigation: the deposit is a fact about the source chain
and the delivered amount is a fact about the bridge, and a pipeline that keeps one
number cannot answer "where did the missing 1.50 go?" months later. Two columns make
the fee the difference between two recorded values, and `INTENT_TRANSITIONED` records
the update alongside the hop.

`fundable_money` is what the card is funded with: the bridged amount when there is
one, the deposited amount when there is not. A card funded with `amount_minor` after
a fee is a card funded with money nobody has.

`bridged_amount_minor` is on `MUTABLE_INTENT_FIELDS` and `amount_minor` is not — the
deposit is history; what survived the bridge is a new fact about it.

### 9.11 Coverage was under-reporting, and SQLAlchemy's greenlet is why

While closing the last gap in `funding/deposits.py`, two `except IntegrityError`
handlers reported as never executed. They were executed: replacing the body with a
`raise AssertionError` made three tests fail. Swapping coverage's tracer core
(`sysmon`, `pytrace`, `ctrace`) changed nothing.

The cause is that **SQLAlchemy's async layer runs the sync DBAPI inside a
greenlet**. A line executed while a greenlet is switched in is invisible to a
tracer that is not told about greenlets, and an `IntegrityError` from an awaited
query is raised *through* that switch — so the handler that catches it is measured
as dead code. `concurrency = ["greenlet", "thread"]` in `[tool.coverage.run]` fixes
it.

Two things worth carrying forward:

- **The design should not be shaped by a misdiagnosed tool.** Before finding the
  real cause, the code had been contorted twice to keep the tracer happy — a
  handler rewritten to end at its `await`, then a flag hoisted out of the block.
  Both were reverted. What survived is the change that was right on its own terms:
  a re-observed deposit is *expected* on every restart, so the ordinary path is now
  a `SELECT` and the unique index is the backstop for the race — the same two
  layers as webhook dedup (§2.4), and the reason the handler is now genuinely rare.
- **The technique generalises.** Before believing a line is dead, make it raise. If
  the suite stays green, it is dead; if the suite fails, the measurement is wrong.
  That is a two-minute check that beats any amount of reading.

Phases 1-4 were re-measured with the corrected setting and are still at 100%, so
nothing was hiding behind the old numbers — but they were true by luck rather than
by measurement, which is worth knowing.

### 9.12 `SETTLED` needs a reference no provider currently sends

The settlement consumer is the first subscriber on the `EventBus` — phase 2 built
the pipe and left the subscriptions to the phases that own them (§3.10), and this is
funding's. It attributes a settlement to an intent on `CardEvent.funding_ref`, the
field that exists for exactly that, **and on nothing else.**

That is a refusal, and it overrules a note phase 4 left in the mock adapter:

> Their transactions carry no reference to our funding intent: money arrives
> on-chain, so a settlement cannot echo a `funding_ref`. Phase 5 reconciles on the
> card and the amount instead.

Phase 5 does not, because at all three providers `SETTLEMENT` is overwhelmingly a
**purchase clearing**, not a funding landing. Reconciling on card and amount would
let a $25 coffee settle a $25 top-up. That is a false reconciliation: silent, wrong,
and worse than the alternative, which is that the intent stays at `FUNDED` — where
it is accurate, because the provider said the card has the money.

So `FUNDED` is where intents rest at these three providers, and that is a fact about
what the providers send rather than a gap in the machine:

| Provider | Why no attributable settlement |
| --- | --- |
| `gnosis_pay_mock` | funding *is* an on-chain deposit, and the chain has never heard of our intent id |
| `lithic` | funding is a spending-limit raise; there is no settlement event for one |
| `stripe_issuing` | same, and its settlements are `issuing_transaction.created` for spend |

**What would make it reachable**, in order of how much each is worth: a provider that
echoes `request.idempotency_key` on the settlement of a funding (Stripe already
returns ours on *card* events, so this is closer than it looks); a book-transfer
funding at a ledger-enabled Lithic program (§4.4), where the transfer has an id and
a completion event; or a reconciler that queries the provider and settles on
evidence rather than on a webhook. The first two are provider capabilities we do not
have; the third is a real option and is deliberately not taken here, because
"reconciled" should mean the provider told us so.

Failure handling is the phase-2 path unchanged: a settlement naming an intent that
is *not* `FUNDED` is an illegal transition — ledgered, then retried and
dead-lettered (§3.7). It means the provider believes a funding completed that we do
not, which is exactly the kind of thing that should end up in front of a person.

### 9.13 The reconciler's backoff is elapsed time, and its scan has two exclusions

SPEC.md §5.3 asks for a task that finds stuck intents, re-queries, and retries with
backoff up to a cap. Two decisions in how that is built.

**Backoff is elapsed time, not sleep.** There is no worker holding a timer: an
intent is simply not eligible again until its state has been unchanged for
`stuck_after_seconds * 2**retry_count`, capped by `max_backoff_seconds`. Every
retry bumps `state_changed_at` and `retry_count` through `advance()`, so the
schedule is a property of the row — it survives a restart, it is the same for every
worker, and it is visible to anyone reading the table. A sleeping worker's backoff
is none of those things.

The cap matters in the other direction too: without it, an intent that retried
eight times would next be looked at in four hours, which is indistinguishable from
abandoned.

**Two states the scan will not touch.**

- **`FUNDED`.** It is waiting for a settlement event that, at all three providers
  here, may never be attributable (§9.12). Failing an intent whose card *has the
  money* would turn a provider limitation into a fabricated incident. So `FUNDED`
  is excluded from the scan entirely rather than given a longer threshold.
- **`PENDING` with no `deposit_tx_ref`.** Nobody has sent anything. There is
  nothing stuck; there is an invoice waiting to be paid.

**`PENDING` *with* a `deposit_tx_ref` is the one thing only the reconciler can
fix**, and it is worth spelling out because it is invisible from anywhere else. If
a process dies between `create_intent()` and the `DEPOSIT_CONFIRMED` transition,
the deposit now *has* an intent — so every later watcher poll finds one, records a
duplicate, and moves on (§9.5). The intent would sit in `PENDING` forever while the
money sits on chain. The reconciler advances it, and the `deposit_tx_ref` is what
makes that safe rather than a guess: the watcher only ever reports `finalized`
transfers, so the reference existing *is* the confirmation.

**The scan orders oldest-first and takes a batch.** With a limit, ordering is what
stops the longest-stuck intent from being starved by a steady arrival of newer
ones. The exponential part of the eligibility test is applied in Python rather than
in SQL, because `POWER(2, retry_count)` in a predicate is not something an index can
serve — and what it filters out is a handful of recently-retried rows, not a table
scan.

### 9.14 `solders` only, and what the signer interface deliberately cannot do

SPEC.md §2 names "`solders` / `solana-py`". Only `solders` is installed, and only
for the things a wire format cannot do: keys, signatures, and program-derived
addresses. The RPC surface this phase needs is **two methods** —
`getSignaturesForAddress` and `getTransaction` — over the `httpx` that was already
here, and writing them is what makes the watcher testable by replaying recorded
responses through `respx`, exactly as the issuer contract tests replay theirs
(§4.3's reasoning about vendor SDKs, applied again).

**The signer interface is a public key and a signature over bytes**, and nothing
else. It cannot build a transaction, fetch a blockhash, or submit one — because
phase 9's `FireblocksSigner` is an HTTP call to a custody service with a policy
engine and an approval flow behind it, and a custody service signs a payload
without an opinion about what the payload was for. An interface that took a
`Transaction` object could not be implemented by something that has never heard of
`solders`, which would make the "swap in Fireblocks" claim (SPEC.md §8) false in
the only way that matters.

So what does the signer *do* in phase 5, with no transaction to sign? **It owns the
deposit address.** A wallet address does not hold USDC; the associated token account
it derives to does, and that derived address is the "Solana devnet deposit address"
the fund screen shows (SPEC.md §9.3) and the watcher polls (§9.8's *source*
address). `app/chain/tokens.py` derives it, and the test for that derivation is
pinned to evidence rather than to itself: the recorded devnet transfer names both
the token account it credited *and* the wallet owning it, so the derivation has to
reproduce what the chain already did.

Submitting a transfer arrives with the thing that owns the money — the in-app
wallet in phase 8. The demo does not send one, and says so.

### 9.15 What the phase-5 demo runs on

`scripts/demo_phase5.py` has two modes, and the default needs **no network and no
credentials**: it replays the recorded devnet responses, so the walk-through cannot
fail because a public endpoint is rate-limiting. `--live` points the same watcher at
devnet and polls read-only, which means whatever transfers that account is actually
receiving drive real intents.

Three things the demo made visible that no unit test would have:

- **A replay run would poison a live run's cursor.** Both watch the same address,
  and a cursor is keyed on `(chain, address)` — so the synthetic signature a replay
  records would be handed to the real node as `until`, which answers `Invalid param:
  WrongSize`. Replay runs now record under a chain label of their own. The
  underlying rule is worth keeping: **a cursor is only ever valid for the world that
  wrote it.**
- **The simulated bridge moves nothing**, so when it reports delivery the demo
  reflects that into the mock provider's Safe. That is not a workaround; it is the
  seam §7.1 describes made concrete — `fund_card` at a `CRYPTO_DEPOSIT` issuer only
  ever *observes* money that is already there, and something has to have put it
  there.
- **The engine's own loop was spending the retry budget** the reconciler exists to
  demonstrate. With `--inject`, the demo now takes one step and hands over, which is
  also how the two would divide the work in production.

### 9.16 The worker, and why new work and failing work are paced differently

Phase 5 built a watcher that polls when asked and an engine that steps when asked,
and for a while nothing asked outside a demo script. `funding/worker.py` is the
caller: **poll every watched address, drive what is new, reconcile the rest.**

`deposit_routes` is the list of addresses to poll, which falls out of what a route
*is*: it exists because somebody was told to send money to that address. Anything
registered has to be watched and nothing else needs to be, and re-reading the table
every pass means a route registered while the worker runs is picked up without a
restart.

**Two stepping paths, not one**, and this is the decision worth recording. The
reconciler is defined by state *age* (SPEC.md §5.3: "state age > threshold") and
that threshold is minutes, because its job is to notice silence. Driving new work
through it would make a confirmed deposit wait minutes for its bridge order. But
driving *everything* promptly is worse: an intent in `BRIDGING` behind a 30-second
bridge would be re-polled on every pass, and since a "not delivered yet" answer is
a retry, a five-second loop would exhaust a five-retry budget in half a minute and
fail a transfer that was about to succeed.

So: **first attempts are prompt, retries are paced.** The filter is
`retry_count == 0` — the moment an intent has retried once, its schedule becomes the
reconciler's business. There is still a small `first_attempt_after_seconds` delay
(default 3s), because whichever process created the intent is probably about to step
it and a worker racing that only contends on the row lock.

The two paths overlap on one case — an un-retried intent old enough to look stuck —
so the driver reports what it touched and `reconcile(skip=…)` honours it. Without
that, one pass would poll the same bridge twice and count the second answer as a
retry, which is the exact failure the split exists to avoid.

**An RPC failure does not stop the loop.** The node is asked again next pass, and
because the cursor is only advanced for what was fully accounted for, nothing is
lost meanwhile. Verified against a live 429 from the public devnet endpoint, which
is also where `error_rate_limited.json` came from.

## 10. Decisions recorded in phase 6

Phase 6 replaces the simulator with a real protocol behind the same two calls.
The simulator stays — SPEC.md §5.2 keeps it as the default so a recorded demo never
depends on a third party's testnet — so this phase is a second implementation of an
interface phase 5 designed without one, which makes it the test of §9.2.

### 10.1 Wormhole, because deBridge has no testnet at all

SPEC.md §5.2 names `debridge.py` first and asks for research at build time. The
research disqualifies it in one line, from deBridge's own FAQ:

> Which testnets are supported? deBridge does not support testnets. The protocol
> relies on extensive on-chain infrastructure across multiple chains, making testnet
> maintenance impractical.

Confirmed against the live API rather than only the docs:
`GET https://dln.debridge.finance/v1.0/supported-chains-info` returns mainnet chain
ids only (Solana `7565164`, BSC `56`), and no testnet API host exists. This is not an
oversight to work around — it follows from what DLN *is*. A solver-filled network
needs market makers holding real inventory on both chains, and nobody funds inventory
on a testnet. **Any intent-based route is unavailable for the same reason**, which
rules out Mayan and LiFi's solver routes too.

Wormhole's Wrapped Token Transfers is lock-and-mint: guardians observe and sign, and
the destination leg is a contract call anyone can make. Nothing about it needs a
funded third party, so it is the kind of protocol that *can* exist on a testnet — and
it does.

**The docs say the route does not exist. The chain says it does.** The supported-
networks table has a **Devnet** column marked ❌ for Solana, and read literally that
is the answer to this phase's question. It means Wormhole's own local Tilt devnet,
not Solana devnet; Wormhole's **Testnet** for Solana *is* Solana devnet. Established
by probing rather than by reading:

| Probe | Result |
| --- | --- |
| Core + Token Bridge programs from the docs' Testnet tab, on `api.devnet.solana.com` | both exist, `executable = true`, owned by the BPF upgradeable loader |
| the same two on `api.testnet.solana.com` | one is a plain system account, the other missing entirely |
| Token Bridge emitter, derived locally as `find_program_address([b"emitter"], DZnkk…)` | `4yttKWzRoNYS2HekxDfcZYmfQqnVWpKiJ8eydYRuFRgs`, which is exactly the `emitterNativeAddr` testnet Wormholescan reports for chain 1 |
| guardian liveness on that emitter | signed VAA sequence 56910 at 2026-07-25T01:00:38Z — hours old, not abandoned |
| `wrappedAsset(1, <devnet USDC mint>)` on the BSC testnet Token Bridge | `0x51a3cc54ea30da607974c5d07b8502599801ac08` — non-zero, so the asset is **already attested** and this phase does not have to run `attest_token` / `create_wrapped` |

The two derived facts are the ones worth keeping: an emitter address that matches
what the explorer independently reports proves the *program* in the docs is the
program the guardians are watching, and a non-zero `wrappedAsset` proves the
destination side of the route is already open. Both took one RPC call. The habit is
§8.10's, learned the expensive way from Stripe: **the network is the documentation.**

**The route is Solana devnet → BSC testnet**, which SPEC.md §5.2 permits explicitly
("implement the real adapter against any available Solana-devnet→EVM-testnet route
to prove the mechanics, and document the mainnet route choice"). Fees and delivery
mechanics are in §10.2.

**The mainnet route choice, and a §5.2 assumption that has expired.** §5.2 predicts
the real product bridges Solana → Gnosis Chain because that is where a Gnosis Pay
card's Safe lives, and offers deBridge and LiFi as the candidates. Neither serves it
today, and the third does not either:

- **deBridge** — no Gnosis Chain in the supported-chains list at all any more. §5.2's
  "deBridge lists Gnosis support" was true when the SPEC was written; it is stale.
- **Wormhole** — no Gnosis deployment on *any* network. Gnosis has no Wormhole chain
  id, and the Executor capability endpoints list no chain 25 on mainnet or testnet.
- **LiFi**, the aggregator §5.2 names as the fallback — `GET /v1/chains` lists Gnosis
  (100) and **no Solana at all**, and a mainnet quote request for Solana USDC → Gnosis
  USDC answers `1002 No available quotes for the requested transfer`.

So the honest mainnet answer is that Solana → Gnosis Chain is **a two-hop route**:
bridge Solana → an EVM chain that both sides support (Wormhole or CCTP into Polygon
or Ethereum), then Gnosis's own canonical omnibridge into Gnosis Chain. That is a
different reconciliation problem from what this phase builds — two independent legs,
each with its own reference and its own failure mode, and an intermediate balance
that belongs to nobody while it waits. `BridgeProvider` can hold it (a composite
implementation whose `bridge_ref` names both legs), but it is not what a testnet can
demonstrate, and inventing it here would be the "shaped like the first
implementation" mistake §9.2 exists to avoid.

### 10.2 What a lock-and-mint route changes about reconciliation

SPEC.md §11 asks what a third-party bridge implies for reconciliation. The simulator
answered the easy half. The real one changes five things, and they are not the five
§9.2 anticipated.

**1. The destination leg is ours, so `status()` has to act.** With a solver-filled
route, `BRIDGING → BRIDGED` happens whether or not this service is running: someone
else is paid to complete it. Lock-and-mint has no such someone. The VAA sits signed
and unredeemed until a transaction submits it to BSC testnet, which means **a
transfer can be permanently stuck in a state that is otherwise indistinguishable from
healthy in-flight.** The reconciler's mandate (SPEC.md §5.3, "re-query the relevant
system") is not enough on its own — re-querying a stuck transfer returns "still
pending" forever. So redemption happens *inside* the adapter's `status()`: if the
guardians have signed and the destination has not redeemed, submit it. `status()` is
therefore not a pure read for this provider, which is a real departure from the
simulator, and it is deliberate: §9.2 chose two calls on the ground that anything
richer "belongs inside an implementation until a caller needs it", and this is that
case. The engine and the reconciler are unchanged.

**2. Money in flight cannot be given up on.** The simulator's `FAILED` means nothing
moved. Here, once the source transaction lands, USDC is locked in the Token Bridge's
custody account and **the signed VAA is the only key**. It does not expire. So a
retry cap that ends in `FAILED_BRIDGE` is safe *before* the lock and dangerous after
it: hitting the cap must not be read as "the money is gone", it means "nobody has
finished this yet". The consequence for this phase is that the VAA identity is
ledgered on every attempt, so a stalled transfer can always be completed by hand from
the ledger alone.

**3. Idempotency has to be constructed, because the protocol has none.** There is no
`Idempotency-Key` here and no `order_ref` the protocol understands. A duplicated
`submit` does not return the first transfer — it locks a second amount and produces a
second VAA, and the engine retries `submit` precisely when it cannot tell whether the
first call landed. So the adapter derives the transfer's Solana **message account
deterministically from `order_ref`** (the funding intent id): a second attempt tries
to create an account that already exists, fails on-chain, and the adapter reads the
existing sequence back instead of duplicating. This is the sharpest contrast in the
phase — the simulator gets idempotency for free from a dict keyed on `order_ref`, and
`BridgeOrder.order_ref` was documented in phase 5 as "our idempotency key" on the
assumption that a provider would honour it. One does not.

**4. `amount_out == amount_in`, and the fee model §9.2 built is not exercised.** WTT
charges no protocol fee, and Wormhole normalizes amounts to at most 8 decimals, which
a 6-decimal USDC survives without truncation. The costs are gas on two chains, paid
in SOL and BNB by us, entirely outside the transferred amount. §9.2's separate
`amount_in` / `amount_out` remains right — a solver route *does* deduct from the
amount, which is why the simulator can charge `SIMULATED_BRIDGE_FEE_MINOR` — but this
route is not what proves it. Worth stating plainly rather than letting the field look
exercised: **the real adapter's fee path is the trivial one.**

**5. Finality, not the bridge, sets the clock — and it sets the reconciler's
threshold.** `BRIDGING` here spans Solana finality (guardians will not sign sooner),
guardian quorum, and then BSC inclusion of our redemption. The reconciler's job is to
notice *silence*, so `RECONCILER_STUCK_AFTER_SECONDS` has to exceed that whole span
or a healthy transfer gets treated as stuck on every pass — the §9.16 failure mode,
arriving through a different door. The simulator's `SIMULATED_BRIDGE_LATENCY_SECONDS`
existed to make this tunable before it was real; phase 6 is where the number has to
match a protocol instead of a config knob.

### 10.3 A hand-built instruction, and why that is defensible

Wormhole's SDK is TypeScript. There is no Python binding for its Solana programs,
so `transfer_native` is built here byte by byte: 55 bytes of little-endian Borsh
and seventeen accounts in a fixed order. That is normally a bad idea, and what
makes it acceptable is where the layout came from.

**It came off a transaction the chain accepted.** The recorded VAA
(`tests/fixtures/wormhole/`) has a `txHash`, and that transaction is a real
`transfer_native` on the exact program this adapter calls. Recorded in `json`
encoding rather than `jsonParsed`, because the parsed form drops the per-instruction
account *indices* and those indices are the ABI. Three tests then close the loop:

- `transfer_native_data` rebuilt from the recorded instruction's own field values
  is **byte-for-byte identical** to it;
- the seventeen derived accounts are the seventeen the transaction used, in order;
- every writable flag matches.

All eight PDA derivations reproduced the recording on the first attempt, which is
weak evidence that the seeds were guessed well and strong evidence that they are
right — a wrong seed produces a valid-looking address that exists nowhere, and the
chain reports that as a program error with nothing in it about seeds.

**Two things the recording taught that no documentation mentioned.** A transfer
needs an SPL `approve` to the `authority_signer` PDA first, for exactly the amount;
without it the transfer reverts and nothing in the instruction layout hints that the
approval is missing. And the instruction's amount is the *token's* — `625600000` at
nine decimals — while the VAA carries `62560000` at eight. That is Wormhole's
normalization, visible in one pair of recorded numbers instead of taken on trust.
USDC has six decimals, so nothing this pipeline sends is scaled, and
`normalized_amount` exists so the day that changes the truncation is a named thing
rather than a surprise in a reconciliation report.

**One flag in the recording is a red herring**, and it is documented as such in the
code: the source token account is a signer there, because that transfer wrapped SOL
through a temporary account whose keypair signed its own creation. An associated
token account signs nothing. Reading flags off a recording needs that caution
generally — a transaction's flags are the *union* across its instructions, so an
account can look writable because a different instruction wrote it. Being generous
with `mut` is safe on a non-program account; being stingy aborts the transaction.

### 10.4 Idempotency built out of an account address

`BridgeOrder.order_ref` was documented in phase 5 as "our idempotency key… on the
assumption that a provider would honour it". Wormhole does not have the concept. A
duplicate `submit` locks a second amount and produces a second VAA, and the engine
retries `submit` precisely when it cannot tell whether the first call landed — which
is the most dangerous shape a missing idempotency key can take.

The mechanism is the **message account**. Wormhole's core bridge requires the
account that will hold the posted message to *sign*, which makes it a keypair rather
than a PDA — and a keypair this service chooses. So it is derived from the order
reference: before sending, the adapter reads that address, and if an account is
already there this order was already submitted and its sequence is read back instead.
The same read closes the window between sending and being told the send worked,
because it asks the chain rather than remembering what this process did.

**Derived from a signature, not from a hash of the order id.** The address has to be
reproducible from the order alone — that is the whole mechanism — *and* unguessable
by anyone else, because an outsider who could predict it could create the account
first and make the transfer permanently unsubmittable. A signature over a
domain-separated payload satisfies both without introducing a second secret to
configure: Ed25519 signatures are deterministic by construction (RFC 8032), so the
same signer over the same bytes always produces the same seed, and only the key
holder can produce it. It also keeps the derivation inside `TransactionSigner`, so
phase 9's custody service needs no change here — **provided it signs
deterministically.** One that randomizes signatures would break idempotency rather
than correctness, and that is worth knowing before wiring one up.

Reading the sequence back needs the posted-message layout, so every offset in it was
checked against two independent things: the VAA the guardians signed (sequence,
emitter, consistency level, payload) and the instruction that created it (nonce). An
account at the derived address that is *not* a posted message is refused rather than
parsed — it means somebody else created it, and a sequence read out of unrelated
bytes becomes a `bridge_ref` naming a stranger's transfer.

### 10.5 `status` acts, and `COMPLETED` means the destination says so

The interface has two calls, and with a lock-and-mint bridge one of them has to do
something. `status` fetches the VAA and, if the guardians have signed and the
destination has not redeemed, submits the redemption. A `status` that only observed
would leave every transfer pending forever (§10.2, point 1). §9.2 chose two calls on
the grounds that anything richer "belongs inside an implementation until a caller
needs it" — this is that case, and the engine and the reconciler are unchanged, which
is the strongest evidence available that §9.2's shape was right.

What the three states mean here, precisely, because the money is locked while this
is being decided:

| State | Means | Does *not* mean |
| --- | --- | --- |
| `PENDING` | sent-not-signed, signed-not-redeemed, or redemption-in-flight | nothing has happened |
| `COMPLETED` | the destination Token Bridge answers `isTransferCompleted` | we submitted a redemption, or a receipt looked good |
| `FAILED` | the chain reverted and will repeat that | the money is gone |

`isTransferCompleted` is asked **first, always**. It is the authoritative answer to
"did the money arrive", it costs nothing, and it is what makes the operation
restartable: a redemption submitted and then lost to a crash or a redeploy is
discovered by asking rather than by re-sending. It is also the reason a retry does
not pay gas to fail on a transfer somebody else already delivered.

The digest it is asked with is `keccak256(keccak256(body))` — a **double** keccak.
Getting that wrong would make every delivered transfer read as undelivered and every
redemption look like a duplicate, so it has two independent confirmations behind it:
Wormhole's `Messages.sol`, which computes it under a comment reading *"SECURITY: Do
not change the way the hash of a VM is computed!"*, and Wormholescan's own `digest`
field for a real testnet VAA, which the test asserts equality with.

Gas is estimated before anything is signed, which buys a free pre-flight: a
redemption that cannot succeed reverts there, with the chain's own words, without
spending. That is where "already completed" and "invalid emitter" surface, and the
reason is carried into the ledger rather than swallowed — because per §10.2 point 2,
a refusal is not the same as lost money, and the VAA remains the key to it.

### 10.6 What phase 6 could not verify

Recorded plainly, in the spirit of §8.9.

**No real transfer has been sent.** The route is verified against both chains —
`scripts/demo_phase6.py` does it in six read-only calls, and it passes — but moving
devnet USDC through it needs two funded testnet keys that this environment does not
have: `SOLANA_DEPOSIT_KEYPAIR` with devnet USDC and SOL, and
`EVM_REDEEMER_PRIVATE_KEY` with test BNB. `--transfer` says which is missing and
stops rather than half-running. What that leaves unverified is specifically the
parts a fixture cannot stand in for: that the built transaction is accepted by the
program (as opposed to being byte-identical to one that was), that the guardians
sign our message, and that `completeTransfer` succeeds against a VAA of ours.

**The "already completed" revert is a prediction.** `RedemptionRefused` is exercised
against a real revert — *VM version incompatible*, from the real contract, recorded
— but the specific revert a duplicate redemption produces has not been seen. Newer
Wormhole releases use a custom error (`TransferAlreadyCompleted()`) where older ones
used a string, and the deployed BSC testnet version is unknown. This is why the
adapter branches on `isTransferCompleted` rather than on a revert reason: the check
is authoritative and the string is not.

**Nothing has been run against a second guardian set.** Testnet signs with one
guardian; mainnet has nineteen. The VAA parser handles the count generically and a
test pins the one-signature case, but a mainnet-shaped VAA has never been through it.

**Finality timing is unmeasured.** §10.2 point 5 says
`RECONCILER_STUCK_AFTER_SECONDS` has to exceed Solana finality plus guardian quorum
plus BSC inclusion, and the current default (120s) is a guess that was never a
measurement. The first real transfer is what turns it into one.
