"""The 3DS / OTP service (SPEC.md §6).

    three_ds_challenge webhook -> Redis (short TTL) -> GET /otp/pending + /ws/otp
                                                   -> approve/decline

A package of its own rather than a corner of `funding/` or `webhooks/`, and that is
an addition to the tree in SPEC.md §1 — which names an `api/` route for OTP but no
service to sit behind it. The reason it is not part of either neighbour: `webhooks/`
owns *arrival* and must stay ignorant of what any particular event means, while
`funding/` owns money and a challenge moves none. What this package owns is one
short-lived secret and the two ways an app can be told about it.

Three things about it are unlike everything else in this service, and each one is
recorded where it is enforced rather than only here:

* **Nothing it holds is durable.** The code lives in Redis under a TTL and nowhere
  else; `CardEvent.otp_code` is excluded from every serializer so that no sink can
  quietly acquire a copy (docs/ARCHITECTURE.md §11.2).
* **Push is pub/sub, not the `EventBus`.** The stream is a replayable log, which is
  the wrong shape for a value that must stop existing. Redis pub/sub has no
  retention, which here is the feature (§11.5).
* **Polling is the contract and push is the courtesy.** SPEC.md §6.3 puts it that
  way round, so `GET /otp/pending` is authoritative and a dropped WebSocket costs
  latency rather than correctness.
"""
