# Payloads built from Lithic's published schema, not recorded

Everything in `tests/fixtures/lithic/` is a verbatim recording from the sandbox,
written by `scripts/record_lithic_fixtures.py` — **except these two**.

A 3DS authentication challenge and a dispute update cannot be triggered from the
sandbox: there is no `POST /v1/simulate/...` for either, and both need a real network
or a real cardholder filing a claim. So these payloads are constructed from Lithic's
own OpenAPI `webhooks` definitions (the `three_ds_authentication.challenge` and
`dispute.updated` entries, available as JSON under
`https://docs.lithic.com/reference/postcards.md`), keeping every required field and
the field names and types exactly as published.

That makes them weaker evidence than the recordings, and they are kept apart so the
difference is visible rather than assumed. What they are good for is pinning the
*mapping* — which normalized `CardEventType` each provider event becomes, and which
fields are read out of it. What they cannot prove is that Lithic's live payload looks
like its published schema.

Two consequences are worth knowing:

- **`dispute.updated` carries no `card_token`.** It identifies a `transaction_token`.
  Resolving that to a card would mean an API call, and `parse_webhook` is pure — the
  receiver calls it again for duplicate deliveries. So a chargeback event has
  `card_id = None` and the transaction token in `raw`.
- **The 3DS payload states its amount as a decimal number plus a
  `currency_exponent`**, not as minor units. Money in this system is integer minor
  units only, so the amount is deliberately left unnormalized (`amount = None`) with
  the provider's own numbers intact in `raw`. Phase 7 can convert it exactly, via
  `Decimal`, at the point where it actually needs to show a cardholder what they are
  approving.
