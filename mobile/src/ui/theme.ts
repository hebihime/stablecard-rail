/**
 * One place for colour and spacing.
 *
 * Not a design system — four screens do not need one. It exists so the card face,
 * the state-machine stepper and the OTP modal agree with each other, and so the
 * dark/light split is decided once rather than per component.
 */

import { StyleSheet } from 'react-native';

export const palette = {
  background: '#0B1020',
  surface: '#161B2E',
  surfaceMuted: '#1F2540',
  border: '#2A3154',
  text: '#F2F4FF',
  textMuted: '#98A0C0',
  accent: '#6C8CFF',
  positive: '#3ECF8E',
  warning: '#F5A623',
  negative: '#FF6B6B',
  /** Deliberately dull. A frozen card should not look like an error. */
  frozen: '#7A85B0',
} as const;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 32,
} as const;

export const radius = { sm: 8, md: 14, lg: 20 } as const;

/**
 * How a card state reads on screen.
 *
 * `unactivated` and `frozen` are both "cannot spend" and are not the same thing —
 * one has never been used and one has been deliberately stopped — so they get
 * different words and different colours rather than a shared "inactive".
 */
export const cardStateStyle = {
  unactivated: { label: 'Not activated', colour: palette.textMuted },
  active: { label: 'Active', colour: palette.positive },
  frozen: { label: 'Frozen', colour: palette.frozen },
  canceled: { label: 'Canceled', colour: palette.negative },
} as const;

export const text = StyleSheet.create({
  title: { color: palette.text, fontSize: 26, fontWeight: '700' },
  heading: { color: palette.text, fontSize: 18, fontWeight: '600' },
  body: { color: palette.text, fontSize: 15 },
  muted: { color: palette.textMuted, fontSize: 13 },
  mono: {
    color: palette.text,
    fontSize: 15,
    fontFamily: 'Courier',
    letterSpacing: 1,
  },
});
