/**
 * Money formatting, and the assumptions underneath it.
 *
 * The backend refuses floats for amounts and so does this side. What is asserted
 * here is that the one division in the client produces a *string*, that it uses the
 * right exponent for the currency, and that a currency it has never heard of
 * degrades to something readable instead of throwing inside a render.
 */

import {
  formatCountdown,
  formatExpiry,
  formatMinor,
  formatTokenMinor,
  maskedPan,
  minorUnitExponent,
} from '../money';

describe('formatMinor', () => {
  it('renders a USD amount from its minor units', () => {
    expect(formatMinor(2500, 'USD')).toMatch(/25\.00/);
    expect(formatMinor(2470, 'USD')).toMatch(/24\.70/);
  });

  it('keeps a sub-cent-free amount exact rather than approximate', () => {
    // 1234567 minor units is $12,345.67 exactly. A float path can render
    // 12345.669999999998 and this is the assertion that would catch it.
    expect(formatMinor(1_234_567, 'USD')).toMatch(/12,345\.67/);
  });

  it('uses no decimal places for a zero-exponent currency', () => {
    // JPY has no minor unit: ¥100 is a hundred yen. A hardcoded /100 would be
    // wrong by two orders of magnitude rather than by a rounding error.
    expect(minorUnitExponent('JPY')).toBe(0);
    expect(formatMinor(100, 'JPY')).toMatch(/100/);
    expect(formatMinor(100, 'JPY')).not.toMatch(/1\.00/);
  });

  it('is case-insensitive about the currency code', () => {
    expect(minorUnitExponent('usd')).toBe(2);
  });

  it('assumes two places for a currency it does not know', () => {
    expect(minorUnitExponent('XYZ')).toBe(2);
  });

  it('degrades readably rather than throwing on a bad currency code', () => {
    // `Intl` throws on a malformed code, and a card screen that renders nothing
    // is worse than one that renders an unstyled number.
    expect(formatMinor(2500, 'NOT-A-CODE')).toBe('25.00 NOT-A-CODE');
  });

  it('returns a string, so nothing downstream can add it to anything', () => {
    expect(typeof formatMinor(2500, 'USD')).toBe('string');
  });

  it('renders zero rather than an empty balance', () => {
    expect(formatMinor(0, 'USD')).toMatch(/0\.00/);
  });
});

describe('the card face', () => {
  it('masks a PAN it does not have', () => {
    // There is no PAN in this system to mask; the dots are literal.
    expect(maskedPan('4242')).toBe('•••• •••• •••• 4242');
  });

  it('zero-pads a single-digit month', () => {
    expect(formatExpiry(3, 2029)).toBe('03/29');
  });

  it('zero-pads a year ending in a single digit', () => {
    // 2030 -> "30" is fine; 2005 -> "05" is the one that needs the pad.
    expect(formatExpiry(12, 2005)).toBe('12/05');
  });
});

describe('formatCountdown', () => {
  it.each([
    [300, '5:00'],
    [61, '1:01'],
    [9, '0:09'],
    [0, '0:00'],
  ])('renders %s seconds as %s', (seconds, expected) => {
    expect(formatCountdown(seconds)).toBe(expected);
  });

  it('never goes negative', () => {
    // An expired challenge sits at 0:00 while the screen decides what to do.
    expect(formatCountdown(-3)).toBe('0:00');
  });

  it('floors a fractional second rather than rounding up past the deadline', () => {
    expect(formatCountdown(9.9)).toBe('0:09');
  });
});


describe('formatTokenMinor', () => {
  /**
   * The regression this section exists for.
   *
   * The fund screen rendered a six-decimal USDC amount with a hardcoded `/ 100` and
   * offered to send **$100.00** for a transfer of one dollar — a hundredfold
   * overstatement on the one button in the app that moves money. jp read the button
   * and asked whether it was real.
   *
   * The rule was already written down: docs/ARCHITECTURE.md §9, "USDC has six
   * decimals, a USD card has two … integer division by 10^4". Knowing it was not
   * enough; the conversion had to stop being arithmetic at the call site.
   */
  it('renders one USDC as one dollar', () => {
    expect(formatTokenMinor(1_000_000, 6, 'USD')).toMatch(/^\$?1\.00$/);
  });

  it('does not render one USDC as a hundred dollars', () => {
    // Stated as its own assertion because this is the bug, not a variation of it.
    expect(formatTokenMinor(1_000_000, 6, 'USD')).not.toMatch(/100/);
  });

  it('scales by the difference in decimals, not by a constant', () => {
    expect(formatTokenMinor(2_500_000, 6, 'USD')).toMatch(/2\.50/);
    // An 18-decimal token — what a Gnosis Safe holds — through the same function.
    expect(formatTokenMinor(2_500_000_000_000_000_000, 18, 'USD')).toMatch(/2\.50/);
  });

  it('needs no scaling when the token and the currency agree', () => {
    expect(formatTokenMinor(250, 2, 'USD')).toMatch(/2\.50/);
  });

  it('truncates rather than rounding up into money nobody sent', () => {
    // 1.999999 USDC is one dollar and ninety-nine cents plus dust. Rounding it to
    // $2.00 would show a figure larger than the transfer.
    expect(formatTokenMinor(1_999_999, 6, 'USD')).toMatch(/1\.99/);
  });

  it('shows sub-cent dust as zero rather than as something', () => {
    expect(formatTokenMinor(999, 6, 'USD')).toMatch(/0\.00/);
  });

  it('handles a token with fewer decimals than the currency', () => {
    // Not a real pairing here, and the arithmetic must not invert and multiply.
    expect(formatTokenMinor(250, 0, 'USD')).toMatch(/2\.50/);
  });
});
