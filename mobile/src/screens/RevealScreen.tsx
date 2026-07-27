/**
 * The reveal screen (SPEC.md §9.2).
 *
 * "Full PAN/CVV fetched via a short-lived, single-use reveal token from the
 * backend; auto-hide after a countdown; screenshot-guard flag on the screen where
 * the platform supports it. Structure this deliberately as the Gnosis Pay PSE
 * pattern."
 *
 * **The PAN is not here, and that is the PSE pattern rather than a shortfall.**
 * Under PSE the card number is rendered by the provider's own SDK inside a
 * component the client cannot read, and it never reaches the partner's backend at
 * all — so ours has none to send (docs/ARCHITECTURE.md §12.2). What this screen
 * demonstrates is the exchange: a token minted server-side, spent once, expiring on
 * a server-supplied clock, with the place the real number would appear named
 * explicitly rather than faked with a plausible-looking string of digits.
 *
 * The token goes into `CardVault` between minting and spending. That is not
 * ceremony: an app backgrounded mid-reveal — which is what happens when someone
 * switches to their messages app to check something — comes back to a token it can
 * still spend instead of minting a second one and leaving the first to be ledgered
 * as an abandoned mint.
 */

import * as ScreenCapture from 'expo-screen-capture';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform, StyleSheet, Text, View } from 'react-native';

import { useClient } from '../api/ApiProvider';
import { ApiError } from '../api/client';
import type { RevealedCard } from '../api/types';
import { VAULT_KEYS, cardVault, vaultDescription } from '../../modules/card-vault';
import { formatCountdown, formatExpiry, maskedPan } from '../money';
import type { CardSelection } from '../session';
import { Button, ErrorNotice, Field, Loading, Screen, Section } from '../ui/components';
import { palette, radius, spacing, text } from '../ui/theme';

interface Revealed {
  card: RevealedCard;
  expiresAt: number;
}

export function RevealScreen({ selection }: { selection: CardSelection }) {
  const client = useClient();
  const { providerId, cardId } = selection;
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [guard, setGuard] = useState<'active' | 'unavailable' | 'unknown'>('unknown');
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // The screenshot guard, held only while this screen is mounted. Enabling it
  // globally would stop screenshots of the card screen and the fund screen too,
  // neither of which shows anything worth guarding.
  useEffect(() => {
    let released = false;
    void (async () => {
      try {
        await ScreenCapture.preventScreenCaptureAsync();
        if (!released) {
          setGuard('active');
        }
      } catch {
        // Simulators and the web build have no such flag. Not an error — the screen
        // works, and it says which of the two it got rather than implying the guard.
        setGuard('unavailable');
      }
    })();
    return () => {
      released = true;
      void ScreenCapture.allowScreenCaptureAsync().catch(() => undefined);
    };
  }, []);

  const hide = useCallback(() => {
    setRevealed(null);
    setRemaining(0);
  }, []);

  const reveal = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // A token already in the vault is one that was minted and never spent — the
      // app was backgrounded between the two halves. Reuse it; it is single-use and
      // has not been used. If it has expired the exchange 404s and we mint again.
      const stored = await cardVault().getItem(VAULT_KEYS.revealToken);
      let card: RevealedCard;
      let expiresIn: number;
      try {
        if (stored === null) {
          throw new ApiError(404, 'not_found', 'nothing stored');
        }
        card = await client.redeemRevealToken(stored);
        expiresIn = REVEAL_VISIBLE_SECONDS;
      } catch {
        const minted = await client.mintRevealToken(providerId, cardId);
        await cardVault().setItem(VAULT_KEYS.revealToken, minted.token);
        card = await client.redeemRevealToken(minted.token);
        expiresIn = minted.expires_in;
      }
      // Spent, so it is worthless — and a spent token left on the device is a
      // credential kept for no reason.
      await cardVault().deleteItem(VAULT_KEYS.revealToken);
      if (!mounted.current) {
        return;
      }
      setRevealed({ card, expiresAt: Date.now() + expiresIn * 1000 });
      setRemaining(expiresIn);
    } catch (raised) {
      if (mounted.current) {
        setError(raised);
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [client, providerId, cardId]);

  // The auto-hide. Counted against a wall-clock deadline rather than by
  // decrementing a number once a second: a JavaScript timer does not run while the
  // app is backgrounded, so a decrementing counter would resume where it paused and
  // keep the details on screen well past the deadline.
  useEffect(() => {
    if (revealed === null) {
      return;
    }
    const tick = setInterval(() => {
      const left = Math.ceil((revealed.expiresAt - Date.now()) / 1000);
      if (left <= 0) {
        hide();
      } else {
        setRemaining(left);
      }
    }, 250);
    return () => {
      clearInterval(tick);
    };
  }, [revealed, hide]);

  const vault = vaultDescription();
  const capability = error instanceof ApiError && error.code === 'reveal_unsupported';

  return (
    <Screen>
      <Section title="Card details">
        {revealed === null ? (
          <>
            <Text style={text.muted}>
              The backend mints a token that lives {REVEAL_VISIBLE_SECONDS} seconds and works
              once. This screen spends it.
            </Text>
            {busy ? (
              <Loading label="Minting a token and spending it…" />
            ) : (
              <Button label="Reveal" onPress={() => void reveal()} testID="reveal" />
            )}
          </>
        ) : (
          <View style={styles.revealed} testID="revealed">
            <Text style={styles.pan}>{maskedPan(revealed.card.last_four)}</Text>
            <Field
              label="Expires"
              value={formatExpiry(revealed.card.exp_month, revealed.card.exp_year)}
            />
            <View style={styles.countdown}>
              <Text style={text.muted}>Hides in</Text>
              <Text style={styles.countdownValue} testID="countdown">
                {formatCountdown(remaining)}
              </Text>
            </View>
            <Button label="Hide now" onPress={hide} variant="secondary" testID="hide" />
          </View>
        )}
        {error !== null && <ErrorNotice error={error} onRetry={() => void reveal()} />}
      </Section>

      {revealed !== null && (
        <Section title="Where the number is">
          {/* The honest part, and the reason this screen is worth building. */}
          <Text style={text.body}>
            Rendered in <Text style={text.mono}>{revealed.card.rendered_in}</Text>.
          </Text>
          <Text style={text.muted}>
            Under the Payment Service Element pattern this models, the card number is drawn by
            the provider inside a component neither this app nor the backend can read. There is
            no PAN in this system to show you — that is the design, not a limitation of the
            demo.
          </Text>
        </Section>
      )}

      {capability && (
        <Section title="Why this provider cannot">
          <Text style={text.muted}>
            Lithic and Stripe would both return a sandbox card number if asked. The backend
            does not ask: routing card data through a service built so that none exists in it
            would give up the property the whole design is for.
          </Text>
        </Section>
      )}

      <Section title="On this device">
        <Field label="Secure storage" value={`${vault.backend} (${vault.protection})`} />
        <Text style={text.muted}>{PROTECTION_NOTE[vault.protection]}</Text>
        <Field label="Screenshot guard" value={GUARD_LABEL[guard]} />
        {Platform.OS === 'ios' && guard === 'active' && (
          // Worth saying out loud: the API name promises more than iOS delivers.
          <Text style={text.muted}>
            On iOS this detects a screenshot rather than preventing one — only Android has a
            flag that actually blocks the capture.
          </Text>
        )}
      </Section>
    </Screen>
  );
}

/** How long the details stay on screen. Independent of the token, which is spent. */
const REVEAL_VISIBLE_SECONDS = 30;

const PROTECTION_NOTE = {
  'device-keystore':
    'The reveal token was held by the operating system’s keystore between being minted and being spent.',
  'origin-scoped':
    'In a browser the token is encrypted under a key this page cannot export — but any script on this origin could still ask that key to decrypt. Weaker than a phone, and named as such.',
  none: 'No secure storage on this platform, so nothing was persisted. Expo Go cannot load a custom native module; a development build can.',
} as const;

const GUARD_LABEL = {
  active: 'On for this screen',
  unavailable: 'Not available on this platform',
  unknown: 'Checking…',
} as const;

const styles = StyleSheet.create({
  revealed: { gap: spacing.md },
  pan: { color: palette.text, fontSize: 24, letterSpacing: 2, fontFamily: 'Courier' },
  countdown: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: spacing.sm,
    backgroundColor: palette.surfaceMuted,
    borderRadius: radius.sm,
    padding: spacing.sm,
  },
  countdownValue: { color: palette.warning, fontSize: 20, fontWeight: '700' },
});
