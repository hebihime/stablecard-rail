"""Webhook receipt: verification, dedup, dispatch (SPEC.md §4).

    receiver.py   the pipeline: verify -> dedup -> parse -> ledger -> dispatch
    dedup.py      Redis SETNX claims, backed by the ledger's unique index
    bus.py        EventBus interface + Redis Streams implementation
    dispatch.py   handler subscriptions, dispatch, and the retry drain
    retry.py      the backoff queue and the dead-letter writer
    models.py     webhook_dead_letters

Nothing in here imports an issuer adapter: providers are resolved through the
registry and every payload arrives as a normalized `CardEvent`, which is what makes
"a new issuer changes no webhook code" true.
"""

from __future__ import annotations
