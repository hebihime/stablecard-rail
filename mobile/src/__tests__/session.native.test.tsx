/**
 * Which card the app is looking at, and how long it remembers.
 *
 * The rule these pin is one bug's worth of hindsight: **the selection must not
 * outlive the thing it points at.** `gnosis_pay_mock`'s simulator lives inside the
 * backend process and the demo backend lives inside a page load; a selection stored
 * in the device's vault outlives both. jp hit the result on the first real run —
 * "no such card", on every launch after the backend restarted, with nothing on
 * screen able to clear it.
 */

import { Text } from 'react-native';
import { act, render, screen } from '@testing-library/react-native';

import { __setCardVaultForTesting, VAULT_KEYS, cardVault } from '../../modules/card-vault';
import { MemoryCardVault } from '../../modules/card-vault/src/memoryVault';
import { SessionProvider, useSession } from '../session';

function Probe() {
  const { selection, ready } = useSession();
  return (
    <Text testID="probe">
      {!ready ? 'loading' : selection === null ? 'none' : `${selection.providerId}/${selection.cardId}`}
    </Text>
  );
}

beforeEach(() => {
  __setCardVaultForTesting(new MemoryCardVault());
});
afterEach(() => {
  __setCardVaultForTesting(null);
});

async function mount(persist: boolean) {
  return render(
    <SessionProvider persist={persist}>
      <Probe />
    </SessionProvider>,
  );
}

describe('when the selection is persisted', () => {
  it('restores what was stored', async () => {
    await cardVault().setItem(
      VAULT_KEYS.selectedCard,
      JSON.stringify({ providerId: 'gnosis_pay_mock', cardId: 'card_1' }),
    );

    await mount(true);

    expect(await screen.findByTestId('probe')).toHaveTextContent('gnosis_pay_mock/card_1');
  });

  it('reports none rather than loading forever when nothing is stored', async () => {
    await mount(true);

    expect(await screen.findByTestId('probe')).toHaveTextContent('none');
  });

  it('treats an unreadable stored value as nothing', async () => {
    // A record from an older build, or a partly-written one. Crashing the first
    // render of the first screen is unrecoverable on a device.
    await cardVault().setItem(VAULT_KEYS.selectedCard, '{not json');

    await mount(true);

    expect(await screen.findByTestId('probe')).toHaveTextContent('none');
  });

  it('treats a stored value of the wrong shape as nothing', async () => {
    await cardVault().setItem(VAULT_KEYS.selectedCard, JSON.stringify({ providerId: 7 }));

    await mount(true);

    expect(await screen.findByTestId('probe')).toHaveTextContent('none');
  });
});

describe('when it is not', () => {
  it('ignores a stored selection entirely', async () => {
    // Demo mode. The backend is a closure recreated on every page load, so a card
    // created before a reload does not exist after one — and a restored selection
    // would point at it on every single visit.
    await cardVault().setItem(
      VAULT_KEYS.selectedCard,
      JSON.stringify({ providerId: 'gnosis_pay_mock', cardId: 'card_1' }),
    );

    await mount(false);

    expect(await screen.findByTestId('probe')).toHaveTextContent('none');
  });

  it('still tracks a selection made during this session', async () => {
    // Not persisting is not the same as not remembering: the card screen has to
    // work after onboarding creates a card, reload or no reload.
    let session: ReturnType<typeof useSession> | null = null;
    function Capture() {
      session = useSession();
      return <Probe />;
    }
    await render(
      <SessionProvider persist={false}>
        <Capture />
      </SessionProvider>,
    );
    await screen.findByTestId('probe');

    await act(async () => {
      await session!.select({ providerId: 'lithic', cardId: 'card_9' });
    });

    expect(screen.getByTestId('probe')).toHaveTextContent('lithic/card_9');
    // And nothing was written, which is the whole point.
    expect(await cardVault().getItem(VAULT_KEYS.selectedCard)).toBeNull();
  });
});

describe('forgetting', () => {
  it('clears a stored selection even in a build that would not write one', async () => {
    // Asymmetric on purpose. A selection left behind by an earlier build, or by a
    // run configured differently, has to be clearable by the button that exists to
    // clear it — otherwise the recovery path cannot recover.
    await cardVault().setItem(
      VAULT_KEYS.selectedCard,
      JSON.stringify({ providerId: 'gnosis_pay_mock', cardId: 'card_1' }),
    );
    let session: ReturnType<typeof useSession> | null = null;
    function Capture() {
      session = useSession();
      return <Probe />;
    }
    await render(
      <SessionProvider persist={false}>
        <Capture />
      </SessionProvider>,
    );
    await screen.findByTestId('probe');

    await act(async () => {
      await session!.forget();
    });

    expect(await cardVault().getItem(VAULT_KEYS.selectedCard)).toBeNull();
  });
});
