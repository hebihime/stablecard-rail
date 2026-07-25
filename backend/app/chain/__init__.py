"""Everything this service does on a chain (SPEC.md §5.2, §8).

Three concerns, deliberately separate: watching a chain for deposits, moving
value across one (`bridge/`), and signing (`signer.py`). Nothing here knows about
funding intents, the ledger or issuers — `funding/` composes them. That keeps the
watcher testable against recorded RPC responses and the bridge swappable for the
real one in phase 6.
"""
