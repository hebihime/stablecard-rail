/**
 * The app shell: providers, and a navigator.
 *
 * `expo-router` rather than a navigator configured in code, for one reason that
 * only matters because of jp's call at the phase-8 boundary: the same routes become
 * real URLs in the web build, so the deployed demo has `/fund` and `/reveal` to
 * link to rather than one opaque page.
 */

import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';

import { ApiProvider } from '../src/api/ApiProvider';
import { SessionProvider } from '../src/session';
import { palette } from '../src/ui/theme';

export default function RootLayout() {
  return (
    <ApiProvider>
      <SessionProvider>
        <StatusBar style="light" />
        <Stack
          screenOptions={{
            headerStyle: { backgroundColor: palette.background },
            headerTintColor: palette.text,
            headerShadowVisible: false,
            contentStyle: { backgroundColor: palette.background },
          }}
        >
          <Stack.Screen name="index" options={{ title: 'Card' }} />
        </Stack>
      </SessionProvider>
    </ApiProvider>
  );
}
