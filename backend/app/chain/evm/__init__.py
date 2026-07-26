"""Reading and writing an EVM chain, for the leg a lock-and-mint bridge leaves to us.

Nothing here knows what Wormhole is. It is a JSON-RPC client, a signer, and just
enough ABI encoding to call one function — the same division `chain/rpc.py` and
`chain/signer.py` drew on the Solana side, and for the same reason: a custody
service (phase 9) signs bytes and has no opinion about which protocol asked.
"""
