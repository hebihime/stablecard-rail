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

module.exports = {
  projects: [
    {
      preset: 'jest-expo/ios',
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: nativeOnly,
    },
    {
      preset: 'jest-expo/android',
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: nativeOnly,
    },
    {
      preset: 'jest-expo/web',
      setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
      testPathIgnorePatterns: webOnly,
    },
  ],
  collectCoverageFrom: ['src/**/*.{ts,tsx}', 'modules/**/src/**/*.{ts,tsx}'],
};
