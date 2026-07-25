# Stripe Issuing fixtures

Three kinds of file live here, and the difference is the difference between evidence
and a reading of documentation. Do not blur them.

Regenerate with `python scripts/record_stripe_fixtures.py`, which needs
`STRIPE_ISSUING_API_KEY`, Issuing activated, and a funded Issuing balance. **The test
suite never calls Stripe** (SPEC.md §10) — it replays these.

## 1. Recorded — the provider's own words

Captured verbatim from a live test-mode account. These are evidence: Stripe really
sent this shape. Where one of these disagrees with the adapter, **the fixture is right
and the adapter is what changes.**

`cardholder_created` · `card_created` · `card_created_replayed` · `card_read_back` ·
`card_activated` · `card_frozen` · `card_unfrozen` · `card_funded` · `card_canceled` ·
`authorization_created` · `authorization_captured` · `authorization_reversed` ·
`authorization_pending` · `authorization_declined` · `transactions_all` ·
`transactions_page_1` · `transactions_page_2` · `authorizations_pending` ·
`error_resource_missing` · `error_malformed_id` · `error_missing_cardholder` ·
`error_card_canceled` · `error_idempotency_key_reused` · `event_type_census` ·
`event_card_created` · `event_card_updated` · `event_authorization_created` ·
`event_authorization_updated` · `event_transaction_created` ·
`event_cardholder_created`

`event_*` are real Event envelopes from `GET /v1/events`, and **an Event envelope is
byte-for-byte what a webhook delivers** — which is how the webhook mappings get tested
against reality without a public URL or an endpoint secret.

`event_type_census` is the distribution of `type` over the last hundred events on the
account: the provider's own vocabulary, not a payload. Two tests in
`test_stripe_webhooks.py` read it — one asserting every event name the adapter claims to
map is a name Stripe actually sends (the event names in `adapter.py` are hand-typed
strings, and a mapping keyed on a name nobody sends maps nothing, silently), one
asserting every family Stripe *does* send is either mapped or on a deliberately-ignored
list.

## 2. Derived — a recorded card with one stated mutation

Card states Stripe will not hand over on demand, produced by `derive_variants()` from
the recorded `card_activated`: real field names, real nullability, one documented
change each. Asking Stripe for these would add cards and round trips to prove
something about *our* reading of a spending limit rather than about their API.

| File | Mutation |
| --- | --- |
| `card_with_limit` | one `all_time` limit, unfunded |
| `card_unlimited` | `spending_limits: []` — Stripe's way of saying unlimited |
| `card_monthly_limit` | the limit on `monthly`, which resets and so is not a balance |
| `card_limited_to_zero` | `all_time` limit of `0`, which really does mean zero |

## 3. Authored — hand-written from Stripe's published examples

Still a reading of documentation, kept because the recording walk never produced them.
Every test using one says so in a comment.

| File | Why the walk never produced it |
| --- | --- |
| `event_authorization_request` | needs a real-time authorization endpoint — verified unproducible, see below |
| `event_authorization_expired` | this account's API version (`2026-06-24.dahlia`) spells a lapse `reversed` |
| `event_authorization_closed` | the capture path closed via the transaction event instead |
| `event_transaction_capture`, `event_transaction_refund`, `event_transaction_unknown_type` | no refund or refund-reversal occurred |
| `event_card_activated`, `event_card_unknown_status` | superseded by `event_card_updated`; the unknown status cannot be provoked |
| `event_dispute_created` | disputes need a settled transaction and a filing |
| `event_token_created` | no wallet was provisioned |
| `event_without_an_object` | Stripe never sends one; it is a defensive case |
| `error_rate_limited` | not worth provoking deliberately |
| `authorizations_none` | an empty list, trivially correct |

## What the recording could not settle, and how it was settled instead

**The signature scheme** (`docs/ARCHITECTURE.md` §8.2) is not in these files, because
no API response contains it: the question is how Stripe signs an *outbound* delivery.
It was confirmed on 2026-07-26 with `stripe listen` — genuine deliveries verified and
were ledgered, and returned 401 against a wrong secret. It is not pinned as a fixture
here, and will not be: verifying a captured delivery requires the endpoint secret, and
committing a webhook secret to a tracked file breaks a hard rule.

**`event_authorization_request.json` cannot be recorded on this account.** Verified,
not assumed: `stripe trigger issuing_authorization.request` produces only the
downstream `issuing_authorization.created`, and `GET /v1/events?type=issuing_
authorization.request` returns nothing. Stripe creates that event only for an account
with a real-time authorization endpoint configured, which this pipeline deliberately
does not serve (§8.7). So it stays authored.

## Redaction

`number` and `cvc` are replaced with obvious synthetic values, and the Stripe account
id with `acct_stablecard000001`. Test card numbers are not credentials, but
card-number-shaped material and an account identifier have no business in a tracked
repo. The cardholder is deliberately "Ada Lovelace" at `example.test` with a
placeholder address — see `SANDBOX_ADDRESS` in the adapter for why placeholders rather
than real identity data.
