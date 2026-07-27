/**
 * The web build's `CardVault`.
 *
 * Metro resolves `.web.ts` ahead of `.ts`, so importing `./CardVaultModule` gets the
 * native module on a phone and this on a browser — one import site, three
 * implementations, and no `Platform.OS` branch in application code.
 *
 * Constructed lazily. `WebCardVault`'s constructor asks whether IndexedDB and
 * WebCrypto exist, and a module-level instance would ask that at import time, which
 * during a static web export is Node rather than a browser — and would answer for
 * the wrong environment.
 */

import type { CardVault } from './CardVault.types';
import { WebCardVault } from './webVault';

let instance: CardVault | null = null;

const lazyVault: CardVault = {
  setItem: (key, value) => vault().setItem(key, value),
  getItem: (key) => vault().getItem(key),
  deleteItem: (key) => vault().deleteItem(key),
  describe: () => vault().describe(),
};

function vault(): CardVault {
  instance ??= new WebCardVault();
  return instance;
}

export default lazyVault;
