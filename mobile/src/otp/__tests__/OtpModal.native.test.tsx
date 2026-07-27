/**
 * The 3DS modal and its two delivery routes (SPEC.md §6.3, §9.4).
 *
 * The behaviour worth protecting is the ordering in §6.3: polling is the contract
 * and push is a courtesy. Several tests here take the socket away entirely and
 * assert the modal still works, because a hook that quietly depended on the socket
 * would pass every happy-path test and strand a cardholder on hotel wifi.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react-native';

import { OtpModal } from '../OtpModal';
import { aChallenge, fakeClient, renderWithApi } from '../../screens/__tests__/support';

/** The shape both fakes satisfy — a constructor taking a URL, as `WebSocket` is. */
type SocketConstructor = new (url: string) => DeadSocket;

/** A `WebSocket` that never connects — the everyday case this must survive. */
class DeadSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  close = jest.fn();

  constructor(_url: string) {
    // Signature only; a dead socket does nothing with the URL it is given.
  }
}

/** One that connects and can be driven from a test. */
class LiveSocket extends DeadSocket {
  static last: LiveSocket | null = null;
  readonly url: string;

  constructor(url: string) {
    super(url);
    this.url = url;
    LiveSocket.last = this;
    // Connect on the next tick, as a real one does.
    setTimeout(() => this.onopen?.(), 0);
  }

  deliver(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

function useSocket(implementation: SocketConstructor | null) {
  const original = globalThis.WebSocket;
  if (implementation === null) {
    // Not merely broken — absent. A runtime without WebSocket at all is the
    // harshest version of "push is a courtesy".
    Object.defineProperty(globalThis, 'WebSocket', { value: undefined, configurable: true });
  } else {
    Object.defineProperty(globalThis, 'WebSocket', {
      value: implementation,
      configurable: true,
    });
  }
  return () => {
    Object.defineProperty(globalThis, 'WebSocket', { value: original, configurable: true });
  };
}

let restoreSocket: (() => void) | null = null;

afterEach(() => {
  restoreSocket?.();
  restoreSocket = null;
  LiveSocket.last = null;
});

async function show(
  routes: Record<string, unknown> = {},
  socket: SocketConstructor | null = DeadSocket,
) {
  restoreSocket = useSocket(socket);
  const { client, calls } = fakeClient({
    'GET /otp/pending': { count: 0, challenges: [] },
    ...routes,
  });
  await renderWithApi(<OtpModal cardId="card_1" />, client);
  return { calls };
}

describe('polling, which is the contract', () => {
  it('shows a challenge the poll found, with no socket at all', async () => {
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } }, null);

    expect(await screen.findByTestId('otp-modal')).toBeTruthy();
    expect(screen.getByTestId('otp-code')).toHaveTextContent('481920');
  });

  it('shows one the poll found when the socket never connects', async () => {
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } });

    expect(await screen.findByTestId('otp-modal')).toBeTruthy();
  });

  it('stays out of the way when nothing is open', async () => {
    await show();

    await waitFor(() => {
      expect(screen.queryByTestId('otp-modal')).toBeNull();
    });
  });

  it('says which route is live, without depending on the answer', async () => {
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } });

    await screen.findByTestId('otp-modal');
    expect(screen.getByText('polling')).toBeTruthy();
  });
});

describe('push, which is the courtesy', () => {
  it('shows a challenge that arrived over the socket', async () => {
    await show({}, LiveSocket);
    await waitFor(() => {
      expect(LiveSocket.last).not.toBeNull();
    });

    LiveSocket.last!.deliver(aChallenge({ code: '112233' }));

    expect(await screen.findByTestId('otp-code')).toHaveTextContent('112233');
  });

  it('connects to the socket URL derived from the API base', async () => {
    await show({}, LiveSocket);

    await waitFor(() => {
      expect(LiveSocket.last?.url).toBe('ws://127.0.0.1:8000/ws/otp?card_id=card_1');
    });
  });

  it('does not show the same challenge twice when both routes deliver it', async () => {
    // The reason the backend sends the identical shape on both: a client dedupes on
    // the id and never has to know which arrived first.
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } }, LiveSocket);
    await waitFor(() => {
      expect(LiveSocket.last).not.toBeNull();
    });

    LiveSocket.last!.deliver(aChallenge());

    await screen.findByTestId('otp-modal');
    expect(screen.getAllByTestId('otp-code')).toHaveLength(1);
  });

  it('ignores a frame it cannot parse', async () => {
    // The poll produces the same challenge shortly, correctly parsed. Throwing
    // inside the socket handler would take the hook down with it.
    await show({}, LiveSocket);
    await waitFor(() => {
      expect(LiveSocket.last).not.toBeNull();
    });

    LiveSocket.last!.onmessage?.({ data: 'not json' });

    await waitFor(() => {
      expect(screen.queryByTestId('otp-modal')).toBeNull();
    });
  });
});

describe('answering', () => {
  const respond = 'POST /otp/gnosis_pay_mock/3ds_000001/respond';

  it('approves', async () => {
    const { calls } = await show({
      'GET /otp/pending': { count: 1, challenges: [aChallenge()] },
      [respond]: {
        provider_id: 'gnosis_pay_mock',
        challenge_id: '3ds_000001',
        decision: 'approve',
        delivered: true,
        provider_ref: '3ds_000001',
        detail: null,
      },
    });

    fireEvent.press(await screen.findByTestId('approve'));

    await waitFor(() => {
      expect(calls).toContain(respond);
    });
  });

  it('closes on the button rather than on the next poll', async () => {
    // Three seconds of a code still on screen after the tap reads as the tap not
    // registering, and invites a second one.
    await show({
      'GET /otp/pending': { count: 1, challenges: [aChallenge()] },
      [respond]: {
        provider_id: 'gnosis_pay_mock',
        challenge_id: '3ds_000001',
        decision: 'decline',
        delivered: true,
        provider_ref: null,
        detail: null,
      },
    });

    fireEvent.press(await screen.findByTestId('decline'));

    await waitFor(() => {
      expect(screen.queryByTestId('otp-modal')).toBeNull();
    });
  });

  it('reports an undeliverable decision as recorded, not as failed', async () => {
    // SPEC.md §6.5's fallback. The provider has no endpoint; the decision is in the
    // ledger. Calling that a failure would be wrong about what happened.
    await show({
      'GET /otp/pending': { count: 1, challenges: [aChallenge()] },
      [respond]: {
        provider_id: 'gnosis_pay_mock',
        challenge_id: '3ds_000001',
        decision: 'approve',
        delivered: false,
        provider_ref: null,
        detail: 'stripe_issuing has no challenge-response endpoint',
      },
    });

    fireEvent.press(await screen.findByTestId('approve'));

    expect(await screen.findByTestId('otp-outcome')).toHaveTextContent(/no challenge-response/);
  });

  it('surfaces a challenge that expired before the tap landed', async () => {
    await show({
      'GET /otp/pending': { count: 1, challenges: [aChallenge()] },
      [respond]: {
        status: 404,
        code: 'not_found',
        detail: 'no open 3DS challenge; it may have expired or already been answered',
      },
    });

    fireEvent.press(await screen.findByTestId('approve'));

    expect(await screen.findByTestId('otp-outcome')).toHaveTextContent(/expired/);
  });
});

describe('the code itself', () => {
  it('says when the code was minted here rather than sent by the provider', async () => {
    // A cardholder waiting for an SMS that is never coming is stuck, and only this
    // line tells them otherwise (ARCHITECTURE §11.4).
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge({ derived: true })] } });

    expect(await screen.findByText(/no SMS coming/)).toBeTruthy();
  });

  it('copies the code', async () => {
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } });

    fireEvent.press(await screen.findByTestId('copy-code'));

    await waitFor(() => {
      expect(screen.getByTestId('copy-code')).toHaveTextContent('Copied');
    });
  });

  it('shows the amount being confirmed', async () => {
    await show({ 'GET /otp/pending': { count: 1, challenges: [aChallenge()] } });

    expect(await screen.findByText(/42\.50/)).toBeTruthy();
  });

  it('counts down against the server’s deadline', async () => {
    // Not `seconds_remaining` ticking down locally: the deadline is what the server
    // sent, and the device's clock is the one the backend cannot vouch for.
    const expires = new Date(Date.now() + 65_000).toISOString();
    await show({
      'GET /otp/pending': {
        count: 1,
        challenges: [aChallenge({ expires_at: expires, seconds_remaining: 65 })],
      },
    });

    expect(await screen.findByTestId('otp-countdown')).toHaveTextContent(/1:0[45]/);
  });
});
