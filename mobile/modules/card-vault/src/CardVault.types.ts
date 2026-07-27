/**
 * One interface, three genuinely different protections (SPEC.md §9).
 *
 * SPEC.md §9 asks for "at least one small native-module touchpoint (e.g. secure
 * storage via Keychain/Keystore for the reveal token) so 'native module experience'
 * is honestly demonstrable". This is it, and the honesty runs both ways: the module
 * is real Swift and real Kotlin talking to real OS primitives, *and* it reports
 * which protection a given platform actually gives, because they are not the same.
 */

/**
 * What is actually guarding the value, in the only terms that matter: who could
 * get at it, and what they would need.
 */
export type VaultProtection =
  /**
   * An OS keystore. The secret is held by the system, released to this app alone,
   * and is not readable by another app or by anything that reads the filesystem.
   * The iOS Keychain and the Android Keystore both land here.
   */
  | 'device-keystore'
  /**
   * A key the page cannot export, holding data the page can still decrypt.
   *
   * The browser's honest ceiling, and deliberately *not* described as equivalent
   * to the above. A non-extractable `CryptoKey` cannot be exfiltrated — that is a
   * real property — but any script running on this origin can ask it to decrypt.
   * It defends against a stolen database, not against a script on the page.
   */
  | 'origin-scoped'
  /**
   * Nothing. The value lives in memory for the life of the process.
   *
   * Only reached when a platform offers neither of the above, which for this app
   * means a browser without WebCrypto or IndexedDB. Reported rather than silently
   * substituted, so a screen can decline to store anything at all.
   */
  | 'none';

export interface VaultDescription {
  /** The implementation in use: `keychain`, `keystore`, `webcrypto`, `memory`. */
  backend: string;
  protection: VaultProtection;
}

/**
 * The surface every platform implements.
 *
 * Values are strings, keys are opaque and namespaced by the caller. Nothing here
 * takes a card number, because nothing in this system has one — what this holds is
 * a reveal token (SPEC.md §9.2) and the demo wallet's secret key (§9.3).
 */
export interface CardVault {
  setItem(key: string, value: string): Promise<void>;
  /** `null` for a key never set, or one whose value has been deleted. */
  getItem(key: string): Promise<string | null>;
  deleteItem(key: string): Promise<void>;
  describe(): VaultDescription;
}
