"""Provider lookup by `provider_id` (SPEC.md §3.1).

This module plus `base.py` is the entire surface the rest of the service may
touch. Adding an issuer means writing an adapter and adding one `register()` call
in `app/issuers/__init__.py` — no route, no handler, no migration, no mobile
change.

Registration is by **factory**, not instance, for two reasons: adapters read
settings (and later open HTTP clients), which should not happen at import time;
and the first `get_adapter()` call memoizes, so an adapter holding provider-side
state — the mock's in-process simulator — is a singleton per process rather than
one object per call site.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.issuers.base import CardIssuerAdapter, FundingModel

__all__ = [
    "AdapterFactory",
    "DuplicateProviderError",
    "UnknownProviderError",
    "describe",
    "get_adapter",
    "known_providers",
    "register",
    "reset_instances",
]

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[], CardIssuerAdapter]

_FACTORIES: dict[str, AdapterFactory] = {}
_INSTANCES: dict[str, CardIssuerAdapter] = {}


class UnknownProviderError(LookupError):
    """No adapter is registered for this `provider_id`.

    A distinct type so the API can answer 404 instead of leaking a 500 for what
    is really a client naming a provider that does not exist.
    """

    def __init__(self, provider_id: str) -> None:
        available = ", ".join(known_providers()) or "none registered"
        super().__init__(f"unknown issuer provider_id {provider_id!r}; registered: {available}")
        self.provider_id = provider_id


class DuplicateProviderError(RuntimeError):
    """Two adapters claimed the same `provider_id`.

    Always a bug: `provider_id` is stored on funding intents and ledger rows, so
    a collision would silently reroute money. Overriding requires `replace=True`.
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f"provider_id {provider_id!r} is already registered; "
            f"pass replace=True if that is deliberate"
        )
        self.provider_id = provider_id


def register(provider_id: str, factory: AdapterFactory, *, replace: bool = False) -> None:
    """Register an adapter factory. Called once per adapter at import time."""
    if provider_id in _FACTORIES and not replace:
        raise DuplicateProviderError(provider_id)
    _FACTORIES[provider_id] = factory
    # Drop any memoized instance so a replacement actually takes effect.
    _INSTANCES.pop(provider_id, None)
    logger.debug("registered issuer adapter provider_id=%s", provider_id)


def get_adapter(provider_id: str) -> CardIssuerAdapter:
    """Resolve an adapter, building it on first use."""
    cached = _INSTANCES.get(provider_id)
    if cached is not None:
        return cached
    factory = _FACTORIES.get(provider_id)
    if factory is None:
        raise UnknownProviderError(provider_id)
    adapter = factory()
    _INSTANCES[provider_id] = adapter
    return adapter


def known_providers() -> tuple[str, ...]:
    """Registered provider ids, sorted — safe to expose over the API."""
    return tuple(sorted(_FACTORIES))


def describe() -> tuple[tuple[str, FundingModel], ...]:
    """Provider ids with their funding model (SPEC.md §3.2's taxonomy).

    Instantiates each adapter, since `funding_model` is a property of the adapter
    rather than of its registration.
    """
    return tuple(
        (provider_id, get_adapter(provider_id).funding_model) for provider_id in known_providers()
    )


def reset_instances() -> None:
    """Forget memoized adapters, keeping registrations.

    Used by the test suite so each test gets a provider with fresh in-process
    state, and by nothing else — production builds one adapter per process.
    """
    _INSTANCES.clear()
