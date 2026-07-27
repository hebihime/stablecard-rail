/**
 * A vault that stores nothing durably and says so.
 *
 * Reached in exactly one situation that matters: the app running in Expo Go, where
 * a custom native module does not exist and `requireNativeModule('CardVault')`
 * throws. Rather than failing to start, the app runs with this and every screen
 * that touches a secret can see `protection: 'none'` and behave accordingly.
 *
 * The important property is that it is *loud*. A silent in-memory fallback is how a
 * demo ends up appearing to persist a wallet key that evaporates on reload, and how
 * a reviewer concludes the Keychain work was never done. `describe()` is the whole
 * reason this class is not just a `Map`.
 */

import type { CardVault, VaultDescription } from './CardVault.types';

export class MemoryCardVault implements CardVault {
  private readonly entries = new Map<string, string>();

  describe(): VaultDescription {
    return { backend: 'memory', protection: 'none' };
  }

  async setItem(key: string, value: string): Promise<void> {
    this.entries.set(key, value);
  }

  async getItem(key: string): Promise<string | null> {
    return this.entries.get(key) ?? null;
  }

  async deleteItem(key: string): Promise<void> {
    this.entries.delete(key);
  }
}
