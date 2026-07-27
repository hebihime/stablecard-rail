/**
 * The reveal screen (SPEC.md §9.2).
 *
 * The exchange, the auto-hide, and the two places this screen has to tell the truth
 * rather than imply something flattering: there is no PAN, and the protection it got
 * on this device may not be the one the phones give.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react-native';

import { MemoryCardVault } from '../../../modules/card-vault/src/memoryVault';
import { VAULT_KEYS, __setCardVaultForTesting, cardVault } from '../../../modules/card-vault';
import { RevealScreen } from '../RevealScreen';
import { fakeClient, renderWithApi } from './support';

const SELECTION = { providerId: 'gnosis_pay_mock', cardId: 'card_1' };
const MINT = 'POST /providers/gnosis_pay_mock/cards/card_1/reveal-token';

const A_TOKEN = { token: 'tok_abc', provider_id: 'gnosis_pay_mock', card_id: 'card_1', expires_at: '2026-07-27T12:01:00Z', expires_in: 60 };
const A_CARD = {
  provider_id: 'gnosis_pay_mock',
  card_id: 'card_1',
  last_four: '4242',
  exp_month: 3,
  exp_year: 2029,
  rendered_in: 'pse-iframe',
  raw: {},
};

beforeEach(() => {
  __setCardVaultForTesting(new MemoryCardVault());
});
afterEach(() => {
  __setCardVaultForTesting(null);
});

async function show(routes: Record<string, unknown> = {}) {
  const { client, calls } = fakeClient({
    [MINT]: A_TOKEN,
    'POST /reveal': A_CARD,
    ...routes,
  });
  await renderWithApi(<RevealScreen selection={SELECTION} />, client);
  return { calls };
}

describe('the exchange', () => {
  it('mints a token and spends it', async () => {
    const { calls } = await show();

    fireEvent.press(await screen.findByTestId('reveal'));

    expect(await screen.findByTestId('revealed')).toBeTruthy();
    expect(calls).toEqual(expect.arrayContaining([MINT, 'POST /reveal']));
  });

  it('does not ask until it is asked', async () => {
    // Nothing is revealed on mount. A screen that mints on navigation spends a
    // token — and writes a ledger row — for someone who only opened the wrong tab.
    const { calls } = await show();

    expect(calls).toEqual([]);
  });

  it('shows the masked number and the expiry', async () => {
    await show();
    fireEvent.press(await screen.findByTestId('reveal'));

    expect(await screen.findByText('•••• •••• •••• 4242')).toBeTruthy();
    expect(screen.getByText('03/29')).toBeTruthy();
  });

  it('names where the real number would be, instead of inventing one', async () => {
    // The whole honesty of this screen. Sixteen plausible digits would demo better
    // and would be a lie about what the backend holds.
    await show();
    fireEvent.press(await screen.findByTestId('reveal'));

    await screen.findByTestId('revealed');
    expect(screen.getByText('pse-iframe')).toBeTruthy();
    expect(screen.getByText(/no PAN in this system/)).toBeTruthy();
  });

  it('keeps the token in the vault only between minting and spending', async () => {
    // Stored so an app backgrounded mid-reveal can finish; deleted the moment it is
    // spent, because a spent token is a credential kept for no reason.
    await show();
    fireEvent.press(await screen.findByTestId('reveal'));
    await screen.findByTestId('revealed');

    expect(await cardVault().getItem(VAULT_KEYS.revealToken)).toBeNull();
  });

  it('spends a token left over from a backgrounded attempt rather than minting again', async () => {
    await cardVault().setItem(VAULT_KEYS.revealToken, 'tok_from_before');
    const { calls } = await show();

    fireEvent.press(await screen.findByTestId('reveal'));
    await screen.findByTestId('revealed');

    expect(calls).not.toContain(MINT);
  });

  it('mints a fresh one when the stored token has expired', async () => {
    await cardVault().setItem(VAULT_KEYS.revealToken, 'tok_stale');
    let attempt = 0;
    const { calls } = await show({
      'POST /reveal': () => {
        attempt += 1;
        return attempt === 1
          ? { status: 404, code: 'not_found', detail: 'reveal token is not valid' }
          : A_CARD;
      },
    });

    fireEvent.press(await screen.findByTestId('reveal'));

    expect(await screen.findByTestId('revealed')).toBeTruthy();
    expect(calls).toContain(MINT);
  });
});

describe('the auto-hide', () => {
  it('hides the details when the countdown runs out', async () => {
    // Real timers, and a one-second deadline. Jest's fake clock does not move the
    // `Date.now()` this screen counts against — deliberately, because the deadline
    // is wall-clock: a JavaScript timer stops while an app is backgrounded, so a
    // decrementing counter would resume where it paused and leave card details on
    // screen long after they should have gone.
    await show({ [MINT]: { ...A_TOKEN, expires_in: 1 } });
    fireEvent.press(await screen.findByTestId('reveal'));
    await screen.findByTestId('revealed');

    await waitFor(
      () => {
        expect(screen.queryByTestId('revealed')).toBeNull();
      },
      { timeout: 3000 },
    );
    expect(screen.getByTestId('reveal')).toBeTruthy();
  });

  it('hides on demand', async () => {
    await show();
    fireEvent.press(await screen.findByTestId('reveal'));
    await screen.findByTestId('revealed');

    fireEvent.press(screen.getByTestId('hide'));

    // `waitFor` rather than a bare assertion: React 19 renders concurrently, so a
    // state change from a press is not on screen by the time `press` returns.
    await waitFor(() => {
      expect(screen.queryByTestId('revealed')).toBeNull();
    });
  });
});

describe('a provider that has no reveal path', () => {
  it('explains rather than reporting an error', async () => {
    await show({
      [MINT]: { status: 501, code: 'reveal_unsupported', detail: 'lithic has no card-reveal path' },
    });

    fireEvent.press(await screen.findByTestId('reveal'));

    expect(await screen.findByText('Not available for this provider')).toBeTruthy();
    // And no retry button: waiting does not give a provider a capability.
    expect(screen.queryByTestId('retry')).toBeNull();
  });
});

describe('what this device actually gave us', () => {
  it('reports the vault backend instead of implying a Keychain', async () => {
    // Under Jest there is no native module, so the fallback is in play — and this
    // is exactly the case where a screen must not claim secure storage.
    await show();

    expect(screen.getByText(/memory \(none\)/)).toBeTruthy();
    expect(screen.getByText(/No secure storage on this platform/)).toBeTruthy();
  });
});
