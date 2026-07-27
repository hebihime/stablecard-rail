"""Can this provider reveal a card at all?

Asked before a token is minted, so a provider that cannot answer says so while the
client is still asking rather than after it has a credential and a countdown running.

**The answer is read off the interface, not off a flag.** `CardIssuerAdapter.reveal`
has a default that raises `RevealUnsupported`, so "does this adapter override it" is
exactly the same question, with one source of truth instead of two. The alternative —
a `can_reveal: bool` class attribute — is a second thing to keep in sync, and the
failure mode is silent: an adapter that implements `reveal` and forgets the flag
looks unsupported forever.

The cost of introspection is that it can drift from the behaviour it describes, so
`tests/test_reveal_api.py` calls `reveal` on every registered provider and asserts
the two answers agree. That test is what makes this file trustworthy rather than
merely clever.
"""

from __future__ import annotations

from app.issuers.base import CardIssuerAdapter

__all__ = ["supports_reveal"]


def supports_reveal(adapter: CardIssuerAdapter) -> bool:
    """True when this adapter implements a reveal of its own.

    Walks the MRO by comparing functions rather than looking in `vars(type(adapter))`
    directly, so an adapter that inherits `reveal` from an intermediate base class —
    a shared "two providers of the same shape" parent, which nothing here has yet —
    is still reported as supporting it.
    """
    return type(adapter).reveal is not CardIssuerAdapter.reveal
