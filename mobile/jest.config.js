/**
 * Jest, on jest-expo's multi-project preset.
 *
 * `jest-expo` runs each suite once per platform, which is the point rather than
 * thoroughness theatre: the secure-storage module has three implementations behind
 * one interface (iOS Keychain, Android Keystore, WebCrypto), and a shared test that
 * only ever ran on one of them would prove nothing about the other two.
 */
module.exports = {
  preset: 'jest-expo',
  projects: [
    { preset: 'jest-expo/ios', setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'] },
    { preset: 'jest-expo/android', setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'] },
    { preset: 'jest-expo/web', setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'] },
  ],
  collectCoverageFrom: ['src/**/*.{ts,tsx}', 'modules/**/src/**/*.{ts,tsx}'],
};
