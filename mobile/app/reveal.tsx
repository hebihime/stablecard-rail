import { Redirect } from 'expo-router';

import { RevealScreen } from '../src/screens/RevealScreen';
import { useSession } from '../src/session';
import { Loading, Screen } from '../src/ui/components';

export default function RevealRoute() {
  const { selection, ready } = useSession();

  if (!ready) {
    return (
      <Screen>
        <Loading label="Loading…" />
      </Screen>
    );
  }
  // Reachable directly in the web build, where these are real URLs someone can
  // type or bookmark — so a card has to be selected before the screen means
  // anything, and the honest answer is to send them to the one that selects it.
  if (selection === null) {
    return <Redirect href="/" />;
  }
  return <RevealScreen selection={selection} />;
}
