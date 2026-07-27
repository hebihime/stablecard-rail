/**
 * The handful of shared pieces every screen uses.
 *
 * `ErrorNotice` is the one worth reading. Every screen can fail in the same four
 * ways, and the difference between them is what the person looking at the screen
 * should do next — retry, start the backend, or stop asking. That decision is made
 * once here rather than four times.
 */

import type { ReactNode } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { ApiError } from '../api/client';
import { palette, radius, spacing, text } from './theme';

export function Screen({ children }: { children: ReactNode }) {
  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.screenContent}
      testID="screen"
    >
      {children}
    </ScrollView>
  );
}

export function Section({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <View style={styles.section}>
      {title !== undefined && <Text style={[text.heading, styles.sectionTitle]}>{title}</Text>}
      {children}
    </View>
  );
}

export function Button({
  label,
  onPress,
  variant = 'primary',
  disabled = false,
  busy = false,
  testID,
}: {
  label: string;
  onPress: () => void;
  variant?: 'primary' | 'secondary' | 'danger';
  disabled?: boolean;
  busy?: boolean;
  testID?: string;
}) {
  const inert = disabled || busy;
  return (
    <Pressable
      accessibilityRole="button"
      // Both, deliberately: `accessibilityState` is what a screen reader announces
      // and what @testing-library/react-native asserts on, and `disabled` is what
      // actually stops the press. Setting only one gives a button that says it is
      // disabled and works, or works and says nothing.
      accessibilityState={{ disabled: inert, busy }}
      disabled={inert}
      onPress={onPress}
      testID={testID}
      style={({ pressed }) => [
        styles.button,
        styles[variant],
        inert && styles.buttonInert,
        pressed && styles.buttonPressed,
      ]}
    >
      {busy ? (
        <ActivityIndicator color={palette.text} />
      ) : (
        <Text style={styles.buttonLabel}>{label}</Text>
      )}
    </Pressable>
  );
}

export function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <View style={styles.field}>
      <Text style={text.muted}>{label}</Text>
      <Text style={mono === true ? text.mono : text.body}>{value}</Text>
    </View>
  );
}

export function Pill({ label, colour }: { label: string; colour: string }) {
  return (
    <View style={[styles.pill, { borderColor: colour }]}>
      <Text style={[styles.pillLabel, { color: colour }]}>{label}</Text>
    </View>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <View style={styles.centered} testID="loading">
      <ActivityIndicator color={palette.accent} />
      <Text style={[text.muted, styles.loadingLabel]}>{label}</Text>
    </View>
  );
}

/**
 * A failure, described in terms of what to do about it.
 *
 * Four cases, and they are genuinely different actions:
 *
 * - `unreachable` — nothing answered. The backend is not running, or the phone
 *   cannot see it. Retrying now will fail the same way; the fix is elsewhere.
 * - `timeout` and any 5xx — something is there and struggling. Retry is reasonable.
 * - `reveal_unsupported` — a capability this provider does not have. Not an error
 *   at all, and offering a retry button would be a lie.
 * - anything else — the backend's own `detail`, which is written to be read.
 */
export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const problem = error instanceof ApiError ? error : null;
  const unreachable = problem?.code === 'unreachable';
  const capability = problem?.code === 'reveal_unsupported';
  const retryable = problem === null || problem.isTransient;

  return (
    <View style={styles.notice} testID="error-notice">
      <Text style={[text.heading, { color: capability ? palette.warning : palette.negative }]}>
        {unreachable
          ? 'Cannot reach the backend'
          : capability
            ? 'Not available for this provider'
            : 'Something went wrong'}
      </Text>
      <Text style={[text.muted, styles.noticeDetail]}>
        {problem?.detail ?? (error instanceof Error ? error.message : String(error))}
      </Text>
      {unreachable && (
        <Text style={[text.muted, styles.noticeDetail]}>
          Start it with `uvicorn app.main:app --port 8000` from `backend/`, or run the app with
          EXPO_PUBLIC_DEMO=1 to use recorded fixtures.
        </Text>
      )}
      {onRetry !== undefined && retryable && !capability && (
        <Button label="Try again" onPress={onRetry} variant="secondary" testID="retry" />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: palette.background },
  screenContent: { padding: spacing.md, gap: spacing.md, paddingBottom: spacing.xl },
  section: {
    backgroundColor: palette.surface,
    borderRadius: radius.md,
    padding: spacing.md,
    gap: spacing.sm,
    borderWidth: 1,
    borderColor: palette.border,
  },
  sectionTitle: { marginBottom: spacing.xs },
  button: {
    borderRadius: radius.sm,
    paddingVertical: spacing.sm + 2,
    paddingHorizontal: spacing.md,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 44,
  },
  primary: { backgroundColor: palette.accent },
  secondary: { backgroundColor: palette.surfaceMuted },
  danger: { backgroundColor: palette.negative },
  buttonInert: { opacity: 0.45 },
  buttonPressed: { opacity: 0.8 },
  buttonLabel: { color: palette.text, fontWeight: '600', fontSize: 15 },
  field: { gap: 2 },
  pill: {
    borderWidth: 1,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
    alignSelf: 'flex-start',
  },
  pillLabel: { fontSize: 12, fontWeight: '600' },
  centered: { alignItems: 'center', gap: spacing.sm, padding: spacing.lg },
  loadingLabel: { textAlign: 'center' },
  notice: {
    backgroundColor: palette.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: palette.border,
    padding: spacing.md,
    gap: spacing.sm,
  },
  noticeDetail: { lineHeight: 19 },
});
