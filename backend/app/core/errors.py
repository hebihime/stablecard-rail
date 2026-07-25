"""Failures that came from outside this process, and whether to try again.

Two subsystems talk to systems we do not control — `issuers/` and `chain/` — and
the funding engine drives both. It catches their failures without knowing which
provider or which bridge raised, so the one thing it needs from every such
failure is the same: **could the identical call succeed if it were made again?**

That question is what `ExternalError` carries. It is a marker and not an
instruction: *how* to retry — how often, how far apart, and what an exhausted
budget turns into — is the engine's policy, and it lives next to `retry_count`
and the transition table (docs/ARCHITECTURE.md §9.1).
"""

from __future__ import annotations

__all__ = ["ExternalError"]


class ExternalError(Exception):
    """A failure originating outside this process.

    Not retryable is the default, deliberately. A wrongly-retried permanent
    refusal spends the retry budget a genuinely transient failure needed and
    delays the `FAILED_*` an operator has to see; a wrongly-failed transient
    error costs one re-opened intent. The cheaper mistake is the default.
    """

    #: A class attribute, so every subclass has it whether or not it sets one,
    #: and a failure type that is *always* transient can say so once by
    #: overriding it rather than at every raise site.
    retryable: bool = False

    def __init__(self, *args: object, retryable: bool | None = None) -> None:
        super().__init__(*args)
        # `None` rather than `False`: an explicit default here would silently
        # overwrite a subclass's own, which is the point of the class attribute.
        if retryable is not None:
            self.retryable = retryable
