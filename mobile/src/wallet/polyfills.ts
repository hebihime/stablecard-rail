/**
 * The two globals `@solana/web3.js` assumes and React Native does not have.
 *
 * Imported for effect, first, before anything that might reach for either. Both are
 * genuinely absent rather than merely different: Hermes has no `Buffer` at all, and
 * `crypto.getRandomValues` is a browser API that React Native does not implement.
 *
 * The randomness one matters more than it looks. A polyfill that returned anything
 * predictable would produce guessable keypairs, and it would work — every test would
 * pass, transactions would sign, and the wallet would be worthless.
 * `react-native-get-random-values` is backed by the platform CSPRNG
 * (`SecRandomCopyBytes` on iOS, `SecureRandom` on Android), which is the only
 * acceptable source. On web the browser already provides it and this import is a
 * no-op.
 */

import 'react-native-get-random-values';
import { Buffer as NodeBuffer } from 'buffer';

// Assigned through a widened view of `globalThis` rather than a `declare global`:
// declaring `var Buffer: typeof import('buffer').Buffer` makes the type refer to
// itself once the global exists, which TypeScript reports as a circularity.
const scope = globalThis as { Buffer?: typeof NodeBuffer };
scope.Buffer ??= NodeBuffer;
