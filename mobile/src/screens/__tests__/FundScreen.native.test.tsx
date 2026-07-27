/**
 * The fund screen (SPEC.md §9.3).
 *
 * The wallet is mocked here and nowhere else. Its own module is exercised against
 * real `@solana/web3.js` in `src/wallet/__tests__`; what this file is about is the
 * screen — which address it shows, what it does with a wallet that has no money,
 * and whether the stepper renders the backend's sequence rather than one of its own.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react-native';

import { FundScreen } from '../FundScreen';
import { WalletError } from '../../wallet/errors';
import { anIntent, fakeClient, renderWithApi } from './support';

// Mocked outright, with no `requireActual`. `wallet.ts` imports
// `@solana/web3.js`, which is ESM-only and would have to be transformed for this
// suite — for a screen that never touches a chain. The wallet's own tests load it
// for real; the pure half it shares with this screen lives in `wallet/errors.ts`.
jest.mock('../../wallet/wallet', () => ({
  __esModule: true,
  loadWallet: jest.fn(async () => ({ keypair: { publicKey: 'fake' }, persisted: true })),
  readWallet: jest.fn(async () => ({
    address: 'WalletAddr111',
    solLamports: 50_000_000,
    usdcMinor: 5_000_000,
    persisted: true,
  })),
  sendUsdc: jest.fn(async () => 'sig_sent_1'),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const walletModule = require('../../wallet/wallet') as {
  readWallet: jest.Mock;
  sendUsdc: jest.Mock;
};

jest.mock('react-native-qrcode-svg', () => {
  // The QR library draws SVG paths, which say nothing useful in a snapshot. What
  // matters is that it is handed the address, so the stand-in records that.
  const { Text } = jest.requireActual('react-native') as typeof import('react-native');
  return {
    __esModule: true,
    default: ({ value }: { value: string }) => <Text testID="qr-value">{value}</Text>,
  };
});

const SELECTION = { providerId: 'gnosis_pay_mock', cardId: 'card_1' };
const ROUTE = {
  chain: 'solana-devnet',
  deposit_address: 'DepositATA111',
  owner_address: 'WalletOwner111',
  mint: 'Mint111',
  decimals: 6,
  provider_id: 'gnosis_pay_mock',
  card_id: 'card_1',
};

afterEach(() => {
  jest.clearAllMocks();
});

async function show(routes: Record<string, unknown> = {}) {
  const { client, calls } = fakeClient({
    'POST /funding/deposit-routes': ROUTE,
    'GET /funding/intents': { count: 0, intents: [] },
    ...routes,
  });
  await renderWithApi(<FundScreen selection={SELECTION} />, client);
  return { calls };
}

describe('the address', () => {
  it('shows the account the backend watches, in text and as a QR', async () => {
    await show();

    expect(await screen.findByTestId('deposit-address')).toHaveTextContent('DepositATA111');
    expect(screen.getByTestId('qr-value')).toHaveTextContent('DepositATA111');
  });

  it('is the source address, and says so beside the owner', async () => {
    // The §9.8 trap in its other direction: showing the card's Safe here would
    // collect real money at an address the watcher never polls.
    await show();

    await screen.findByTestId('deposit-address');
    expect(screen.getByText(/not the card's Safe/)).toBeTruthy();
    expect(screen.getByText(/WalletOwner111/)).toBeTruthy();
  });

  it('claims the route on open, which the backend treats as idempotent', async () => {
    const { calls } = await show();

    await screen.findByTestId('deposit-address');
    expect(calls).toContain('POST /funding/deposit-routes');
  });

  it('copies the address', async () => {
    await show();

    fireEvent.press(await screen.findByTestId('copy-address'));

    await waitFor(() => {
      expect(screen.getByTestId('copy-address')).toHaveTextContent('Copied');
    });
  });
});

describe('the wallet', () => {
  it('sends to the watched address, with the mint and decimals the backend gave', async () => {
    // `transferChecked` verifies both at the token program. Getting the decimals
    // wrong is a valid transaction that moves the wrong amount.
    await show();

    fireEvent.press(await screen.findByTestId('send-usdc'));

    await waitFor(() => {
      expect(walletModule.sendUsdc).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ to: 'DepositATA111', mint: 'Mint111', decimals: 6 }),
      );
    });
  });

  it('shows the chain’s own refusal rather than a generic failure', async () => {
    // The state of the demo today: the wallet has no money. The message is the one
    // that says what to do about it, and nothing here changes when a faucet lands.
    walletModule.sendUsdc.mockRejectedValueOnce(
      new WalletError('no-sol', 'this wallet has no devnet SOL to pay the fee'),
    );
    await show();

    fireEvent.press(await screen.findByTestId('send-usdc'));

    expect(await screen.findByTestId('wallet-error')).toHaveTextContent(/no devnet SOL/);
  });

  it('warns before the attempt when the wallet is visibly empty', async () => {
    walletModule.readWallet.mockResolvedValueOnce({
      address: 'WalletAddr111',
      solLamports: 0,
      usdcMinor: null,
      persisted: true,
    });
    await show();

    expect(await screen.findByText(/Unfunded/)).toBeTruthy();
  });

  it('says when a signing key could not be kept', async () => {
    // Persisting a signing key into storage that admits it protects nothing would
    // be worse than not persisting it, so the screen reports the choice.
    walletModule.readWallet.mockResolvedValueOnce({
      address: 'WalletAddr111',
      solLamports: 50_000_000,
      usdcMinor: 5_000_000,
      persisted: false,
    });
    await show();

    expect(await screen.findByText(/no secure storage/)).toBeTruthy();
  });
});

describe('the state machine', () => {
  it('renders the sequence the backend sent, not one of its own', async () => {
    // The point of serving `progress.sequence`: a client-side copy of the machine
    // is a second source of truth, and this one has already changed twice.
    await show({ 'GET /funding/intents': { count: 1, intents: [anIntent()] } });

    await screen.findByTestId('step-PENDING');
    for (const step of ['DEPOSIT_CONFIRMED', 'BRIDGING', 'BRIDGED', 'FUNDING', 'FUNDED']) {
      expect(screen.getByTestId(`step-${step}`)).toBeTruthy();
    }
  });

  it('draws a failure as a stopped journey, with the reason', async () => {
    // Not a red step three of seven with four still to come. `position` is null
    // for a failure state precisely so a client cannot draw that.
    await show({
      'GET /funding/intents': {
        count: 1,
        intents: [
          anIntent({
            state: 'FAILED_BRIDGE',
            last_error: 'bridge refused: no route',
            progress: {
              sequence: ['PENDING', 'DEPOSIT_CONFIRMED', 'BRIDGING'],
              position: null,
              is_terminal: true,
              is_failure: true,
            },
          }),
        ],
      },
    });

    expect(await screen.findByTestId('intent-failure')).toHaveTextContent('bridge refused: no route');
    expect(screen.queryByTestId('step-PENDING')).toBeNull();
  });

  it('shows the bridge fee as the difference between two numbers', async () => {
    await show({
      'GET /funding/intents': {
        count: 1,
        intents: [anIntent({ amount_minor: 2500, bridged_amount_minor: 2470 })],
      },
    });

    expect(await screen.findByText(/fee.*0\.30/)).toBeTruthy();
  });

  it('says nothing is happening rather than showing an empty stepper', async () => {
    await show();

    expect(await screen.findByText(/Nothing yet/)).toBeTruthy();
  });
});
