/**
 * One typed client for the whole backend (SPEC.md §9).
 *
 * Every screen goes through here, so the awkward parts of talking to this API are
 * handled once. Three of them are worth naming, because each is a bug a screen
 * would otherwise carry its own version of:
 *
 * - **A failure is not always a status.** `fetch` rejects outright when nothing
 *   answers — the backend is not running, the phone is on a captive network — and a
 *   screen that only knows about status codes will render "500" for it. `ApiError`
 *   has a null status and the code `unreachable` for exactly that case, because
 *   "cannot reach the backend" is the only honest thing to show.
 * - **The error envelope is documented, and proxies do not read documentation.**
 *   `api/errors.py` returns `{code, detail}`; a gateway in front of it returns HTML.
 *   Both have to arrive here as an `ApiError`.
 * - **A request that never finishes is worse than one that fails.** Screens poll, so
 *   a hung request leaves a spinner up and queues the next tick behind it.
 *
 * The base URL is configuration, not a constant: the same bundle runs against a
 * local backend during development and against nothing at all in the demo build
 * (see `resolveBaseUrl`).
 */

import type {
  Balance,
  Card,
  ChallengeDecision,
  ChallengeResponse,
  FundingIntent,
  FundingIntentPage,
  LedgerPage,
  PendingChallenges,
  Provider,
  RevealToken,
  RevealedCard,
} from './types';

/** Long enough for a cold backend, short enough that a screen is never stuck. */
const DEFAULT_TIMEOUT_MS = 10_000;

export interface ClientOptions {
  baseUrl: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
}

/**
 * Anything that went wrong talking to the backend.
 *
 * `status` is `null` when there was no HTTP response at all, which is a different
 * situation from every status code and is the one users hit most often in a demo.
 */
export class ApiError extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly detail: string;

  constructor(status: number | null, code: string, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }

  /** True when retrying could plausibly help — mirrors the backend's `retryable`. */
  get isTransient(): boolean {
    return (
      this.code === 'unreachable' ||
      this.code === 'timeout' ||
      (this.status !== null && this.status >= 500)
    );
  }
}

export class StableCardClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(options: ClientOptions) {
    // A trailing slash is the obvious thing to put in an env var and would produce
    // `//providers`, which some servers route and some 404.
    this.baseUrl = options.baseUrl.replace(/\/+$/, '');
    // Looked up per call rather than bound now. Constructing a client must not
    // depend on a global that may not exist yet: `challengeSocketUrl` needs no
    // `fetch` at all, and a constructor that throws would take the socket down
    // with it on any runtime where `fetch` arrives late or by polyfill.
    this.fetchImpl = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  // --- discovery and cards (SPEC.md §9.1) ----------------------------------

  listProviders(): Promise<Provider[]> {
    return this.request<Provider[]>('GET', '/providers');
  }

  getCard(providerId: string, cardId: string): Promise<Card> {
    return this.request<Card>('GET', `/providers/${enc(providerId)}/cards/${enc(cardId)}`);
  }

  getBalance(providerId: string, cardId: string): Promise<Balance> {
    return this.request<Balance>(
      'GET',
      `/providers/${enc(providerId)}/cards/${enc(cardId)}/balance`,
    );
  }

  freezeCard(providerId: string, cardId: string): Promise<Card> {
    return this.request<Card>('POST', `/providers/${enc(providerId)}/cards/${enc(cardId)}/freeze`);
  }

  /** Also the unfreeze path — one toggle on the card screen, two provider verbs. */
  activateCard(providerId: string, cardId: string): Promise<Card> {
    return this.request<Card>(
      'POST',
      `/providers/${enc(providerId)}/cards/${enc(cardId)}/activate`,
    );
  }

  // --- the reveal (SPEC.md §9.2) -------------------------------------------

  /** Throws `ApiError` with code `reveal_unsupported` for a provider that has none. */
  mintRevealToken(providerId: string, cardId: string): Promise<RevealToken> {
    return this.request<RevealToken>(
      'POST',
      `/providers/${enc(providerId)}/cards/${enc(cardId)}/reveal-token`,
    );
  }

  /** The card is not named: it is whatever the token was minted for. */
  redeemRevealToken(token: string): Promise<RevealedCard> {
    return this.request<RevealedCard>('POST', '/reveal', { token });
  }

  // --- funding (SPEC.md §9.3) ----------------------------------------------

  getIntent(intentId: string): Promise<FundingIntent> {
    return this.request<FundingIntent>('GET', `/funding/intents/${enc(intentId)}`);
  }

  listIntents(options: { cardId?: string; limit?: number } = {}): Promise<FundingIntentPage> {
    return this.request<FundingIntentPage>(
      'GET',
      `/funding/intents${query({ card_id: options.cardId, limit: options.limit })}`,
    );
  }

  // --- 3DS / OTP (SPEC.md §6, §9.4) ----------------------------------------

  pendingChallenges(cardId?: string): Promise<PendingChallenges> {
    return this.request<PendingChallenges>('GET', `/otp/pending${query({ card_id: cardId })}`);
  }

  respondToChallenge(
    providerId: string,
    challengeId: string,
    decision: ChallengeDecision,
  ): Promise<ChallengeResponse> {
    return this.request<ChallengeResponse>(
      'POST',
      `/otp/${enc(providerId)}/${enc(challengeId)}/respond`,
      { decision },
    );
  }

  /**
   * Where the push half of SPEC.md §6.3 lives.
   *
   * Derived from the API base rather than configured separately, so there is one
   * thing to point at a backend. `https` implies `wss`: a secure page cannot open
   * an insecure socket, and getting this wrong shows up only in a deployed build.
   */
  challengeSocketUrl(cardId?: string): string {
    const socketBase = this.baseUrl.replace(/^http/, 'ws');
    return `${socketBase}/ws/otp${query({ card_id: cardId })}`;
  }

  // --- the ledger (SPEC.md §7) ---------------------------------------------

  listLedger(options: { cardId?: string; intentId?: string; limit?: number } = {}) {
    return this.request<LedgerPage>(
      'GET',
      `/ledger${query({
        card_id: options.cardId,
        intent_id: options.intentId,
        limit: options.limit,
      })}`,
    );
  }

  // --- the one place a request is actually made ----------------------------

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, this.timeoutMs);

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        signal: controller.signal,
        ...(body === undefined
          ? {}
          : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      });
    } catch (raised) {
      // An abort and a dead network arrive the same way — as a rejection rather
      // than a response — and mean different things to whoever is looking at the
      // screen: one is "try again", the other is "start the backend".
      const aborted = raised instanceof Error && raised.name === 'AbortError';
      throw aborted
        ? new ApiError(null, 'timeout', `the backend did not answer within ${this.timeoutMs}ms`)
        : new ApiError(null, 'unreachable', describeUnreachable(raised, this.baseUrl));
    } finally {
      clearTimeout(timer);
    }

    if (!response.ok) {
      throw await problemFrom(response);
    }
    return (await response.json()) as T;
  }
}

/**
 * Read the backend's `{code, detail}` envelope, and cope when it is not there.
 *
 * FastAPI's own `HTTPException` produces `{detail}` with no code, and anything in
 * front of the API produces whatever it likes. Both have to become an `ApiError`
 * rather than a parse failure five frames away from the cause.
 */
async function problemFrom(response: Response): Promise<ApiError> {
  let parsed: unknown;
  try {
    parsed = await response.json();
  } catch {
    return new ApiError(response.status, 'http_error', `HTTP ${response.status}`);
  }
  const body = (parsed ?? {}) as Record<string, unknown>;
  const code = typeof body.code === 'string' ? body.code : 'http_error';
  const detail =
    typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`;
  return new ApiError(response.status, code, detail);
}

function describeUnreachable(raised: unknown, baseUrl: string): string {
  const because = raised instanceof Error ? raised.message : String(raised);
  return `cannot reach the backend at ${baseUrl} (${because})`;
}

/** Opaque identifiers go in paths, and nothing promises they are URL-safe. */
function enc(segment: string): string {
  return encodeURIComponent(segment);
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    // Absent, not empty: `?card_id=` is a filter for the empty string, which
    // matches nothing, and is a far more confusing bug than a missing filter.
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const rendered = search.toString();
  return rendered === '' ? '' : `?${rendered}`;
}
