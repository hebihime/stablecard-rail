/**
 * The facade, and the fallback nobody should ever be surprised by.
 *
 * Runs on all three platforms, because the contract it checks is the one every
 * backend has to honour. The platform-specific behaviour lives in
 * `webVault.web.test.ts` and — for Swift and Kotlin — in a review rather than a
 * test, which docs/ARCHITECTURE.md §12.9 states rather than glosses over.
 */

import {
  __setCardVaultForTesting,
  VAULT_KEYS,
  cardVault,
  vaultDescription,
} from '../../index';
import type { CardVault } from '../CardVault.types';
import { MemoryCardVault } from '../memoryVault';

afterEach(() => {
  __setCardVaultForTesting(null);
});

describe('MemoryCardVault', () => {
  let vault: MemoryCardVault;

  beforeEach(() => {
    vault = new MemoryCardVault();
  });

  it('says it protects nothing, which is the only reason it is allowed to exist', () => {
    // A silent in-memory fallback is how a demo appears to persist a wallet key
    // that evaporates on reload, and how a reviewer concludes the Keychain work was
    // never done. This method is the whole point of the class.
    expect(vault.describe()).toEqual({ backend: 'memory', protection: 'none' });
  });

  it('honours the same contract as the real ones', async () => {
    expect(await vault.getItem('absent')).toBeNull();
    await vault.setItem('k', 'v');
    expect(await vault.getItem('k')).toBe('v');
    await vault.setItem('k', 'v2');
    expect(await vault.getItem('k')).toBe('v2');
    await vault.deleteItem('k');
    expect(await vault.getItem('k')).toBeNull();
    await expect(vault.deleteItem('absent')).resolves.toBeUndefined();
  });
});

describe('the keys this app stores on a device', () => {
  it('are exactly two, and neither is a card number', () => {
    // Named centrally so there is one place to see everything kept on a device.
    // The absence of a PAN here is not an oversight: the backend has none to give
    // (docs/ARCHITECTURE.md §12.2).
    expect(Object.values(VAULT_KEYS)).toEqual(['wallet.secret-key', 'reveal.token']);
  });

  it('are namespaced, so two purposes cannot collide', () => {
    for (const key of Object.values(VAULT_KEYS)) {
      expect(key).toMatch(/^[a-z-]+\.[a-z-]+$/);
    }
  });
});

describe('cardVault', () => {
  it('returns the same instance every time', () => {
    // The web backend memoizes its encryption key per instance; a fresh vault per
    // call would work and would open a database connection per operation.
    expect(cardVault()).toBe(cardVault());
  });

  it('reports the protection the platform actually gives', () => {
    // Whatever backend this platform resolved to, `describe()` must agree with it —
    // a screen decides what to tell the user from this and nothing else.
    const description = vaultDescription();
    expect(['device-keystore', 'origin-scoped', 'none']).toContain(description.protection);
    expect(description.backend).not.toBe('');
  });

  it('can be replaced for a test, and put back', async () => {
    const fake: CardVault = new MemoryCardVault();
    __setCardVaultForTesting(fake);

    expect(cardVault()).toBe(fake);

    __setCardVaultForTesting(null);
    expect(cardVault()).not.toBe(fake);
  });
});
