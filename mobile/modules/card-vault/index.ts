/**
 * `CardVault` — secure storage, with one interface over three real backends.
 *
 * SPEC.md §9's native touchpoint. What the app imports is this file; what it gets
 * is the iOS Keychain, the Android Keystore, a non-extractable WebCrypto key in
 * IndexedDB, or — only in Expo Go, where custom native code cannot exist — an
 * in-memory stub that reports itself as one.
 *
 * The keys it holds are named here rather than by callers, so there is one place to
 * see everything this app keeps on a device. There are two, and neither is a card
 * number: the backend has no PAN to give out (docs/ARCHITECTURE.md §12.2).
 */

import type { CardVault, VaultDescription, VaultProtection } from './src/CardVault.types';
import { MemoryCardVault } from './src/memoryVault';

export type { CardVault, VaultDescription, VaultProtection };

/**
 * Everything this app stores on a device, in one list.
 *
 * `reveal-token` is short-lived by construction and is kept only across the moment
 * between minting and exchanging it — long enough to survive the app being
 * backgrounded mid-reveal, and expired at the server sixty seconds later regardless.
 */
export const VAULT_KEYS = {
  /** The demo wallet's secret key (SPEC.md §9.3). The one durable secret here. */
  walletSecret: 'wallet.secret-key',
  /** The most recent reveal token (SPEC.md §9.2). Worthless once spent. */
  revealToken: 'reveal.token',
  /**
   * The `(provider_id, card_id)` pair the app is looking at.
   *
   * Not a secret, and kept here anyway. This system has no authentication, so
   * there is no session to derive a card from — the selection is device state, and
   * the vault is the only key-value store this app has that works identically on
   * all three platforms. The alternative is a second storage dependency for one
   * string. See `src/session.tsx`.
   */
  selectedCard: 'session.selected-card',
} as const;

/**
 * Load the platform's implementation, or fall back and be honest about it.
 *
 * `requireNativeModule` throws when the native side is absent. That is not an
 * error case to log and continue past — it is precisely what running in Expo Go
 * looks like, and the app must start there. The fallback reports
 * `protection: 'none'`, so a screen can tell the user what it is actually getting
 * rather than implying a Keychain that is not present.
 */
function load(): CardVault {
  try {
    // Required lazily: a static import would be evaluated before the try block
    // could catch anything it throws.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const loaded = require('./src/CardVaultModule') as { default: CardVault };
    return loaded.default;
  } catch {
    return new MemoryCardVault();
  }
}

let cached: CardVault | null = null;

/** The vault for this platform. Loaded once, on first use. */
export function cardVault(): CardVault {
  cached ??= load();
  return cached;
}

/**
 * What kind of protection this device is actually giving.
 *
 * Exposed so screens can say so. A reveal screen that stores a token under
 * `protection: 'none'` should say "not stored securely on this platform" rather
 * than nothing, and the fund screen refuses to persist a wallet key at all.
 */
export function vaultDescription(): VaultDescription {
  return cardVault().describe();
}

/** For tests, and for nothing else: replace the platform implementation. */
export function __setCardVaultForTesting(vault: CardVault | null): void {
  cached = vault;
}
