/**
 * Money, as integer minor units, everywhere except the last inch.
 *
 * The backend's rule, unchanged on this side: an amount is an integer count of the
 * smallest unit of its currency, and there is no float anywhere near it. This file
 * is the *only* place a division happens, it happens once, and what comes out is a
 * string for a human to read rather than a value to compute with.
 *
 * That last part is the whole discipline. `2470 / 100` is `24.7`, and once that
 * exists, someone adds it to something.
 */

/**
 * Minor units per major unit, for the currencies this demo can produce.
 *
 * Not every currency has two. JPY has none — ¥100 is a hundred yen, not one yen —
 * and a hardcoded `/ 100` would show a hundredfold error rather than a rounding
 * one. The demo is USD end to end, so this map is short; it exists because the
 * assumption behind not having one is wrong rather than merely unproven.
 */
const MINOR_UNITS: Readonly<Record<string, number>> = {
  USD: 2,
  EUR: 2,
  GBP: 2,
  JPY: 0,
};

const DEFAULT_EXPONENT = 2;

export function minorUnitExponent(currency: string): number {
  return MINOR_UNITS[currency.toUpperCase()] ?? DEFAULT_EXPONENT;
}

/**
 * Render an amount for display. Never for arithmetic.
 *
 * Uses `Intl.NumberFormat`, so a locale that groups with spaces or writes the
 * symbol after the number gets what it expects. The conversion to a float happens
 * here and the result leaves as a string, so nothing downstream can accumulate it.
 */
export function formatMinor(amountMinor: number, currency: string): string {
  const exponent = minorUnitExponent(currency);
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: exponent,
      maximumFractionDigits: exponent,
    }).format(amountMinor / 10 ** exponent);
  } catch {
    // `Intl` throws on a currency code it does not recognise, and a card screen
    // that renders nothing is worse than one that renders "2500 XTS".
    return `${(amountMinor / 10 ** exponent).toFixed(exponent)} ${currency}`;
  }
}

/**
 * A masked card number, from the only digits this system holds.
 *
 * Four groups, so it reads as a card rather than as a string that happens to end in
 * digits. The dots are literal — there is no PAN to mask, because the backend never
 * has one (SPEC.md §9.2, docs/ARCHITECTURE.md §12.2).
 */
export function maskedPan(lastFour: string): string {
  return `•••• •••• •••• ${lastFour}`;
}

/** `MM/YY`, zero-padded, from the provider's two integers. */
export function formatExpiry(month: number, year: number): string {
  return `${String(month).padStart(2, '0')}/${String(year % 100).padStart(2, '0')}`;
}

/**
 * `m:ss` for a countdown.
 *
 * Clamped at zero rather than going negative: an expired challenge or reveal shows
 * `0:00` while the screen decides what to do about it, and `-0:03` is a bug report.
 */
export function formatCountdown(seconds: number): string {
  const clamped = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(clamped / 60);
  return `${minutes}:${String(clamped % 60).padStart(2, '0')}`;
}
