# Stripe Issuing fixtures

**These are documentation-derived, not recorded.** Every file here was written by
hand from the example objects published in Stripe's API reference
(`docs.stripe.com/api/issuing/...`), keeping their field names, types, enum
spellings and sign conventions verbatim and changing only the identifiers,
amounts and timestamps.

That is a weaker kind of evidence than `tests/fixtures/lithic/`, which
`scripts/record_lithic_fixtures.py` captured from the live sandbox. The
difference matters and is recorded in `docs/ARCHITECTURE.md` §8.7:

- A recorded fixture proves the provider sent that shape. These prove only that
  we read the documented shape correctly.
- Anything Stripe sends that their docs omit — an extra field, a nullable that is
  never actually null, a status enum with a member the reference does not list —
  is invisible to this suite.

So the adapter is deliberately strict about what it requires and lenient about
what it ignores, and every "we could not map this" path is an `UNMAPPED` event or
a loud `IssuerError` rather than a guess.

Re-recording these against a real test-mode account is the first thing to do once
`STRIPE_ISSUING_API_KEY` exists. Phase 3's recorder is the model to copy.

Identifiers use Stripe's documented prefixes: `ich_` cardholder, `ic_` card,
`iauth_` authorization, `ipi_` transaction, `evt_` event.
