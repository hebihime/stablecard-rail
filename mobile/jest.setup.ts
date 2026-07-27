/**
 * Test-environment setup, applied to every platform project.
 *
 * Nothing here fakes application behaviour. It supplies the two things a bare JSDOM
 * or Node environment lacks and the platform itself would otherwise provide.
 */

// `@solana/web3.js` needs `crypto.getRandomValues` to generate a keypair. In the
// app that comes from `react-native-get-random-values` (native) or the browser
// (web); under Jest neither is present, so Node's own implementation stands in.
// Deliberately the *real* one rather than a deterministic stub: a test that passes
// against predictable "randomness" would say nothing about key generation.
import { webcrypto } from 'node:crypto';

// `subtle` and not just `crypto`: JSDOM supplies a `crypto` object with
// `getRandomValues` and no `subtle` at all, so testing for the outer object would
// leave the vault's own availability check answering "no primitives here" and every
// encryption test asserting the fallback path instead of the real one.
if (globalThis.crypto?.subtle === undefined) {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
}

// `structuredClone`, for the same reason and with a wrinkle worth recording.
//
// IndexedDB stores values by structured clone, and that is exactly why the web
// vault can hold a non-extractable `CryptoKey` at all — the handle survives, the key
// material never becomes a JavaScript value. JSDOM's global does not expose
// `structuredClone`, so `fake-indexeddb` throws on the first write.
//
// The real implementation is nonetheless *present*: Jest's JSDOM environment builds
// its global object inside Node's own V8 context, so the function exists on the
// context while being absent from the global the tests see. `runInThisContext`
// reaches it. That matters rather than being a trick — a hand-written shim would
// have to decide what to do with a `CryptoKey`, and the test asserting that keys
// survive IndexedDB would then be asserting the shim's behaviour instead of the
// platform's.
// `TextEncoder`/`TextDecoder`, absent from JSDOM's global for the same reason.
// Node's are the same implementation a browser has, so this changes no behaviour.
import { TextDecoder, TextEncoder } from 'node:util';

for (const [name, value] of [
  ['TextEncoder', TextEncoder],
  ['TextDecoder', TextDecoder],
] as const) {
  if ((globalThis as Record<string, unknown>)[name] === undefined) {
    Object.defineProperty(globalThis, name, { value, configurable: true });
  }
}

if ((globalThis as { structuredClone?: unknown }).structuredClone === undefined) {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const vm = require('node:vm') as typeof import('node:vm');
  Object.defineProperty(globalThis, 'structuredClone', {
    value: vm.runInThisContext('structuredClone'),
    configurable: true,
  });
}
