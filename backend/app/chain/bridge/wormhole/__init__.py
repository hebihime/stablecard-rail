"""The real bridge (SPEC.md §5.2, phase 6): Wormhole, Solana devnet → BSC testnet.

Chosen because deBridge — which SPEC.md §5.2 names first — has no testnet and says
so plainly, and because that limitation is structural to every intent-based route:
a solver-filled network needs market makers holding inventory on both chains, and
nobody funds inventory on a testnet. Wormhole's Wrapped Token Transfers is
lock-and-mint, so it needs no funded third party and can exist on a testnet. It
does — docs/ARCHITECTURE.md §10.1 records how that was established, and why their
docs' "Devnet" column is not the answer to the question it looks like it answers.

What follows from lock-and-mint is that **the destination leg is ours**. Guardians
sign, and then a transaction has to submit that signature to BSC testnet or the
transfer sits complete-but-undelivered forever. §10.2 records the five things that
changes about reconciliation; the one to keep in mind while reading this package is
that `status()` therefore *acts* rather than only observing.

The simulator stays the default (SPEC.md §5.2), so nothing here is on the path of
a recorded demo.
"""
