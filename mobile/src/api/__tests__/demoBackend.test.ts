/**
 * The recorded backend behind `EXPO_PUBLIC_DEMO=1`.
 *
 * Driven through the real `StableCardClient`, because that is how the app uses it.
 * What is asserted is the honesty of the thing: that its shapes are the ones the
 * screens consume, that its refusals are the real refusals, and that its timeline
 * advances rather than sitting on a fixed screenshot.
 */

import { StableCardClient } from '../client';
import { demoFetch } from '../demoBackend';

function demoClient(clock: { at: number }) {
  return new StableCardClient({
    baseUrl: 'https://demo.invalid',
    fetch: demoFetch({ now: () => clock.at }),
  });
}

describe('the demo backend', () => {
  it('lists the three registered providers', async () => {
    const clock = { at: 0 };

    const providers = await demoClient(clock).listProviders();

    expect(providers.map((p) => p.provider_id)).toEqual([
      'gnosis_pay_mock',
      'lithic',
      'stripe_issuing',
    ]);
  });

  it('creates a card that reads back', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    const holder = await client.createCardholder('gnosis_pay_mock', {
      email: 'a@b.test',
      first_name: 'A',
      last_name: 'B',
    });

    const card = await client.createCard('gnosis_pay_mock', holder.cardholder_id, {
      currency: 'USD',
    });

    expect((await client.getCard('gnosis_pay_mock', card.card_id)).last_four).toBe('4242');
  });

  it('refuses a reveal at the providers that really refuse one', async () => {
    // Not a convenience. Faking a Lithic reveal here would advertise a capability
    // the backend deliberately does not have (docs/ARCHITECTURE.md §12.2).
    const clock = { at: 0 };

    await expect(demoClient(clock).mintRevealToken('lithic', 'card_demo')).rejects.toMatchObject({
      code: 'reveal_unsupported',
    });
  });

  it('advances the funding intent as time passes', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');

    const first = (await client.listIntents()).intents[0];
    clock.at += 10_000;
    const later = (await client.listIntents()).intents[0];

    expect(first?.state).toBe('PENDING');
    expect(later?.progress.position).toBeGreaterThan(first!.progress.position!);
  });

  it('settles rather than looping forever', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');

    clock.at += 60_000;
    const intent = (await client.listIntents()).intents[0];

    expect(intent?.state).toBe('SETTLED');
    expect(intent?.progress.is_terminal).toBe(true);
  });

  it('shows the bridge fee as a difference, as the real one does', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');
    clock.at += 60_000;

    const intent = (await client.listIntents()).intents[0];

    expect(intent?.amount_minor).toBe(100_000);
    expect(intent?.bridged_amount_minor).toBe(99_700);
  });

  it('credits the card only once the intent reaches FUNDED', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.createCard('gnosis_pay_mock', 'user_demo', { currency: 'USD' });
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');

    const before = await client.getBalance('gnosis_pay_mock', 'card_demo');
    clock.at += 60_000;
    const after = await client.getBalance('gnosis_pay_mock', 'card_demo');

    expect(before.amount_minor).toBe(0);
    expect(after.amount_minor).toBe(99_700);
  });

  it('raises a 3DS challenge on its own, without being asked', async () => {
    // The thing worth demonstrating is a challenge *arriving*. A button marked
    // "simulate a challenge" would show the modal and hide the point.
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');

    expect((await client.pendingChallenges()).count).toBe(0);
    clock.at += 60_000;
    expect((await client.pendingChallenges()).count).toBe(1);
  });

  it('stops showing a challenge once it is answered', async () => {
    const clock = { at: 0 };
    const client = demoClient(clock);
    await client.claimDepositRoute('gnosis_pay_mock', 'card_demo');
    clock.at += 60_000;

    await client.respondToChallenge('gnosis_pay_mock', '3ds_demo_1', 'approve');

    expect((await client.pendingChallenges()).count).toBe(0);
  });

  it('answers an unknown path the way the real backend would', async () => {
    // So a screen reaching for something unimplemented fails the same way it would
    // against a real backend, rather than hanging or returning undefined.
    const clock = { at: 0 };

    await expect(demoClient(clock).getIntent('nope')).rejects.toMatchObject({
      status: 404,
      code: 'not_found',
    });
  });
});
