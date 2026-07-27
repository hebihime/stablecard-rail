/**
 * The native `CardVault` — Swift on iOS, Kotlin on Android.
 *
 * Metro picks `CardVaultModule.web.ts` over this file for a web build, so this is
 * only ever loaded where the native side exists.
 */

import { NativeModule, requireNativeModule } from 'expo';

import type { CardVault, VaultDescription } from './CardVault.types';

declare class CardVaultNativeModule extends NativeModule<Record<never, never>> {
  setItem(key: string, value: string): Promise<void>;
  getItem(key: string): Promise<string | null>;
  deleteItem(key: string): Promise<void>;
  describe(): VaultDescription;
}

// `requireNativeModule` throws when the native side is absent, which is what
// happens in Expo Go — this module cannot be loaded there, and `index.ts` catches
// that and falls back rather than letting the app fail to start.
export default requireNativeModule<CardVaultNativeModule>('CardVault') as CardVault;
