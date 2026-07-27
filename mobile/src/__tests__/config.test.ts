/**
 * Where the app points.
 *
 * The one setting that decides whether this bundle talks to a backend or to
 * recorded fixtures, so it is worth being exact about. The test that matters most
 * is the last one: demo mode must never be something the app falls into because a
 * request failed.
 */

import { LOCAL_API_URL, resolveConfig } from '../config';

describe('resolveConfig', () => {
  it('points at a backend on this machine when nothing is set', () => {
    expect(resolveConfig({})).toEqual({ apiBaseUrl: LOCAL_API_URL, demoMode: false });
  });

  it('uses the loopback address rather than the localhost name', () => {
    // Not interchangeable. `localhost` resolves ::1 first on macOS, and this repo
    // has already lost time to a container on the IPv6 wildcard answering requests
    // meant for a host process on IPv4 — while looking entirely plausible.
    expect(LOCAL_API_URL).toBe('http://127.0.0.1:8000');
  });

  it('takes a configured API URL', () => {
    expect(resolveConfig({ EXPO_PUBLIC_API_URL: 'https://api.example.test' }).apiBaseUrl).toBe(
      'https://api.example.test',
    );
  });

  it('ignores an empty or whitespace-only setting', () => {
    // A CI environment that sets the variable to "" should get the default, not a
    // client whose base URL is the empty string and whose every request is relative.
    expect(resolveConfig({ EXPO_PUBLIC_API_URL: '' }).apiBaseUrl).toBe(LOCAL_API_URL);
    expect(resolveConfig({ EXPO_PUBLIC_API_URL: '   ' }).apiBaseUrl).toBe(LOCAL_API_URL);
  });

  it('turns demo mode on only when it is asked for explicitly', () => {
    expect(resolveConfig({ EXPO_PUBLIC_DEMO: '1' }).demoMode).toBe(true);
    expect(resolveConfig({ EXPO_PUBLIC_DEMO: '0' }).demoMode).toBe(false);
    expect(resolveConfig({ EXPO_PUBLIC_DEMO: 'true' }).demoMode).toBe(false);
    expect(resolveConfig({}).demoMode).toBe(false);
  });

  it('does not infer demo mode from an unreachable backend', () => {
    // The important one. If the app fell back to fixtures when a request failed,
    // someone whose backend had merely stopped would be shown fabricated balances
    // and a funded card, with nothing on screen to say so. Demo mode is a build
    // decision; an unreachable backend is an error message.
    expect(resolveConfig({ EXPO_PUBLIC_API_URL: 'http://nothing.invalid' }).demoMode).toBe(false);
  });
});
