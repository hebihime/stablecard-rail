/**
 * The API client (SPEC.md §9).
 *
 * Every screen goes through here, so this is where the awkward parts of talking to
 * the backend are handled once: the error envelope `api/errors.py` returns, a
 * network failure that is not an HTTP status at all, and the difference between
 * "the provider refused" and "the request never arrived". A screen that has to tell
 * those apart itself will get it wrong on at least one screen.
 *
 * `fetch` is stubbed rather than a server being run. The contract this asserts is
 * the *shape* of the exchange — what is sent, what is made of what comes back — and
 * that the backend really produces these shapes is asserted on the backend, by 1659
 * tests that do run against a database.
 */

import { ApiError, StableCardClient } from '../client';

const BASE = 'http://127.0.0.1:8000';

/**
 * A stand-in for `Response`, built by hand rather than constructed.
 *
 * `fetch` is stubbed here, so the client never sees a real one — and the global
 * `Response` constructor exists in the native test environments and not in the web
 * one (JSDOM omits it). Duck-typing the two members the client actually reads keeps
 * one test file running identically on all three platforms, which is the point of
 * running them on all three.
 */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** A response whose body is not JSON — a proxy's HTML error page, typically. */
function opaqueResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON');
    },
  } as unknown as Response;
}

/**
 * Await a call that must fail, and hand back the `ApiError` it failed with.
 *
 * `.catch(e => e as ApiError)` types as a union with the success value, so every
 * assertion after it needs a cast. This narrows once, and turns "it unexpectedly
 * succeeded" into a readable failure instead of a property-access error.
 */
async function failure(call: Promise<unknown>): Promise<ApiError> {
  try {
    await call;
  } catch (raised) {
    return raised as ApiError;
  }
  throw new Error('expected the call to fail, and it did not');
}

function client(fetchImpl: typeof fetch): StableCardClient {
  return new StableCardClient({ baseUrl: BASE, fetch: fetchImpl });
}

describe('requests', () => {
  it('asks the configured backend, not a hardcoded one', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse([]));

    await client(fetchMock).listProviders();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/providers`, expect.anything());
  });

  it('strips a trailing slash from the base URL so paths never double up', async () => {
    // `EXPO_PUBLIC_API_URL=http://localhost:8000/` is the obvious thing to type and
    // would otherwise produce `//providers`, which some servers route and some 404.
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse([]));
    const withSlash = new StableCardClient({ baseUrl: `${BASE}/`, fetch: fetchMock });

    await withSlash.listProviders();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/providers`, expect.anything());
  });

  it('sends JSON with a content type on a body-carrying call', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse({ provider_id: 'gnosis_pay_mock', challenge_id: 'c1', decision: 'approve' }),
    );

    await client(fetchMock).respondToChallenge('gnosis_pay_mock', 'c1', 'approve');

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' });
    expect(JSON.parse(String(init.body))).toEqual({ decision: 'approve' });
  });

  it('percent-encodes an identifier in a path', async () => {
    // Provider ids are opaque strings. Nothing guarantees they are URL-safe, and a
    // card id with a slash in it would otherwise silently address another route.
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse({ amount_minor: 0 }));

    await client(fetchMock).getBalance('gnosis_pay_mock', 'card/../../providers');

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${BASE}/providers/gnosis_pay_mock/cards/card%2F..%2F..%2Fproviders/balance`);
  });
});

describe('failures', () => {
  it('raises the backend’s own error code, not just a status', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValue(jsonResponse({ code: 'not_found', detail: 'no card card_9' }, 404));

    await expect(client(fetchMock).getCard('gnosis_pay_mock', 'card_9')).rejects.toMatchObject({
      status: 404,
      code: 'not_found',
      detail: 'no card card_9',
    });
  });

  it('survives an error body that is not the documented envelope', async () => {
    // A 502 from a proxy in front of the API is HTML, and a client that assumes
    // JSON turns an upstream outage into a parse error nobody can read.
    const fetchMock = jest
      .fn()
      .mockResolvedValue(opaqueResponse(502));

    const error = await failure(client(fetchMock).listProviders());

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.code).toBe('http_error');
  });

  it('reports a request that never reached a server as such', async () => {
    // Distinct from every HTTP status: the backend is not running, or the phone is
    // on a captive-network wifi. "Cannot reach the backend" is the only honest thing
    // to show, and a screen cannot say it if the client reports a fake 500.
    const fetchMock = jest.fn().mockRejectedValue(new TypeError('Network request failed'));

    const error = await failure(client(fetchMock).listProviders());

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBeNull();
    expect(error.code).toBe('unreachable');
  });

  it('treats a 501 from the reveal path as a capability gap, not a bug', async () => {
    // Lithic and Stripe have no reveal path the backend will use. A screen shows
    // "not available for this provider" rather than an error, so the code matters.
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse({ code: 'reveal_unsupported', detail: 'lithic has no card-reveal path' }, 501),
    );

    await expect(client(fetchMock).mintRevealToken('lithic', 'card_1')).rejects.toMatchObject({
      code: 'reveal_unsupported',
    });
  });
});

describe('timeouts', () => {
  it('gives up rather than hanging a screen forever', async () => {
    // A poll that never resolves is worse than one that fails: the screen shows a
    // spinner and the next tick queues behind it.
    const fetchMock = jest.fn().mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        }),
    );
    const impatient = new StableCardClient({
      baseUrl: BASE,
      fetch: fetchMock as unknown as typeof fetch,
      timeoutMs: 10,
    });

    const error = await failure(impatient.listProviders());

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('timeout');
  });
});

describe('paths', () => {
  const cases: Array<[string, (c: StableCardClient) => Promise<unknown>, string]> = [
    ['listProviders', (c) => c.listProviders(), '/providers'],
    ['getCard', (c) => c.getCard('p', 'c'), '/providers/p/cards/c'],
    ['getBalance', (c) => c.getBalance('p', 'c'), '/providers/p/cards/c/balance'],
    ['freezeCard', (c) => c.freezeCard('p', 'c'), '/providers/p/cards/c/freeze'],
    ['activateCard', (c) => c.activateCard('p', 'c'), '/providers/p/cards/c/activate'],
    ['mintRevealToken', (c) => c.mintRevealToken('p', 'c'), '/providers/p/cards/c/reveal-token'],
    ['redeemRevealToken', (c) => c.redeemRevealToken('t'), '/reveal'],
    ['getIntent', (c) => c.getIntent('i'), '/funding/intents/i'],
    ['pendingChallenges', (c) => c.pendingChallenges(), '/otp/pending'],
    [
      'respondToChallenge',
      (c) => c.respondToChallenge('p', 'c', 'decline'),
      '/otp/p/c/respond',
    ],
  ];

  it.each(cases)('%s hits %s', async (_name, call, path) => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse({}));

    await call(client(fetchMock)).catch(() => undefined);

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${BASE}${path}`);
  });

  it('filters intents by card without inventing a path', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse({ count: 0, intents: [] }));

    await client(fetchMock).listIntents({ cardId: 'card_1' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${BASE}/funding/intents?card_id=card_1`);
  });

  it('omits an absent filter rather than sending an empty one', async () => {
    // `?card_id=` is a filter for the empty string, which matches nothing.
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse({ count: 0, intents: [] }));

    await client(fetchMock).listIntents();

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${BASE}/funding/intents`);
  });
});

describe('the websocket URL', () => {
  it.each([
    ['http://127.0.0.1:8000', 'ws://127.0.0.1:8000/ws/otp'],
    ['https://api.example.test', 'wss://api.example.test/ws/otp'],
  ])('derives %s into %s', (base, expected) => {
    expect(new StableCardClient({ baseUrl: base }).challengeSocketUrl()).toBe(expected);
  });

  it('carries a card filter', () => {
    expect(new StableCardClient({ baseUrl: BASE }).challengeSocketUrl('card_1')).toBe(
      `ws://127.0.0.1:8000/ws/otp?card_id=card_1`,
    );
  });
});
