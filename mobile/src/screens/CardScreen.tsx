/**
 * The card screen (SPEC.md §9.1).
 *
 * "Virtual card visual, masked PAN, balance (from `get_balance`), freeze/unfreeze
 * toggle." All four, plus the thing that makes this project's centrepiece visible:
 * the provider and its funding model, read from the registry rather than assumed.
 *
 * The freeze toggle is one control over two provider verbs — `freeze` and
 * `activate`, because activate *is* the unfreeze path (SPEC.md §9.1, and
 * `CardIssuerAdapter.activate_card`'s docstring). Which of the two to call is the
 * adapter's problem for a frozen card and this screen's problem for a card that has
 * never been activated, since those need different words on the button.
 */

import { useCallback, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { Balance, Card } from '../api/types';
import { useClient, useQuery } from '../api/ApiProvider';
import { ApiError } from '../api/client';
import { formatExpiry, formatMinor, maskedPan } from '../money';
import type { CardSelection } from '../session';
import { Button, ErrorNotice, Field, Loading, Pill, Screen, Section } from '../ui/components';
import { cardStateStyle, palette, radius, spacing, text } from '../ui/theme';

/** Slow: a card's state changes when this screen changes it, not on its own. */
const BALANCE_POLL_MS = 15_000;

export function CardScreen({
  selection,
  onOpenReveal,
  onOpenFund,
}: {
  selection: CardSelection;
  onOpenReveal: () => void;
  onOpenFund: () => void;
}) {
  const client = useClient();
  const { providerId, cardId } = selection;
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);

  const card = useQuery<Card>(() => client.getCard(providerId, cardId), {
    deps: [providerId, cardId],
  });
  const balance = useQuery<Balance>(() => client.getBalance(providerId, cardId), {
    deps: [providerId, cardId],
    intervalMs: BALANCE_POLL_MS,
  });

  const toggle = useCallback(async () => {
    if (card.data === null) {
      return;
    }
    setPendingAction(true);
    setActionError(null);
    try {
      const frozen = card.data.state === 'frozen';
      await (frozen
        ? client.activateCard(providerId, cardId)
        : client.freezeCard(providerId, cardId));
      // Both, and in this order. The card's own state is what the toggle reads, and
      // a freeze can change spendable balance at a crypto-deposit provider, where
      // the balance is the Safe's rather than the card's.
      card.refresh();
      balance.refresh();
    } catch (raised) {
      setActionError(raised);
    } finally {
      setPendingAction(false);
    }
  }, [card, balance, client, providerId, cardId]);

  if (card.loading) {
    return (
      <Screen>
        <Loading label="Reading the card from the provider…" />
      </Screen>
    );
  }
  if (card.data === null) {
    return (
      <Screen>
        <ErrorNotice error={card.error} onRetry={card.refresh} />
      </Screen>
    );
  }

  const state = cardStateStyle[card.data.state];
  const canceled = card.data.state === 'canceled';
  const frozen = card.data.state === 'frozen';
  const unactivated = card.data.state === 'unactivated';

  return (
    <Screen>
      <View style={styles.card} testID="card-face">
        <View style={styles.cardTop}>
          <Text style={text.muted}>{providerId}</Text>
          <Pill label={state.label} colour={state.colour} />
        </View>
        <Text style={styles.pan} testID="masked-pan">
          {maskedPan(card.data.last_four)}
        </Text>
        <View style={styles.cardBottom}>
          <Field label="Expires" value={formatExpiry(card.data.exp_month, card.data.exp_year)} />
          <Field label="Currency" value={card.data.currency} />
        </View>
      </View>

      <Section title="Balance">
        {balance.data === null ? (
          balance.loading ? (
            <Loading label="Asking the provider…" />
          ) : (
            <ErrorNotice error={balance.error} onRetry={balance.refresh} />
          )
        ) : (
          <>
            <Text style={styles.balance} testID="balance">
              {formatMinor(balance.data.amount_minor, balance.data.currency)}
            </Text>
            {/* A stale figure beside a live one, rather than instead of it: a poll
                that fails should not blank a balance that was correct a moment ago. */}
            {balance.error !== null && (
              <Text style={text.muted}>
                Last refresh failed
                {balance.error instanceof ApiError ? `: ${balance.error.detail}` : ''}
              </Text>
            )}
          </>
        )}
      </Section>

      <Section title="Controls">
        {actionError !== null && <ErrorNotice error={actionError} />}
        <Button
          label={unactivated ? 'Activate card' : frozen ? 'Unfreeze card' : 'Freeze card'}
          onPress={() => {
            void toggle();
          }}
          variant={frozen || unactivated ? 'primary' : 'secondary'}
          disabled={canceled}
          busy={pendingAction}
          testID="freeze-toggle"
        />
        {canceled && (
          // Terminal at every provider. A disabled button with no explanation reads
          // as a broken screen.
          <Text style={text.muted}>
            A canceled card is terminal — no provider here can reopen one.
          </Text>
        )}
        <Button label="Reveal card details" onPress={onOpenReveal} variant="secondary" />
        <Button label="Add funds" onPress={onOpenFund} variant="secondary" />
      </Section>

      <Section title="Card">
        <Field label="Card id" value={card.data.card_id} mono />
        <Field label="Cardholder id" value={card.data.cardholder_id} mono />
        {card.data.spend_limit_minor !== null && (
          <Field
            label="Spend limit"
            value={formatMinor(card.data.spend_limit_minor, card.data.currency)}
          />
        )}
        {card.data.deposit_address !== null && (
          // Present only for a crypto-deposit provider, and it is the card's Safe —
          // the *destination*, not the Solana address deposits are sent to. The two
          // are different and confusing them cost this project a day (§9.8).
          <Field label="Safe address (destination)" value={card.data.deposit_address} mono />
        )}
      </Section>
    </Screen>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: palette.surfaceMuted,
    borderRadius: radius.lg,
    padding: spacing.lg,
    gap: spacing.lg,
    borderWidth: 1,
    borderColor: palette.border,
    minHeight: 190,
    justifyContent: 'space-between',
  },
  cardTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  cardBottom: { flexDirection: 'row', gap: spacing.xl },
  pan: { color: palette.text, fontSize: 22, letterSpacing: 2, fontFamily: 'Courier' },
  balance: { color: palette.text, fontSize: 34, fontWeight: '700' },
});
