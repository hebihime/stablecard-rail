/**
 * Which card this app is looking at, and how it remembers.
 *
 * **There is no authentication in this system** (docs/ARCHITECTURE.md §11.6), so
 * there is no session to derive a card from. The app therefore holds a selection:
 * a `(provider_id, card_id)` pair the user chose or created. In a real deployment
 * this would come from a signed-in identity, and every route would check it — the
 * shape of that gap is recorded rather than hidden behind something that resembles
 * a login.
 *
 * The selection is kept in `CardVault`. It is not a secret and the vault is for
 * secrets, which is worth defending: the alternative is a second storage dependency
 * (`@react-native-async-storage/async-storage`) for one string, on a platform where
 * the vault already works identically on all three targets. The comment on
 * `VAULT_KEYS.selectedCard` says the same thing where someone would look for it.
 */

import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { VAULT_KEYS, cardVault } from '../modules/card-vault';
import { resolveConfig } from './config';

export interface CardSelection {
  providerId: string;
  cardId: string;
}

interface SessionValue {
  selection: CardSelection | null;
  /** False until the stored selection has been read — distinct from "none stored". */
  ready: boolean;
  select: (selection: CardSelection) => Promise<void>;
  forget: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({
  children,
  /** Injected by tests; resolved from the build's configuration otherwise. */
  persist = !resolveConfig().demoMode,
}: {
  children: ReactNode;
  persist?: boolean;
}) {
  const [selection, setSelection] = useState<CardSelection | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // **The selection lasts as long as the thing it points at, and no longer.**
      //
      // In demo mode the backend is a closure recreated on every page load, so a
      // card created before a reload does not exist after one. Persisting the
      // selection across that boundary guarantees a stale pointer on the second
      // visit — the exact dead end `CardScreen` now recovers from, arrived at every
      // single time rather than occasionally. Recovering from a state that never
      // needed to exist is worse than not creating it.
      const stored = persist ? await cardVault().getItem(VAULT_KEYS.selectedCard) : null;
      if (cancelled) {
        return;
      }
      setSelection(stored === null ? null : parseSelection(stored));
      // Set last and unconditionally: a screen that renders "no card yet" while the
      // read is still in flight flashes onboarding at someone who has a card.
      setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [persist]);

  const select = useCallback(
    async (next: CardSelection) => {
      setSelection(next);
      if (persist) {
        await cardVault().setItem(VAULT_KEYS.selectedCard, JSON.stringify(next));
      }
    },
    [persist],
  );

  const forget = useCallback(async () => {
    setSelection(null);
    // Deleted unconditionally, unlike the write. A selection stored by an earlier
    // build — or by a run with a different configuration — has to be clearable by
    // the button that exists to clear it.
    await cardVault().deleteItem(VAULT_KEYS.selectedCard);
  }, []);

  const value = useMemo(
    () => ({ selection, ready, select, forget }),
    [selection, ready, select, forget],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error('useSession must be used inside a SessionProvider');
  }
  return value;
}

/**
 * Read a stored selection, and treat anything unexpected as nothing.
 *
 * The stored value is JSON this app wrote, but a partly-written record or a value
 * from an older build would otherwise crash the first render of the first screen —
 * an unrecoverable state on a device, fixable only by deleting the app.
 */
function parseSelection(stored: string): CardSelection | null {
  try {
    const parsed: unknown = JSON.parse(stored);
    if (typeof parsed !== 'object' || parsed === null) {
      return null;
    }
    const { providerId, cardId } = parsed as Partial<CardSelection>;
    return typeof providerId === 'string' && typeof cardId === 'string'
      ? { providerId, cardId }
      : null;
  } catch {
    return null;
  }
}
