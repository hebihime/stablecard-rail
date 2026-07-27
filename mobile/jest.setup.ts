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

if (globalThis.crypto === undefined) {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
}
