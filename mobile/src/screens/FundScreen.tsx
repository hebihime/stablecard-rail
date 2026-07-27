/**
 * The fund screen (SPEC.md §9.3).
 *
 * "Shows the Solana devnet deposit address (QR + copy), triggers a devnet USDC
 * transfer via the in-app wallet, then live-renders the funding intent's state
 * machine progress (PENDING → … → FUNDED) by polling the intent endpoint."
 *
 * **The address shown is ours, not the card's.** A `CRYPTO_DEPOSIT` card has a Safe
 * and that Safe is the *destination*; the address a user sends to is the Solana
 * account this service watches, and the two are different. Showing the Safe here
 * would collect real money at an address the watcher never polls
 * (docs/ARCHITECTURE.md §9.8, which is the same mistake in the other direction).
 *
 * The stepper renders `progress.sequence` from the response rather than a constant
 * in this file, so the app has no copy of the state machine to keep in step.
 */

import * as Clipboard from 'expo-clipboard';
import { useCallback, useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import QRCode from 'react-native-qrcode-svg';

import { useClient, useQuery } from '../api/ApiProvider';
import type { DepositRoute, FundingIntent, FundingIntentPage } from '../api/types';
import { formatMinor } from '../money';
import type { CardSelection } from '../session';
import { Button, ErrorNotice, Field, Loading, Screen, Section } from '../ui/components';
import { palette, radius, spacing, text } from '../ui/theme';
import { FEE_LAMPORTS, WalletError } from '../wallet/errors';
import { loadWallet, readWallet, sendUsdc } from '../wallet/wallet';
import type { WalletSnapshot } from '../wallet/wallet';

/** Fast, because this is the screen someone watches while a state machine runs. */
const INTENT_POLL_MS = 2_000;
/** One dollar. Small enough to repeat, large enough to survive the dust floor. */
const SEND_AMOUNT_MINOR = 1_000_000;

export function FundScreen({ selection }: { selection: CardSelection }) {
  const client = useClient();
  const { providerId, cardId } = selection;
  const [wallet, setWallet] = useState<WalletSnapshot | null>(null);
  const [walletError, setWalletError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Registering on open is what `funding/routes.py` was built for — its docstring
  // says re-registering is a no-op "because the fund screen will do it every time
  // it is opened".
  const route = useQuery<DepositRoute>(() => client.claimDepositRoute(providerId, cardId), {
    deps: [providerId, cardId],
  });

  const intents = useQuery<FundingIntentPage>(() => client.listIntents({ cardId, limit: 5 }), {
    deps: [cardId],
    intervalMs: INTENT_POLL_MS,
    // Stop when there is nothing left to watch. Polling a settled intent forever
    // is a request every two seconds for an answer that cannot change.
    pollWhile: (page) => page.intents.some((intent) => !intent.progress.is_terminal),
  });

  useEffect(() => {
    if (route.data === null) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const { keypair, persisted } = await loadWallet();
        const snapshot = await readWallet(keypair, route.data!.mint, { persisted });
        if (!cancelled) {
          setWallet(snapshot);
        }
      } catch (raised) {
        if (!cancelled) {
          setWalletError(raised);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [route.data]);

  const copy = useCallback(async () => {
    if (route.data === null) {
      return;
    }
    await Clipboard.setStringAsync(route.data.deposit_address);
    setCopied(true);
    setTimeout(() => {
      setCopied(false);
    }, 2000);
  }, [route.data]);

  const send = useCallback(async () => {
    if (route.data === null) {
      return;
    }
    setSending(true);
    setWalletError(null);
    setSent(null);
    try {
      const { keypair } = await loadWallet();
      const signature = await sendUsdc(keypair, {
        to: route.data.deposit_address,
        mint: route.data.mint,
        decimals: route.data.decimals,
        amountMinor: SEND_AMOUNT_MINOR,
      });
      setSent(signature);
      // The watcher polls on its own schedule; asking now shortens the wait for the
      // first state change from "next tick of the worker" to "next tick of this".
      intents.refresh();
    } catch (raised) {
      setWalletError(raised);
    } finally {
      setSending(false);
    }
  }, [route.data, intents]);

  if (route.loading) {
    return (
      <Screen>
        <Loading label="Claiming a deposit address…" />
      </Screen>
    );
  }
  if (route.data === null) {
    return (
      <Screen>
        <ErrorNotice error={route.error} onRetry={route.refresh} />
      </Screen>
    );
  }

  const broke =
    wallet !== null && (wallet.solLamports < FEE_LAMPORTS || (wallet.usdcMinor ?? 0) === 0);

  return (
    <Screen>
      <Section title="Send devnet USDC here">
        <View style={styles.qr} testID="deposit-qr">
          <QRCode value={route.data.deposit_address} size={180} backgroundColor="white" />
        </View>
        <Text style={text.mono} testID="deposit-address">
          {route.data.deposit_address}
        </Text>
        <Button
          label={copied ? 'Copied' : 'Copy address'}
          onPress={() => void copy()}
          variant="secondary"
          testID="copy-address"
        />
        <Text style={text.muted}>
          This is the token account this service watches — not the card&apos;s Safe, which is
          where the money ends up. Owner {route.data.owner_address}.
        </Text>
      </Section>

      <Section title="This device&apos;s wallet">
        {wallet === null ? (
          walletError === null ? (
            <Loading label="Reading the wallet…" />
          ) : (
            <ErrorNotice error={walletError} />
          )
        ) : (
          <>
            <Field label="Address" value={wallet.address} mono />
            <Field label="SOL" value={`${wallet.solLamports / 1e9}`} />
            <Field
              label="USDC"
              value={
                wallet.usdcMinor === null
                  ? 'no token account yet'
                  : formatMinor(wallet.usdcMinor, 'USD')
              }
            />
            {!wallet.persisted && (
              // A signing key persisted into storage that admits it protects
              // nothing would be worse than one that does not survive a reload.
              <Text style={text.muted}>
                Not saved: this platform reports no secure storage, so the key lives for this
                session only.
              </Text>
            )}
            {broke && (
              <Text style={styles.broke}>
                Unfunded. Send it devnet SOL from faucet.solana.com and devnet USDC from
                faucet.circle.com, and the button below starts working with no change here.
              </Text>
            )}
            <Button
              label={`Send ${formatMinor(SEND_AMOUNT_MINOR / 100, 'USD')}`}
              onPress={() => void send()}
              busy={sending}
              testID="send-usdc"
            />
            {sent !== null && (
              <Field label="Signature" value={sent} mono />
            )}
            {walletError !== null && <WalletProblem error={walletError} />}
          </>
        )}
      </Section>

      <Section title="Funding">
        {intents.data === null ? (
          <Loading label="Looking for a funding intent…" />
        ) : intents.data.intents.length === 0 ? (
          <Text style={text.muted}>
            Nothing yet. An intent appears once the watcher sees a confirmed deposit at the
            address above — which is the only thing that creates one.
          </Text>
        ) : (
          intents.data.intents.map((intent) => <IntentProgress key={intent.id} intent={intent} />)
        )}
      </Section>
    </Screen>
  );
}

/**
 * One intent, as a stepper.
 *
 * A failure is drawn as a stopped journey rather than a red step in the middle of
 * one: `position` is null for a failure state, so there is nothing to highlight and
 * the state's own name plus `last_error` is what gets shown.
 */
function IntentProgress({ intent }: { intent: FundingIntent }) {
  const { sequence, position, is_failure: failed } = intent.progress;
  return (
    <View style={styles.intent} testID={`intent-${intent.id}`}>
      <View style={styles.intentTop}>
        <Text style={text.body}>{formatMinor(intent.amount_minor, intent.currency)}</Text>
        <Text style={[text.muted, failed && styles.failed]}>{intent.state}</Text>
      </View>
      {failed ? (
        <Text style={styles.failed} testID="intent-failure">
          {intent.last_error ?? 'failed with no reason recorded'}
        </Text>
      ) : (
        <View style={styles.steps}>
          {sequence.map((step, index) => (
            <View
              key={step}
              style={[
                styles.step,
                position !== null && index <= position && styles.stepReached,
                position === index && styles.stepCurrent,
              ]}
              testID={`step-${step}`}
            />
          ))}
        </View>
      )}
      {intent.bridged_amount_minor !== null && (
        // Both numbers, so the bridge fee is a subtraction rather than a change
        // nobody can see (SPEC.md §11).
        <Text style={text.muted}>
          bridged {formatMinor(intent.bridged_amount_minor, intent.currency)} — fee{' '}
          {formatMinor(intent.amount_minor - intent.bridged_amount_minor, intent.currency)}
        </Text>
      )}
      {intent.retry_count > 0 && (
        <Text style={text.muted}>retried {intent.retry_count}×</Text>
      )}
    </View>
  );
}

/** A wallet failure, in the words of whoever has to fix it. */
function WalletProblem({ error }: { error: unknown }) {
  if (!(error instanceof WalletError)) {
    return <ErrorNotice error={error} />;
  }
  return (
    <View testID="wallet-error">
      <Text style={styles.failed}>{error.message}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  qr: { alignSelf: 'center', padding: spacing.md, backgroundColor: 'white', borderRadius: radius.md },
  broke: { color: palette.warning, fontSize: 13, lineHeight: 19 },
  failed: { color: palette.negative, fontSize: 13 },
  intent: {
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: palette.border,
  },
  intentTop: { flexDirection: 'row', justifyContent: 'space-between' },
  steps: { flexDirection: 'row', gap: spacing.xs },
  step: { flex: 1, height: 6, borderRadius: 3, backgroundColor: palette.surfaceMuted },
  stepReached: { backgroundColor: palette.positive },
  stepCurrent: { backgroundColor: palette.accent },
});
