/**
 * The browser's answer, and an honest account of how far it goes.
 *
 * A non-extractable AES-256-GCM `CryptoKey` held in IndexedDB, with ciphertext
 * beside it. IndexedDB is required rather than convenient: it is the only web store
 * that can hold a `CryptoKey` at all, because structured clone preserves the key
 * handle without ever exposing the key material to JavaScript. `localStorage` takes
 * strings, and a key you can serialize is a key you have already extracted.
 *
 * **What this does and does not defend against**, because calling it "secure
 * storage" alongside the Keychain would be overclaiming:
 *
 * - It does defend against someone who obtains the stored data — a copied profile
 *   directory, a backup, another origin. They get ciphertext and a key handle that
 *   `exportKey` refuses.
 * - It does **not** defend against script running on this origin. That script
 *   cannot read the key, but it can ask the key to decrypt, which gets it the same
 *   plaintext. XSS beats this, and nothing in a browser does not.
 *
 * That difference is why `describe()` reports `origin-scoped` here and
 * `device-keystore` on the phones, and why the reveal screen says which one it got.
 */

import type { CardVault, VaultDescription } from './CardVault.types';

const DATABASE_NAME = 'stablecard-card-vault';
const DATABASE_VERSION = 1;
const STORE_NAME = 'vault';
/** The one entry that is a key rather than a stored value. */
const KEY_ENTRY = '__aes-gcm-key__';
const GCM_IV_BYTES = 12;

interface SealedValue {
  iv: number[];
  ciphertext: number[];
}

/**
 * A vault that works, or one that admits it does not.
 *
 * `available` is checked once at construction rather than per call, so a caller can
 * ask `describe()` before deciding whether to store anything at all. A browser
 * without WebCrypto (an insecure origin — WebCrypto's `subtle` is unavailable over
 * plain HTTP on anything but localhost) or without IndexedDB (private mode in some
 * browsers) gets `protection: 'none'`, and nothing is written anywhere.
 */
export class WebCardVault implements CardVault {
  private readonly available: boolean;
  private readonly memory = new Map<string, string>();
  private keyPromise: Promise<CryptoKey> | null = null;

  constructor() {
    this.available =
      typeof indexedDB !== 'undefined' &&
      typeof globalThis.crypto?.subtle?.generateKey === 'function';
  }

  describe(): VaultDescription {
    return this.available
      ? { backend: 'webcrypto', protection: 'origin-scoped' }
      : // Named `memory` rather than reported as a working vault. A caller that
        // sees this can decline to persist a wallet key, which is the right call.
        { backend: 'memory', protection: 'none' };
  }

  async setItem(key: string, value: string): Promise<void> {
    if (!this.available) {
      this.memory.set(key, value);
      return;
    }
    const iv = crypto.getRandomValues(new Uint8Array(GCM_IV_BYTES));
    const ciphertext = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv },
      await this.key(),
      new TextEncoder().encode(value),
    );
    const sealed: SealedValue = {
      // Plain arrays, because a `Uint8Array` survives structured clone but reads
      // back as one and comparing the two shapes later is a needless trap.
      iv: Array.from(iv),
      ciphertext: Array.from(new Uint8Array(ciphertext)),
    };
    await withStore('readwrite', (store) => store.put(sealed, entryName(key)));
  }

  async getItem(key: string): Promise<string | null> {
    if (!this.available) {
      return this.memory.get(key) ?? null;
    }
    const sealed = await withStore<SealedValue | undefined>('readonly', (store) =>
      store.get(entryName(key)),
    );
    if (sealed === undefined) {
      return null;
    }
    try {
      const plaintext = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: new Uint8Array(sealed.iv) },
        await this.key(),
        new Uint8Array(sealed.ciphertext),
      );
      return new TextDecoder().decode(plaintext);
    } catch {
      // The key was replaced or the ciphertext was tampered with — GCM's tag check
      // failing is exactly the signal it exists for. Either way this value can
      // never be read again, so drop it rather than failing identically forever.
      await this.deleteItem(key);
      return null;
    }
  }

  async deleteItem(key: string): Promise<void> {
    this.memory.delete(key);
    if (this.available) {
      await withStore('readwrite', (store) => store.delete(entryName(key)));
    }
  }

  /**
   * The key, generated on first use and never again.
   *
   * Memoized on the promise rather than on the resolved key, so two calls racing on
   * a cold start cannot both generate one — the second would overwrite the first
   * and orphan everything already sealed under it.
   */
  private key(): Promise<CryptoKey> {
    this.keyPromise ??= (async () => {
      const existing = await withStore<CryptoKey | undefined>('readonly', (store) =>
        store.get(KEY_ENTRY),
      );
      if (existing !== undefined) {
        return existing;
      }
      const generated = await crypto.subtle.generateKey(
        { name: 'AES-GCM', length: 256 },
        // `false`: the whole point. `exportKey` will refuse, and the key handle can
        // be stored and used without the material ever existing in JavaScript.
        false,
        ['encrypt', 'decrypt'],
      );
      await withStore('readwrite', (store) => store.put(generated, KEY_ENTRY));
      return generated;
    })();
    return this.keyPromise;
  }
}

function entryName(key: string): string {
  // Prefixed so a caller cannot address the key entry by choosing its name.
  return `item:${key}`;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE_NAME);
    };
    request.onsuccess = () => {
      resolve(request.result);
    };
    request.onerror = () => {
      reject(request.error ?? new Error('could not open the vault database'));
    };
  });
}

/**
 * Run one operation in one transaction, and resolve when the *transaction* is done.
 *
 * Resolving on `request.onsuccess` is the tempting version and is wrong for writes:
 * a request can succeed inside a transaction that then aborts, so a `setItem` would
 * resolve for a value that was never committed. `oncomplete` is the only event that
 * means the data is there.
 */
async function withStore<T>(
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore) => IDBRequest,
): Promise<T> {
  const database = await openDatabase();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      const request = operation(transaction.objectStore(STORE_NAME));
      let result: T;
      request.onsuccess = () => {
        result = request.result as T;
      };
      transaction.oncomplete = () => {
        resolve(result);
      };
      transaction.onabort = transaction.onerror = () => {
        reject(transaction.error ?? new Error('the vault transaction failed'));
      };
    });
  } finally {
    database.close();
  }
}
