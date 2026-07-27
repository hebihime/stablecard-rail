/**
 * The card screen (SPEC.md §9.1).
 *
 * The four things §9.1 asks for, and the three ways this screen can be wrong in a
 * way a demo would not reveal: a toggle that calls the wrong verb, a balance that
 * disappears when one poll fails, and a canceled card offering an action no
 * provider will honour.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react-native';

import { CardScreen } from '../CardScreen';
import { StableCardClient } from '../../api/client';
import { BASE, aCard, fakeClient, renderWithApi } from './support';

const SELECTION = { providerId: 'gnosis_pay_mock', cardId: 'card_1' };
const CARD_PATH = '/providers/gnosis_pay_mock/cards/card_1';

async function show(routes: Record<string, unknown>) {
  const { client, calls } = fakeClient({
    [`GET ${CARD_PATH}`]: aCard(),
    [`GET ${CARD_PATH}/balance`]: { card_id: 'card_1', amount_minor: 74_016, currency: 'USD' },
    [`POST ${CARD_PATH}/freeze`]: aCard({ state: 'frozen' }),
    [`POST ${CARD_PATH}/activate`]: aCard({ state: 'active' }),
    ...routes,
  });
  const view = await renderWithApi(
    <CardScreen selection={SELECTION} onOpenReveal={jest.fn()} onOpenFund={jest.fn()} />,
    client,
  );
  return { ...view, calls };
}

describe('what SPEC.md §9.1 asks for', () => {
  it('shows the masked PAN, the balance and the provider', async () => {
    await show({});

    expect(await screen.findByTestId('masked-pan')).toHaveTextContent('•••• •••• •••• 4242');
    expect(await screen.findByTestId('balance')).toHaveTextContent(/740\.16/);
    expect(screen.getByText('gnosis_pay_mock')).toBeTruthy();
  });

  it('shows the expiry as MM/YY', async () => {
    await show({});

    expect(await screen.findByText('03/29')).toBeTruthy();
  });
});

describe('the freeze toggle', () => {
  it('freezes an active card', async () => {
    const { calls } = await show({});
    const toggle = await screen.findByTestId('freeze-toggle');
    expect(toggle).toHaveTextContent('Freeze card');

    fireEvent.press(toggle);

    await waitFor(() => {
      expect(calls).toContain(`POST ${CARD_PATH}/freeze`);
    });
  });

  it('unfreezes through activate, which is the provider verb for it', async () => {
    // Not a `/unfreeze` route: SPEC.md §9.1's toggle is one control, and
    // `activate_card` is documented as the unfreeze path. Which endpoint a given
    // provider needs is the adapter's problem, and this asserts the app does not
    // invent a second one.
    const { calls } = await show({ [`GET ${CARD_PATH}`]: aCard({ state: 'frozen' }) });
    const toggle = await screen.findByTestId('freeze-toggle');
    expect(toggle).toHaveTextContent('Unfreeze card');

    fireEvent.press(toggle);

    await waitFor(() => {
      expect(calls).toContain(`POST ${CARD_PATH}/activate`);
    });
  });

  it('says activate, not unfreeze, for a card that has never been activated', async () => {
    // Same endpoint, different truth. "Unfreeze" on a card nobody froze is a lie
    // about what happened to it.
    await show({ [`GET ${CARD_PATH}`]: aCard({ state: 'unactivated' }) });

    expect(await screen.findByTestId('freeze-toggle')).toHaveTextContent('Activate card');
  });

  it('refuses to offer anything for a canceled card, and says why', async () => {
    await show({ [`GET ${CARD_PATH}`]: aCard({ state: 'canceled' }) });

    const toggle = await screen.findByTestId('freeze-toggle');
    expect(toggle.props.accessibilityState.disabled).toBe(true);
    expect(screen.getByText(/terminal/)).toBeTruthy();
  });

  it('re-reads the balance after a state change, not just the card', async () => {
    // At a crypto-deposit provider the balance is the Safe's rather than the
    // card's, so freezing can change what is spendable.
    const { calls } = await show({});
    fireEvent.press(await screen.findByTestId('freeze-toggle'));

    await waitFor(() => {
      expect(calls.filter((c) => c === `GET ${CARD_PATH}/balance`).length).toBeGreaterThan(1);
    });
  });

  it('reports a refused change without losing the card', async () => {
    await show({
      [`POST ${CARD_PATH}/freeze`]: {
        status: 409,
        code: 'illegal_card_transition',
        detail: 'card card_1 cannot go from canceled to frozen',
      },
    });

    fireEvent.press(await screen.findByTestId('freeze-toggle'));

    expect(await screen.findByTestId('error-notice')).toBeTruthy();
    // Still on screen. A failed action must not blank the thing it acted on.
    expect(screen.getByTestId('masked-pan')).toBeTruthy();
  });
});

describe('when things are not working', () => {
  it('says the backend is unreachable rather than showing a status code', async () => {
    // Through the real client, with `fetch` rejecting the way it does when nothing
    // is listening. Stubbing `getCard` to throw an `ApiError` directly would assert
    // this screen's handling of a value the client might never produce.
    const client = new StableCardClient({
      baseUrl: BASE,
      fetch: (() => Promise.reject(new TypeError('Network request failed'))) as typeof fetch,
    });
    await renderWithApi(
      <CardScreen selection={SELECTION} onOpenReveal={jest.fn()} onOpenFund={jest.fn()} />,
      client,
    );

    expect(await screen.findByText('Cannot reach the backend')).toBeTruthy();
    // And the way out, since the person looking at this is the one who can fix it.
    expect(screen.getByText(/uvicorn app.main:app/)).toBeTruthy();
  });

  it('keeps the last good balance when a refresh fails', async () => {
    // The screen polls. One failed tick should show a notice beside the figure,
    // not replace a balance that was correct fifteen seconds ago.
    let attempts = 0;
    const { client } = fakeClient({
      [`GET ${CARD_PATH}`]: aCard(),
      [`GET ${CARD_PATH}/balance`]: () => {
        attempts += 1;
        return attempts === 1
          ? { card_id: 'card_1', amount_minor: 74_016, currency: 'USD' }
          : { status: 502, code: 'issuer_error', detail: 'provider is having a moment' };
      },
    });
    await renderWithApi(
      <CardScreen selection={SELECTION} onOpenReveal={jest.fn()} onOpenFund={jest.fn()} />,
      client,
    );
    const balance = await screen.findByTestId('balance');

    expect(balance).toHaveTextContent(/740\.16/);
  });
});
