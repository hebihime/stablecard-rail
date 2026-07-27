/**
 * Jest, on jest-expo's multi-project preset.
 *
 * Every suite runs once per platform, which is the point rather than thoroughness
 * theatre: `modules/card-vault` has three implementations behind one interface, and
 * a shared test that only ever ran on one of them would prove nothing about the
 * other two.
 *
 * The naming convention is Metro's, reused here so a test lives next to the
 * implementation it covers and runs only where that implementation exists:
 *
 *   `foo.test.ts`      — every platform. Shared code, and the facade.
 *   `foo.web.test.ts`  — the web project only. IndexedDB, WebCrypto.
 *   `foo.native.test.ts` — iOS and Android only.
 *
 * Component tests are all `.native.`, and not because the web build is untested by
 * choice. `@testing-library/react-native` renders through React Native's own test
 * renderer and asserts on RN host components; under the web preset the same
 * components render as DOM nodes and every query fails on a mismatch that says
 * nothing about the app. The screens are one codebase — what the web build adds is
 * `react-native-web`'s rendering, which a DOM-shaped copy of these tests would
 * exercise without asserting anything the native ones do not. The web-specific code
 * that does exist (`card-vault`'s WebCrypto backend) has its own `.web.` suite.
 *
 * Neither Swift nor Kotlin is covered by any of this. Jest cannot run them, and
 * this repo has no XCTest or JUnit harness — docs/ARCHITECTURE.md §12.9 says
 * exactly which parts of the native module are verified and which are reviewed.
 */
const nativeOnly = ['/node_modules/', '\\.web\\.test\\.[jt]sx?$'];
const webOnly = ['/node_modules/', '\\.native\\.test\\.[jt]sx?$'];

/**
 * Packages Jest must transform rather than skip.
 *
 * `node_modules` is untransformed by default, which is right for the thousands of
 * CommonJS packages and wrong for `@solana/*` and its dependencies: they ship ESM
 * only, so Jest reaches an `import` statement in a file it decided not to compile
 * and reports a syntax error inside somebody else's package.
 *
 * The preset's own pattern is *extended*, not replaced. Writing a fresh one is the
 * obvious move and breaks React Native itself — `expo-modules-core` ships
 * TypeScript sources and stops loading the moment it falls outside the exception
 * list. So this reads jest-expo's list and inserts into its negative lookahead.
 */
function withEsmPackages(patterns, packages) {
  const [nodeModules, ...rest] = patterns;
  return [nodeModules.replace('(?!(', `(?!(${packages.join('|')}|`), ...rest];
}

/**
 * Teach the preset's Babel transform about `.mjs`.
 *
 * Its pattern is `\.[jt]sx?$`, which covers everything React Native ships and none
 * of the `.mjs` and `.native.mjs` files `@solana/web3.js`'s dependencies are
 * published as. Without this they land in the "no transform" bucket no matter what
 * `transformIgnorePatterns` says, and fail with the same syntax error — which is a
 * genuinely confusing pairing, because the obvious fix looks like it is already
 * applied.
 */
function withMjs(transform) {
  const babel = transform['\\.[jt]sx?$'];
  return { ...transform, '\\.mjs$': babel };
}

const ESM_ONLY = ['@solana', 'jayson', 'superstruct', 'rpc-websockets', 'uuid'];

/**
 * Packages whose `exports` map Jest's resolver cannot follow.
 *
 * `rpc-websockets` publishes CommonJS at `dist/index.cjs` and declares it only
 * through `exports`, which Node honours and Jest's resolver does not — so
 * `@solana/web3.js` fails to load with "cannot find module" for a package that is
 * plainly installed. Pointing at the file Node itself resolves is the whole fix.
 */
const RESOLVER_GAPS = {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  '^rpc-websockets$': require.resolve('rpc-websockets'),
};

module.exports = {
  projects: [
    {
      preset: 'jest-expo/ios',
      transformIgnorePatterns: withEsmPackages(
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        require('jest-expo/ios/jest-preset').transformIgnorePatterns,
        ESM_ONLY,
      ),
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      transform: withMjs(require('jest-expo/ios/jest-preset').transform),
      moduleNameMapper: {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/ios/jest-preset').moduleNameMapper,
        ...RESOLVER_GAPS,
      },
      moduleFileExtensions: [
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/ios/jest-preset').moduleFileExtensions,
        'mjs',
      ],
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: nativeOnly,
    },
    {
      preset: 'jest-expo/android',
      transformIgnorePatterns: withEsmPackages(
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        require('jest-expo/android/jest-preset').transformIgnorePatterns,
        ESM_ONLY,
      ),
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      transform: withMjs(require('jest-expo/android/jest-preset').transform),
      moduleNameMapper: {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/android/jest-preset').moduleNameMapper,
        ...RESOLVER_GAPS,
      },
      moduleFileExtensions: [
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/android/jest-preset').moduleFileExtensions,
        'mjs',
      ],
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: nativeOnly,
    },
    {
      preset: 'jest-expo/web',
      transformIgnorePatterns: withEsmPackages(
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        require('jest-expo/web/jest-preset').transformIgnorePatterns,
        ESM_ONLY,
      ),
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      transform: withMjs(require('jest-expo/web/jest-preset').transform),
      moduleNameMapper: {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/web/jest-preset').moduleNameMapper,
        ...RESOLVER_GAPS,
      },
      moduleFileExtensions: [
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        ...require('jest-expo/web/jest-preset').moduleFileExtensions,
        'mjs',
      ],
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: webOnly,
    },
  ],
  collectCoverageFrom: ['src/**/*.{ts,tsx}', 'modules/**/src/**/*.{ts,tsx}'],
};
