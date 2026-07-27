"""The card-reveal path (SPEC.md §9.2).

A package of its own for the reason `app/otp/` is one: SPEC.md §1 puts a route in
`api/` and names no service behind it, and the logic here is not a route's business.
It is also not `issuers/` — an adapter knows how *one provider* renders a card, and
this knows how *our backend* hands a client the right to ask.

**Two tokens, and keeping them apart is the design.** Gnosis Pay's PSE ephemeral
token is minted and redeemed inside a single adapter call, under mTLS a mobile client
could not perform; it never reaches this package or our API. What this package mints
is ours: short-lived, single-use, bound to one card, and worthless at the provider.
A client that steals it can see one card's last four digits, once, for sixty seconds.

Nothing here holds card-number material, because nothing in this repo does — see
`RevealedCard` in `issuers/base.py`, and docs/ARCHITECTURE.md §12.2 for why the two
providers that would happily supply a sandbox PAN are not asked for one.
"""
