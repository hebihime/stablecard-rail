/**
 * The first screen: a card, or the means to get one.
 *
 * Three states rather than two, and the third is the one worth having. `ready`
 * distinguishes "no card selected" from "the stored selection has not been read
 * yet" — without it, every cold start flashes the onboarding screen at someone who
 * already has a card.
 */

import { useRouter } from 'expo-router';
import { useCallback } from 'react';

import { CardScreen } from '../src/screens/CardScreen';
import { OnboardingScreen } from '../src/screens/OnboardingScreen';
import type { CardSelection } from '../src/session';
import { useSession } from '../src/session';
import { Loading, Screen } from '../src/ui/components';

export default function CardRoute() {
  const router = useRouter();
  const { selection, ready, select, forget } = useSession();

  const onCreated = useCallback(
    (created: CardSelection) => {
      void select(created);
    },
    [select],
  );

  if (!ready) {
    return (
      <Screen>
        <Loading label="Loading…" />
      </Screen>
    );
  }
  if (selection === null) {
    return <OnboardingScreen onCreated={onCreated} />;
  }
  return (
    <CardScreen
      selection={selection}
      onOpenReveal={() => {
        router.push('/reveal');
      }}
      onOpenFund={() => {
        router.push('/fund');
      }}
      onForget={() => {
        void forget();
      }}
    />
  );
}
