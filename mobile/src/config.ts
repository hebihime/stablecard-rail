/**
 * Where the app points, and what it does when it points nowhere.
 *
 * One bundle serves three situations, and the difference between them is a single
 * environment variable read at build time:
 *
 * - **`npx expo start`** against a backend on this machine. `EXPO_PUBLIC_API_URL` is
 *   unset, so the local default applies.
 * - **A deployed web build** with `EXPO_PUBLIC_API_URL` pointing at a hosted API.
 * - **A deployed web build with `EXPO_PUBLIC_DEMO=1`** and no backend at all, which
 *   is the Vercel link a stranger clicks. The app runs against recorded fixtures.
 *   Explicit rather than inferred from a failed request: a demo that silently
 *   appears when the backend is merely *down* would show fabricated balances to
 *   someone who thinks they are looking at real ones.
 *
 * `EXPO_PUBLIC_` is not a namespace choice — Expo inlines exactly that prefix into
 * the bundle at build time. Anything here is public by construction, which is why
 * no credential may ever be read through this file.
 */

/** The default when nothing is configured: a backend on this machine. */
export const LOCAL_API_URL = 'http://127.0.0.1:8000';

export interface AppConfig {
  /** Absolute base URL of the API. Meaningless, and unused, in demo mode. */
  apiBaseUrl: string;
  /** True when the app runs on recorded fixtures with no backend behind it. */
  demoMode: boolean;
}

/**
 * `127.0.0.1` rather than `localhost` for the default.
 *
 * Not interchangeable, and this repo has already paid for the difference once: a
 * stale container listening on the IPv6 wildcard answered `localhost` while the
 * host process had IPv4, so requests reached the wrong server and the wrong server
 * looked plausible (docs/ARCHITECTURE.md §9, WORKLOG traps). `localhost` resolves
 * `::1` first on macOS; the loopback address does not.
 */
export function resolveConfig(env: Record<string, string | undefined> = process.env): AppConfig {
  const demoMode = env.EXPO_PUBLIC_DEMO === '1';
  const configured = env.EXPO_PUBLIC_API_URL?.trim();
  return {
    apiBaseUrl: configured !== undefined && configured !== '' ? configured : LOCAL_API_URL,
    demoMode,
  };
}
