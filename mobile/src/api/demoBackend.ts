/**
 * A backend made of recorded shapes, for the build with nothing behind it.
 *
 * This is what `EXPO_PUBLIC_DEMO=1` gets: a deployed web build a stranger can click
 * with no server, no database and no credentials anywhere. jp chose it at the
 * phase-8 boundary over hosting the real stack, which stays available later.
 *
 * **It is a fake `fetch`, not a fake client.** The app builds a real
 * `StableCardClient` around this, so path construction, the error envelope, the
 * `unreachable` mapping and every retry rule are the production ones. A hand-written
 * client double would have quietly replaced all of that, and the demo would be
 * exercising code the app does not run.
 *
 * Two rules it holds itself to:
 *
 * - **Every shape here is one the backend actually produces.** They are transcribed
 *   from the response models in `app/api/`, and the contract test in
 *   `__tests__/contract.native.test.ts` checks them against a live backend when one
 *   is reachable.
 * - **It never pretends money moved.** The funding intent advances on a timer
 *   because that is what a state machine looks like, and the demo says on the fund
 *   screen that no chain is involved. A demo that showed a real-looking transaction
 *   signature would be the one dishonest thing in this repo.
 */

import type {
  Balance,
  Card,
  Cardholder,
  DepositRoute,
  FundingIntent,
  FundingState,
  PendingChallenge,
  Provider,
} from './types';

/** How long each state lasts before the scripted intent moves on. */
const STEP_MS = 2_500;

const SEQUENCE: FundingState[] = [
  'PENDING',
  'DEPOSIT_CONFIRMED',
  'BRIDGING',
  'BRIDGED',
  'FUNDING',
  'FUNDED',
  'SETTLED',
];

const PROVIDERS: Provider[] = [
  { provider_id: 'gnosis_pay_mock', funding_model: 'crypto_deposit' },
  { provider_id: 'lithic', funding_model: 'fiat_rail' },
  { provider_id: 'stripe_issuing', funding_model: 'fiat_rail' },
];

interface DemoState {
  card: Card | null;
  balanceMinor: number;
  intentStartedAt: number | null;
  challengeAnsweredAt: number | null;
  now: () => number;
}

export interface DemoOptions {
  /** Injected by tests so a scripted timeline can be driven rather than waited on. */
  now?: () => number;
}

/**
 * A `fetch` that answers from the script above.
 *
 * Deliberately a plain function rather than a class: the only thing the app needs
 * is something with `fetch`'s signature, and everything stateful is closed over
 * here where it can be read in one pass.
 */
export function demoFetch(options: DemoOptions = {}): typeof fetch {
  const state: DemoState = {
    card: null,
    balanceMinor: 0,
    intentStartedAt: null,
    challengeAnsweredAt: null,
    now: options.now ?? (() => Date.now()),
  };

  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://demo.invalid');
    const method = init?.method ?? 'GET';
    const body: unknown =
      init?.body === undefined ? undefined : JSON.parse(String(init.body));
    const answer = route(state, method, url, body);
    return {
      ok: answer.status < 400,
      status: answer.status,
      json: async () => answer.body,
    } as Response;
  }) as typeof fetch;
}

interface Answer {
  status: number;
  body: unknown;
}

function route(state: DemoState, method: string, url: URL, body: unknown): Answer {
  const path = url.pathname;

  if (method === 'GET' && path === '/providers') {
    return ok(PROVIDERS);
  }

  if (method === 'POST' && path.endsWith('/cardholders')) {
    const providerId = path.split('/')[2] ?? 'gnosis_pay_mock';
    const holder: Cardholder = {
      provider_id: providerId,
      cardholder_id: 'user_demo',
      email: 'demo@stablecard.test',
      state: 'active',
      created_at: new Date(state.now()).toISOString(),
      raw: {},
    };
    return { status: 201, body: holder };
  }

  if (method === 'POST' && path.endsWith('/cards')) {
    const providerId = path.split('/')[2] ?? 'gnosis_pay_mock';
    state.card = {
      provider_id: providerId,
      card_id: 'card_demo',
      cardholder_id: 'user_demo',
      state: 'active',
      last_four: '4242',
      exp_month: 3,
      exp_year: 2029,
      currency: 'USD',
      spend_limit_minor: 100_000,
      deposit_address: providerId === 'gnosis_pay_mock' ? '0xSafeDemo0000000000000000' : null,
      created_at: new Date(state.now()).toISOString(),
      raw: {},
    };
    state.balanceMinor = 0;
    return { status: 201, body: state.card };
  }

  if (method === 'GET' && path.endsWith('/balance')) {
    const balance: Balance = {
      card_id: 'card_demo',
      amount_minor: state.balanceMinor + fundedSoFar(state),
      currency: 'USD',
    };
    return ok(balance);
  }

  if (method === 'GET' && /\/providers\/[^/]+\/cards\/[^/]+$/.test(path)) {
    return state.card === null ? notFound('no such card') : ok(state.card);
  }

  if (method === 'POST' && (path.endsWith('/freeze') || path.endsWith('/activate'))) {
    if (state.card === null) {
      return notFound('no such card');
    }
    state.card = { ...state.card, state: path.endsWith('/freeze') ? 'frozen' : 'active' };
    return ok(state.card);
  }

  if (method === 'POST' && path.endsWith('/reveal-token')) {
    const providerId = path.split('/')[2] ?? '';
    if (providerId !== 'gnosis_pay_mock') {
      // The real refusal, from the real reason: neither Lithic nor Stripe has a
      // reveal path this backend will use (ARCHITECTURE §12.2).
      return { status: 501, body: problem('reveal_unsupported', `${providerId} has no card-reveal path`) };
    }
    return {
      status: 201,
      body: {
        token: `demo_${state.now()}`,
        provider_id: providerId,
        card_id: 'card_demo',
        expires_at: new Date(state.now() + 60_000).toISOString(),
        expires_in: 60,
      },
    };
  }

  if (method === 'POST' && path === '/reveal') {
    return ok({
      provider_id: 'gnosis_pay_mock',
      card_id: 'card_demo',
      last_four: '4242',
      exp_month: 3,
      exp_year: 2029,
      rendered_in: 'pse-iframe',
      raw: {},
    });
  }

  if (method === 'POST' && path === '/funding/deposit-routes') {
    // The moment the fund screen opens is when the scripted intent starts. There
    // is no chain here and the screen says so — what is being demonstrated is the
    // state machine, which is ours and is real.
    state.intentStartedAt ??= state.now();
    const route: DepositRoute = {
      chain: 'solana-devnet',
      deposit_address: 'DemoDeposit1111111111111111111111111111111',
      owner_address: 'DemoWallet11111111111111111111111111111111',
      mint: '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU',
      decimals: 6,
      provider_id: 'gnosis_pay_mock',
      card_id: 'card_demo',
    };
    return { status: 201, body: route };
  }

  if (method === 'GET' && path === '/funding/intents') {
    const intent = scriptedIntent(state);
    return ok({ count: intent === null ? 0 : 1, intents: intent === null ? [] : [intent] });
  }

  if (method === 'GET' && path === '/otp/pending') {
    const challenge = scriptedChallenge(state);
    return ok({ count: challenge === null ? 0 : 1, challenges: challenge === null ? [] : [challenge] });
  }

  if (method === 'POST' && /^\/otp\/[^/]+\/[^/]+\/respond$/.test(path)) {
    state.challengeAnsweredAt = state.now();
    const [, , providerId, challengeId] = path.split('/');
    return ok({
      provider_id: providerId,
      challenge_id: challengeId,
      decision: (body as { decision?: string })?.decision ?? 'approve',
      delivered: true,
      provider_ref: challengeId,
      detail: null,
    });
  }

  if (method === 'GET' && path === '/ledger') {
    return ok({ count: 0, events: [] });
  }

  return notFound(path);
}

/** Where the scripted intent has got to, from elapsed time alone. */
function scriptedIntent(state: DemoState): FundingIntent | null {
  if (state.intentStartedAt === null) {
    return null;
  }
  const elapsed = state.now() - state.intentStartedAt;
  const index = Math.min(SEQUENCE.length - 1, Math.floor(elapsed / STEP_MS));
  const current = SEQUENCE[index]!;
  const bridged = index >= SEQUENCE.indexOf('BRIDGED');
  return {
    id: 'demo-intent-0000-0000-000000000000',
    state: current,
    provider_id: 'gnosis_pay_mock',
    card_id: 'card_demo',
    amount_minor: 100_000,
    currency: 'USD',
    // Net of a fee, so the difference is visible — SPEC.md §11's point, and the
    // reason both numbers are returned rather than one corrected one.
    bridged_amount_minor: bridged ? 99_700 : null,
    deposit_tx_ref: 'demo-signature',
    bridge_ref: index >= SEQUENCE.indexOf('BRIDGING') ? 'demo-bridge-order' : null,
    issuer_funding_ref: index >= SEQUENCE.indexOf('FUNDED') ? 'demo-funding' : null,
    retry_count: 0,
    last_error: null,
    created_at: new Date(state.intentStartedAt).toISOString(),
    updated_at: new Date(state.now()).toISOString(),
    state_changed_at: new Date(state.now()).toISOString(),
    progress: {
      sequence: SEQUENCE,
      position: index,
      is_terminal: current === 'SETTLED',
      is_failure: false,
    },
  };
}

/** What the card has been credited, once the scripted intent reaches FUNDED. */
function fundedSoFar(state: DemoState): number {
  const intent = scriptedIntent(state);
  if (intent === null) {
    return 0;
  }
  return SEQUENCE.indexOf(intent.state) >= SEQUENCE.indexOf('FUNDED')
    ? (intent.bridged_amount_minor ?? 0)
    : 0;
}

/**
 * A 3DS challenge, once the card has money and before it has been answered.
 *
 * Timed rather than triggered, because the thing being demonstrated is a challenge
 * *arriving* — a button marked "simulate a 3DS challenge" would show the modal and
 * hide the point, which is that this happens without the app asking.
 */
function scriptedChallenge(state: DemoState): PendingChallenge | null {
  if (state.challengeAnsweredAt !== null || state.intentStartedAt === null) {
    return null;
  }
  const appearsAt = state.intentStartedAt + STEP_MS * (SEQUENCE.indexOf('FUNDED') + 1);
  if (state.now() < appearsAt) {
    return null;
  }
  return {
    provider_id: 'gnosis_pay_mock',
    challenge_id: '3ds_demo_1',
    card_id: 'card_demo',
    cardholder_id: 'user_demo',
    code: '481920',
    derived: false,
    delivered_at: new Date(appearsAt).toISOString(),
    expires_at: new Date(appearsAt + 300_000).toISOString(),
    seconds_remaining: Math.max(0, Math.ceil((appearsAt + 300_000 - state.now()) / 1000)),
    amount_minor: 4_250,
    currency: 'USD',
  };
}

const ok = (body: unknown): Answer => ({ status: 200, body });
const notFound = (detail: string): Answer => ({
  status: 404,
  body: problem('not_found', detail),
});
const problem = (code: string, detail: string) => ({ code, detail });
