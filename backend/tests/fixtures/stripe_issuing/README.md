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
`error_card_canceled` · `error_idempotency_key_reused` · `events_all` ·
`event_card_created` · `event_card_updated` · `event_authorization_created` ·
`event_authorization_updated` · `event_transaction_created` ·
`event_cardholder_created`

`event_*` are real Event envelopes from `GET /v1/events`, and **an Event envelope is
byte-for-byte what a webhook delivers** — which is how the webhook mappings get tested
against reality without a public URL or an endpoint secret.

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
| `event_authorization_request` | only sent to an account with a real-time authorization endpoint |
| `event_authorization_expired` | this account's API version (`2026-06-24.dahlia`) spells a lapse `reversed` |
| `event_authorization_closed` | the capture path closed via the transaction event instead |
| `event_transaction_capture`, `event_transaction_refund`, `event_transaction_unknown_type` | no refund or refund-reversal occurred |
| `event_card_activated`, `event_card_unknown_status` | superseded by `event_card_updated`; the unknown status cannot be provoked |
| `event_dispute_created` | disputes need a settled transaction and a filing |
| `event_token_created` | no wallet was provisioned |
| `event_without_an_object` | Stripe never sends one; it is a defensive case |
| `error_rate_limited` | not worth provoking deliberately |
| `authorizations_none` | an empty list, trivially correct |

## What the recording still does not settle

**The signature scheme** (`docs/ARCHITECTURE.md` §8.2). Verifying that Stripe keys its
HMAC on the whole `whsec_…` string, prefix included, needs an actual inbound delivery
and `STRIPE_ISSUING_WEBHOOK_SECRET` — a Dashboard endpoint or `stripe listen`. Until
then `tests/test_stripe_signing.py` proves we are self-consistent, not that Stripe
agrees.

## Redaction

`number` and `cvc` are replaced with obvious synthetic values, and the Stripe account
id with `acct_stablecard000001`. Test card numbers are not credentials, but
card-number-shaped material and an account identifier have no business in a tracked
repo. The cardholder is deliberately "Ada Lovelace" at `example.test` with a
placeholder address — see `SANDBOX_ADDRESS` in the adapter for why placeholders rather
than real identity data.
