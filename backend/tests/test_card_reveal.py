"""Reveal at the issuer interface (SPEC.md §9.2, phase 8).

`docs/ARCHITECTURE.md` §7.7 left this method off the interface on purpose: the
simulator has implemented Gnosis Pay's PSE since the refactor, and adding a
`reveal` for one provider with no caller would have meant guessing at a shape every
other adapter then inherits. Phase 8 is the caller, so this is where the guess stops
being one.

**What comes back is not a PAN.** Upstream, a PSE ephemeral token is handed to an
SDK that renders card data in an iframe the partner's own client cannot read, and
the partner's *backend* never sees the number at any point. Modelling that faithfully
means the reveal DTO has nowhere to put a PAN — which is asserted here, not merely
documented, because "we happen not to populate it" and "it cannot be populated" fail
differently the day someone adds a fourth adapter.

The two-token structure is the other thing worth pinning. Our backend mints a
short-lived single-use token of its own (`app/reveal/`), and the *provider's*
ephemeral token is minted and redeemed entirely inside the adapter. So the provider
credential never crosses our API boundary, and a client holding our token cannot
call Gnosis Pay with it.
"""

from __future__ import annotations

import pytest

from app.issuers.base import (
    CardIssuerAdapter,
    CardNotFoundError,
    RevealedCard,
    RevealUnsupported,
)
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from app.issuers.lithic.adapter import LithicAdapter
from app.issuers.stripe_issuing.adapter import StripeIssuingAdapter
from tests.support import StubIssuerAdapter
from tests.test_gnosis_pay_mock import build_adapter, make_card

# ------------------------------------------------------- the interface ----


def test_revealing_is_optional_for_adapters() -> None:
    """Same shape as `respond_to_challenge`, for the same reason.

    A reveal needs a provider-side secure-rendering path, and two of the three
    providers here have none we are willing to use: Lithic and Stripe would both
    hand over a sandbox PAN if asked, and taking it would put card data through a
    backend whose entire design says it never holds any.
    """
    assert "reveal" not in CardIssuerAdapter.__abstractmethods__


async def test_the_default_reveal_refuses_and_names_the_provider() -> None:
    class BareAdapter(StubIssuerAdapter):
        provider_id = "bare_provider"

    with pytest.raises(RevealUnsupported) as raised:
        await BareAdapter().reveal("card_1")

    assert "bare_provider" == raised.value.provider_id
    # Not retryable: waiting does not give a provider a reveal path it lacks.
    assert raised.value.retryable is False


def test_the_reveal_dto_cannot_hold_card_number_material() -> None:
    # The invariant the whole phase-8 reveal design rests on. `Card` is already
    # covered by `test_issuer_interface.py`; this is the DTO that would be the
    # tempting place to break it.
    forbidden = {"pan", "cvv", "cvc", "number", "secret", "track_data"}
    assert not forbidden & set(RevealedCard.model_fields)


@pytest.mark.parametrize("adapter_type", [LithicAdapter, StripeIssuingAdapter])
def test_the_two_real_providers_inherit_the_refusal(
    adapter_type: type[CardIssuerAdapter],
) -> None:
    # Neither overrides it, so neither can start returning a PAN without this
    # failing first. Deliberate: a sandbox PAN is still a PAN.
    assert "reveal" not in vars(adapter_type)


# --------------------------------------------------------- the provider ----


@pytest.fixture
def adapter() -> GnosisPayMockAdapter:
    return build_adapter()


async def test_a_reveal_returns_what_pse_renders(adapter: GnosisPayMockAdapter) -> None:
    card_id = await make_card(adapter)

    revealed = await adapter.reveal(card_id)

    card = await adapter.get_card(card_id)
    assert revealed.card_id == card_id
    assert revealed.provider_id == adapter.provider_id
    assert revealed.last_four == card.last_four
    assert revealed.exp_month == card.exp_month
    assert revealed.exp_year == card.exp_year
    # The field that carries the honesty: it names where the number actually is.
    assert revealed.rendered_in == "pse-iframe"


async def test_the_providers_ephemeral_token_never_leaves_the_adapter(
    adapter: GnosisPayMockAdapter,
) -> None:
    """Minted and redeemed in one call, so there is nothing to leak upward.

    Upstream the mint is an mTLS call the client could not make anyway. Returning
    the token would invite a client to try, and would hand it a credential with a
    60-second life and a provider's name on it.
    """
    card_id = await make_card(adapter)

    revealed = await adapter.reveal(card_id)

    serialized = revealed.model_dump_json()
    for token in adapter.simulator.spent_ephemeral_tokens:
        assert token not in serialized


async def test_each_reveal_mints_its_own_token_so_a_second_one_works(
    adapter: GnosisPayMockAdapter,
) -> None:
    # Single use is a property of the *token*, not of the card. A cardholder who
    # closes the screen and opens it again is doing something entirely normal.
    card_id = await make_card(adapter)

    first = await adapter.reveal(card_id)
    second = await adapter.reveal(card_id)

    assert first == second
    assert len(adapter.simulator.spent_ephemeral_tokens) == 2


async def test_revealing_an_unknown_card_is_not_found(adapter: GnosisPayMockAdapter) -> None:
    with pytest.raises(CardNotFoundError):
        await adapter.reveal("card_nope")
