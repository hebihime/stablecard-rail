/**
 * The 3DS modal (SPEC.md §9.4).
 *
 * "Appears on push/poll, shows the code, copy button, approve/decline." And
 * explicitly *not* a push notification — SPEC.md §6.4 says so in parentheses, and
 * the reason is worth keeping in view: a notification leaves a one-time code in the
 * notification centre of a locked phone, where anyone holding it can read it. An
 * in-app modal shows the code to whoever has the app open and nobody else.
 *
 * Mounted once at the root, so it appears over whichever screen is in front. A
 * challenge arrives because a payment is being attempted, not because of anything
 * the person is doing in the app.
 */

import * as Clipboard from 'expo-clipboard';
import { useCallback, useEffect, useState } from 'react';
import { Modal, StyleSheet, Text, View } from 'react-native';

import type { ChallengeDecision, PendingChallenge } from '../api/types';
import { formatCountdown, formatMinor } from '../money';
import { Button, Pill } from '../ui/components';
import { palette, radius, spacing, text } from '../ui/theme';
import { useChallenges } from './useChallenges';

export function OtpModal({ cardId }: { cardId?: string }) {
  const { challenges, pushConnected, answer, dismiss } = useChallenges(cardId);
  const challenge = challenges[0] ?? null;
  const [busy, setBusy] = useState<ChallengeDecision | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [remaining, setRemaining] = useState(0);

  // Counted from the deadline the *server* sent, not from a number ticking down
  // locally: `seconds_remaining` exists on the payload precisely because the
  // device's clock is the one clock the backend cannot vouch for.
  useEffect(() => {
    if (challenge === null) {
      return;
    }
    const deadline = Date.parse(challenge.expires_at);
    const tick = () => {
      setRemaining(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    };
    tick();
    const timer = setInterval(tick, 500);
    return () => {
      clearInterval(timer);
    };
  }, [challenge]);

  useEffect(() => {
    setOutcome(null);
    setCopied(false);
  }, [challenge?.challenge_id]);

  const copy = useCallback(async () => {
    if (challenge === null) {
      return;
    }
    await Clipboard.setStringAsync(challenge.code);
    setCopied(true);
  }, [challenge]);

  const decide = useCallback(
    async (decision: ChallengeDecision) => {
      if (challenge === null) {
        return;
      }
      setBusy(decision);
      try {
        const result = await answer(challenge, decision);
        if (result.delivered) {
          // Nothing left to say, so get out of the way.
          dismiss(challenge);
          return;
        }
        // `delivered: false` is SPEC.md §6.5's fallback, not a failure: the
        // provider has no endpoint, the decision is ledgered, and saying "failed"
        // would be wrong about what happened. The modal stays until it has been
        // read — closing on the tap would hide the one thing worth reporting.
        setOutcome(
          result.detail ?? 'Recorded. This provider has no endpoint to receive the decision.',
        );
      } catch (raised) {
        setOutcome(raised instanceof Error ? raised.message : String(raised));
      } finally {
        setBusy(null);
      }
    },
    [challenge, answer, dismiss],
  );

  return (
    <Modal
      visible={challenge !== null}
      transparent
      animationType="fade"
      // Not dismissible by tapping away or by the Android back button: a challenge
      // is a payment waiting on an answer, and closing it by accident leaves the
      // cardholder at a terminal that simply times out. It goes when it is decided
      // or when it expires.
      onRequestClose={() => undefined}
    >
      <View style={styles.backdrop}>
        {challenge !== null && (
          <View style={styles.sheet} testID="otp-modal">
            <View style={styles.header}>
              <Text style={text.heading}>Confirm this payment</Text>
              <Pill
                label={pushConnected ? 'live' : 'polling'}
                colour={pushConnected ? palette.positive : palette.textMuted}
              />
            </View>

            {challenge.amount_minor !== null && challenge.currency !== null && (
              <Text style={styles.amount}>
                {formatMinor(challenge.amount_minor, challenge.currency)}
              </Text>
            )}

            <View style={styles.codeBox}>
              <Text style={styles.code} testID="otp-code" selectable>
                {challenge.code}
              </Text>
              <Text style={text.muted} testID="otp-countdown">
                expires in {formatCountdown(remaining)}
              </Text>
            </View>

            <Button
              label={copied ? 'Copied' : 'Copy code'}
              onPress={() => void copy()}
              variant="secondary"
              testID="copy-code"
            />

            {challenge.derived && (
              // Whose code this is changes what it means. A derived one was minted
              // by this card program because the provider's flow makes us the
              // challenge issuer (ARCHITECTURE §11.4) — worth saying, because a
              // cardholder waiting for an SMS that will never arrive is stuck.
              <Text style={text.muted}>
                Issued by this card program rather than by the provider — there is no SMS
                coming.
              </Text>
            )}

            {outcome !== null && (
              <Text style={styles.outcome} testID="otp-outcome">
                {outcome}
              </Text>
            )}

            {outcome === null ? (
              <View style={styles.actions}>
                <Button
                  label="Decline"
                  onPress={() => void decide('decline')}
                  variant="danger"
                  busy={busy === 'decline'}
                  disabled={busy !== null}
                  testID="decline"
                />
                <Button
                  label="Approve"
                  onPress={() => void decide('approve')}
                  busy={busy === 'approve'}
                  disabled={busy !== null}
                  testID="approve"
                />
              </View>
            ) : (
              <Button
                label="Dismiss"
                onPress={() => {
                  dismiss(challenge);
                }}
                variant="secondary"
                testID="dismiss"
              />
            )}

            <Text style={text.muted}>
              {challenge.provider_id} · {challenge.challenge_id}
            </Text>
          </View>
        )}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(4, 7, 18, 0.82)',
    justifyContent: 'center',
    padding: spacing.md,
  },
  sheet: {
    backgroundColor: palette.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: palette.border,
    padding: spacing.lg,
    gap: spacing.md,
  },
  header: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  amount: { color: palette.text, fontSize: 28, fontWeight: '700' },
  codeBox: {
    backgroundColor: palette.surfaceMuted,
    borderRadius: radius.md,
    padding: spacing.md,
    alignItems: 'center',
    gap: spacing.xs,
  },
  code: { color: palette.text, fontSize: 36, fontWeight: '700', letterSpacing: 8 },
  outcome: { color: palette.warning, fontSize: 13, lineHeight: 19 },
  actions: { flexDirection: 'row', gap: spacing.sm },
});
