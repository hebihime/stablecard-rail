import { Redirect } from 'expo-router';

import { FundScreen } from '../src/screens/FundScreen';
import { useSession } from '../src/session';
import { Loading, Screen } from '../src/ui/components';

export default function FundRoute() {
  const { selection, ready } = useSession();

  if (!ready) {
    return (
      <Screen>
        <Loading label="Loading…" />
      </Screen>
    );
  }
  if (selection === null) {
    return <Redirect href="/" />;
  }
  return <FundScreen selection={selection} />;
}
