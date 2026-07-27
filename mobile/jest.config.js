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
