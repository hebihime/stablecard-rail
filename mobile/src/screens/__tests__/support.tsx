/**
 * Shared scaffolding for screen tests.
 *
 * `fakeClient` is a real `StableCardClient` with `fetch` stubbed by route, rather
 * than a hand-written object with the same method names. The difference matters:
 * a hand-written double would not exercise path building, the error envelope, or
 * the `unreachable` mapping, so a screen could pass its tests against a client
 * shaped differently from the one it runs with.
 */

import type { ReactElement } from 'react';
import { render } from '@testing-library/react-native';

import { ApiProvider } from '../../api/ApiProvider';
import { StableCardClient } from '../../api/client';
import type { Card, FundingIntent, PendingChallenge } from '../../api/types';

export const BASE = 'http://127.0.0.1:8000';

/** `METHOD /path` -> what the backend answers, or a function for per-call answers. */
export type Routes = Record<string, unknown | ((body: unknown) => unknown)>;

export interface Failure {
  status: number;
  code: string;
  detail: string;
}

export function isFailure(value: unknown): value is Failure {
  return typeof value === 'object' && value !== null && 'status' in value && 'code' in value;
}

export function fakeClient(routes: Routes): {
  client: StableCardClient;
  calls: string[];
} {
  const calls: string[] = [];
  const client = new StableCardClient({
    baseUrl: BASE,
    fetch: (async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const path = url.slice(BASE.length);
      calls.push(`${method} ${path}`);
      // Exact match first, then the path without its query string: most routes do
      // not care about filters, and the ones that do can register the full form.
      const answer =
        routes[`${method} ${path}`] ?? routes[`${method} ${path.split('?')[0] ?? ''}`];
      if (answer === undefined) {
        return { ok: false, status: 404, json: async () => ({ code: 'not_found', detail: path }) };
      }
      const body =
        typeof answer === 'function'
          ? (answer as (b: unknown) => unknown)(
              init?.body === undefined ? undefined : JSON.parse(String(init.body)),
            )
          : answer;
      if (isFailure(body)) {
        return {
          ok: false,
          status: body.status,
          json: async () => ({ code: body.code, detail: body.detail }),
        };
      }
      return { ok: true, status: 200, json: async () => body };
    }) as unknown as typeof fetch,
  });
  return { client, calls };
}

/**
 * `await` is not optional here. `@testing-library/react-native` 14 made `render`
 * asynchronous for React 19's concurrent renderer, and a forgotten `await` fails as
 * "render function has not been called" from `screen` — which reads as a setup
 * problem rather than a missing keyword.
 */
export async function renderWithApi(element: ReactElement, client: StableCardClient) {
  return render(<ApiProvider client={client}>{element}</ApiProvider>);
}

// --- fixtures, kept minimal and obviously fake -------------------------------

export const aCard = (overrides: Partial<Card> = {}): Card => ({
  provider_id: 'gnosis_pay_mock',
  card_id: 'card_1',
  cardholder_id: 'user_1',
  state: 'active',
  last_four: '4242',
  exp_month: 3,
  exp_year: 2029,
  currency: 'USD',
  spend_limit_minor: 100_000,
  deposit_address: '0xSafe',
  created_at: '2026-07-27T12:00:00Z',
  raw: {},
  ...overrides,
});

export const anIntent = (overrides: Partial<FundingIntent> = {}): FundingIntent => ({
  id: '11111111-1111-1111-1111-111111111111',
  state: 'BRIDGING',
  provider_id: 'gnosis_pay_mock',
  card_id: 'card_1',
  amount_minor: 2500,
  currency: 'USD',
  bridged_amount_minor: null,
  deposit_tx_ref: 'sig_1',
  bridge_ref: null,
  issuer_funding_ref: null,
  retry_count: 0,
  last_error: null,
  created_at: '2026-07-27T12:00:00Z',
  updated_at: '2026-07-27T12:00:00Z',
  state_changed_at: '2026-07-27T12:00:00Z',
  progress: {
    sequence: [
      'PENDING',
      'DEPOSIT_CONFIRMED',
      'BRIDGING',
      'BRIDGED',
      'FUNDING',
      'FUNDED',
      'SETTLED',
    ],
    position: 2,
    is_terminal: false,
    is_failure: false,
    ...overrides.progress,
  },
  ...overrides,
});

export const aChallenge = (overrides: Partial<PendingChallenge> = {}): PendingChallenge => ({
  provider_id: 'gnosis_pay_mock',
  challenge_id: '3ds_000001',
  card_id: 'card_1',
  cardholder_id: 'user_1',
  code: '481920',
  derived: false,
  delivered_at: '2026-07-27T12:00:00Z',
  expires_at: '2026-07-27T12:05:00Z',
  seconds_remaining: 300,
  amount_minor: 4250,
  currency: 'USD',
  ...overrides,
});
