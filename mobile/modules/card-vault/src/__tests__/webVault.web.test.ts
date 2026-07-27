/**
 * The browser backend, against a real IndexedDB and a real WebCrypto.
 *
 * `fake-indexeddb` is a full implementation of the spec rather than a stub, and it
 * preserves a `CryptoKey` through structured clone — which is the property the
 * whole design rests on, so testing against it tests the thing that matters.
 *
 * What is asserted here is deliberately not "a value goes in and comes out". It is
 * the four claims the docstring in `webVault.ts` makes: the key cannot be exported,
 * ciphertext is what is stored, a tampered value fails closed, and a browser
 * without the primitives says so rather than pretending.
 *
 * The Swift and Kotlin implementations have no equivalent here. Jest cannot run
 * them, and this repo has no XCTest or JUnit harness — see the phase-8 section of
 * docs/ARCHITECTURE.md, which says plainly which parts of this module are verified
 * and which are only reviewed.
 */

import 'fake-indexeddb/auto';

import { WebCardVault } from '../webVault';

/**
 * Read the raw stored record, going around the vault's own API.
 *
 * The point of several tests below is what is *on disk*, and asking the vault would
 * answer with what it can decrypt — which is the opposite question.
 */
async function openVaultDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('stablecard-card-vault', 1);
    // The store may not exist yet — a test that reads before the vault has ever
    // written would otherwise fail with `NotFoundError` from `transaction()`,
    // which reads as a vault bug rather than as an empty database.
    request.onupgradeneeded = () => {
      request.result.createObjectStore('vault');
    };
    request.onsuccess = () => {
      resolve(request.result);
    };
    request.onerror = () => {
      reject(request.error);
    };
  });
}

async function rawEntry(name: string): Promise<unknown> {
  const database = await openVaultDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction('vault', 'readonly');
      const query = transaction.objectStore('vault').get(name);
      query.onsuccess = () => {
        resolve(query.result);
      };
      query.onerror = () => {
        reject(query.error);
      };
    });
  } finally {
    database.close();
  }
}

/** Write a record directly, bypassing the vault — for the tampering tests. */
async function putRawEntry(name: string, value: unknown): Promise<void> {
  const database = await openVaultDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction('vault', 'readwrite');
      transaction.objectStore('vault').put(value, name);
      transaction.oncomplete = () => {
        resolve();
      };
      transaction.onabort = transaction.onerror = () => {
        reject(transaction.error);
      };
    });
  } finally {
    database.close();
  }
}

describe('WebCardVault', () => {
  let vault: WebCardVault;

  beforeEach(async () => {
    await new Promise<void>((resolve) => {
      const request = indexedDB.deleteDatabase('stablecard-card-vault');
      request.onsuccess = request.onerror = request.onblocked = () => {
        resolve();
      };
    });
    vault = new WebCardVault();
  });

  it('stores and returns a value', async () => {
    await vault.setItem('token', 'reveal-abc');

    expect(await vault.getItem('token')).toBe('reveal-abc');
  });

  it('reports what it is, without borrowing the phones’ word for it', async () => {
    // `origin-scoped`, not `device-keystore`. A browser cannot offer the latter and
    // saying it could is the overclaim this whole type exists to prevent.
    expect(vault.describe()).toEqual({ backend: 'webcrypto', protection: 'origin-scoped' });
  });

  it('returns null for a key it has never seen', async () => {
    // The cold-start path, taken on every first load. Not an error.
    expect(await vault.getItem('never-set')).toBeNull();
  });

  it('overwrites rather than keeping the first value', async () => {
    // The iOS implementation needs explicit code for this (`SecItemAdd` refuses a
    // duplicate); asserting it here keeps the platforms honest about agreeing.
    await vault.setItem('token', 'first');
    await vault.setItem('token', 'second');

    expect(await vault.getItem('token')).toBe('second');
  });

  it('forgets a deleted value', async () => {
    await vault.setItem('token', 'reveal-abc');
    await vault.deleteItem('token');

    expect(await vault.getItem('token')).toBeNull();
  });

  it('deletes something absent without complaining', async () => {
    await expect(vault.deleteItem('never-set')).resolves.toBeUndefined();
  });

  it('keeps two keys apart', async () => {
    await vault.setItem('a', 'alpha');
    await vault.setItem('b', 'beta');

    expect(await vault.getItem('a')).toBe('alpha');
    expect(await vault.getItem('b')).toBe('beta');
  });

  it('round-trips a value that is not ASCII', async () => {
    // The wallet secret is base58 and the reveal token is base64url, but a
    // TextEncoder bug would be invisible against either.
    const awkward = 'ключ — 秘密 — 🔑';
    await vault.setItem('token', awkward);

    expect(await vault.getItem('token')).toBe(awkward);
  });

  describe('what is actually on disk', () => {
    it('is ciphertext, not the value', async () => {
      await vault.setItem('token', 'reveal-abc');

      const stored = JSON.stringify(await rawEntry('item:token'));
      expect(stored).not.toContain('reveal-abc');
    });

    it('carries a fresh IV for every write', async () => {
      // Reusing an IV under one key breaks AES-GCM completely, and it is exactly
      // the mistake a "generate it once and keep it" implementation makes.
      await vault.setItem('a', 'same value');
      const first = (await rawEntry('item:a')) as { iv: number[] };
      await vault.setItem('b', 'same value');
      const second = (await rawEntry('item:b')) as { iv: number[] };

      expect(first.iv).toHaveLength(12);
      expect(first.iv).not.toEqual(second.iv);
    });

    it('encrypts the same plaintext to different ciphertext', async () => {
      await vault.setItem('a', 'same value');
      const first = (await rawEntry('item:a')) as { ciphertext: number[] };
      await vault.setItem('b', 'same value');
      const second = (await rawEntry('item:b')) as { ciphertext: number[] };

      expect(first.ciphertext).not.toEqual(second.ciphertext);
    });
  });

  describe('the key', () => {
    it('cannot be exported, which is the whole point', async () => {
      await vault.setItem('token', 'reveal-abc');

      const key = (await rawEntry('__aes-gcm-key__')) as CryptoKey;
      expect(key.extractable).toBe(false);
      await expect(crypto.subtle.exportKey('raw', key)).rejects.toThrow();
    });

    it('is generated once, so a second vault reads what the first wrote', async () => {
      // Two page loads, one origin. A vault that generated a fresh key per instance
      // would lose everything on every reload while appearing to work within a session.
      await vault.setItem('token', 'reveal-abc');

      expect(await new WebCardVault().getItem('token')).toBe('reveal-abc');
    });

    it('is not regenerated by concurrent cold reads', async () => {
      // Two calls racing before any key exists. Memoizing the resolved key rather
      // than the promise lets both generate one, and the loser's writes are
      // orphaned — a bug that only appears under load and looks like data loss.
      const [a, b] = await Promise.all([
        vault.setItem('a', 'alpha'),
        vault.setItem('b', 'beta'),
      ]).then(async () => Promise.all([vault.getItem('a'), vault.getItem('b')]));

      expect([a, b]).toEqual(['alpha', 'beta']);
    });
  });

  describe('failing closed', () => {
    it('returns null for a value whose authentication tag does not check out', async () => {
      // GCM's tag is what makes tampering detectable, and this is the assertion
      // that it is actually being checked rather than merely present.
      await vault.setItem('token', 'reveal-abc');
      const sealed = (await rawEntry('item:token')) as { iv: number[]; ciphertext: number[] };
      const tampered = { ...sealed, ciphertext: [...sealed.ciphertext] };
      tampered.ciphertext[0] = (tampered.ciphertext[0]! ^ 0xff) & 0xff;
      await putRawEntry('item:token', tampered);

      expect(await vault.getItem('token')).toBeNull();
    });

    it('drops an unreadable value rather than failing on it forever', async () => {
      await vault.setItem('token', 'reveal-abc');
      await putRawEntry('item:token', { iv: Array(12).fill(0), ciphertext: [1, 2, 3] });

      expect(await vault.getItem('token')).toBeNull();
      expect(await rawEntry('item:token')).toBeUndefined();
    });
  });
});

describe('a browser without the primitives', () => {
  it('says protection is none rather than pretending', () => {
    // WebCrypto's `subtle` is unavailable on an insecure origin, and IndexedDB is
    // unavailable in some private-browsing modes. A caller that sees `none` can
    // decline to persist a wallet key, which is the right decision to leave open.
    const subtle = globalThis.crypto.subtle;
    Object.defineProperty(globalThis.crypto, 'subtle', { value: undefined, configurable: true });
    try {
      expect(new WebCardVault().describe()).toEqual({ backend: 'memory', protection: 'none' });
    } finally {
      Object.defineProperty(globalThis.crypto, 'subtle', { value: subtle, configurable: true });
    }
  });

  it('still works within the session, so a screen is not broken by it', async () => {
    const subtle = globalThis.crypto.subtle;
    Object.defineProperty(globalThis.crypto, 'subtle', { value: undefined, configurable: true });
    try {
      const degraded = new WebCardVault();
      await degraded.setItem('token', 'reveal-abc');
      expect(await degraded.getItem('token')).toBe('reveal-abc');
      await degraded.deleteItem('token');
      expect(await degraded.getItem('token')).toBeNull();
    } finally {
      Object.defineProperty(globalThis.crypto, 'subtle', { value: subtle, configurable: true });
    }
  });
});
