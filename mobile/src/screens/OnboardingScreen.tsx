/**
 * Pick a provider, get a card.
 *
 * Not in SPEC.md §9, and it is here because of what §9 leaves out: with no
 * authentication there is nothing to derive a card from, so the app has to be told
 * which one it is looking at. The choice of *how* is what makes this screen worth
 * having — the provider list comes from `GET /providers`, which is the issuer
 * registry itself (SPEC.md §3.1's "adding a new issuer = one new adapter file +
 * registry entry"). Adding a fourth adapter makes it appear here with no change to
 * this file, which is the claim the whole project rests on, made visible.
 *
 * The funding model beside each name is the other half of that claim: `fiat_rail`
 * and `crypto_deposit` are the two shapes the abstraction covers, and they behave
 * differently on the fund screen.
 */

import { useCallback, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { useClient, useQuery } from '../api/ApiProvider';
import type { Provider } from '../api/types';
import type { CardSelection } from '../session';
import { Button, ErrorNotice, Loading, Pill, Screen, Section } from '../ui/components';
import { palette, spacing, text } from '../ui/theme';

/** The one provider that needs no credentials and no network (SPEC.md §3.2). */
const PREFERRED = 'gnosis_pay_mock';

const FUNDING_MODEL_LABEL = {
  fiat_rail: 'Fiat rail',
  crypto_deposit: 'Crypto deposit',
} as const;

export function OnboardingScreen({ onCreated }: { onCreated: (s: CardSelection) => void }) {
  const client = useClient();
  const providers = useQuery<Provider[]>(() => client.listProviders(), { deps: [] });
  const [creating, setCreating] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const create = useCallback(
    async (providerId: string) => {
      setCreating(providerId);
      setError(null);
      try {
        // Two calls, because a card belongs to a cardholder at every provider here.
        // The details are demo-shaped and deliberately not collected from anyone:
        // there is no KYC in this system, and a form asking for a legal name would
        // imply otherwise.
        const holder = await client.createCardholder(providerId, {
          email: 'demo@stablecard.test',
          first_name: 'Demo',
          last_name: 'Cardholder',
        });
        const card = await client.createCard(providerId, holder.cardholder_id, {
          currency: 'USD',
          spend_limit_minor: 100_000,
        });
        onCreated({ providerId, cardId: card.card_id });
      } catch (raised) {
        setError(raised);
      } finally {
        setCreating(null);
      }
    },
    [client, onCreated],
  );

  if (providers.loading) {
    return (
      <Screen>
        <Loading label="Asking the backend which issuers are registered…" />
      </Screen>
    );
  }
  if (providers.data === null) {
    return (
      <Screen>
        <ErrorNotice error={providers.error} onRetry={providers.refresh} />
      </Screen>
    );
  }

  // The mock first: it is the only one that works with no credentials at all.
  const ordered = [...providers.data].sort((a, b) =>
    a.provider_id === PREFERRED ? -1 : b.provider_id === PREFERRED ? 1 : 0,
  );

  return (
    <Screen>
      <View style={styles.intro}>
        <Text style={text.title}>StableCard Rail</Text>
        <Text style={text.muted}>
          A sandbox card-funding pipeline. Testnets and provider sandboxes only — no mainnet
          funds and no production card programs.
        </Text>
      </View>

      {error !== null && <ErrorNotice error={error} />}

      <Section title="Create a card">
        <Text style={text.muted}>
          Every issuer below is one adapter file and one registry entry on the backend. This
          list is the registry.
        </Text>
        {ordered.map((provider) => (
          <View key={provider.provider_id} style={styles.provider}>
            <View style={styles.providerName}>
              <Text style={text.body}>{provider.provider_id}</Text>
              <Pill
                label={FUNDING_MODEL_LABEL[provider.funding_model]}
                colour={
                  provider.funding_model === 'crypto_deposit' ? palette.accent : palette.textMuted
                }
              />
            </View>
            <Button
              label={provider.provider_id === PREFERRED ? 'Create (no credentials)' : 'Create'}
              onPress={() => {
                void create(provider.provider_id);
              }}
              variant={provider.provider_id === PREFERRED ? 'primary' : 'secondary'}
              busy={creating === provider.provider_id}
              disabled={creating !== null}
              testID={`create-${provider.provider_id}`}
            />
          </View>
        ))}
      </Section>
    </Screen>
  );
}

const styles = StyleSheet.create({
  intro: { gap: spacing.sm, paddingVertical: spacing.md },
  provider: { gap: spacing.sm, paddingVertical: spacing.sm },
  providerName: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
});
