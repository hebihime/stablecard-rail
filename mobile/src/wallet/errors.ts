/**
 * Wallet failures and constants, with no chain library behind them.
 *
 * A separate module from `wallet.ts` for one practical reason and one that turned
 * out to matter more. The practical one: `@solana/web3.js` is ESM-only and pulls a
 * tree of ESM-only dependencies, so anything importing it has to be transformed —
 * and a screen that only needs to *name* an error should not drag that in.
 *
 * The one that matters more: these are the words shown to whoever is looking at an
 * empty wallet, and they belong somewhere a person can find and edit without
 * reading transaction-building code.
 */

/** Lamports one transfer costs, roughly. Shown to explain a refusal, not enforced. */
export const FEE_LAMPORTS = 5_000;

export type WalletFailure =
  /** No SOL to pay the fee. Nothing to do with USDC; the commonest confusion. */
  | 'no-sol'
  /** No USDC, or no token account to hold any. */
  | 'no-usdc'
  /** The node did not answer, or rate-limited. Transient. */
  | 'rpc'
  | 'unknown';

export class WalletError extends Error {
  readonly kind: WalletFailure;

  constructor(kind: WalletFailure, message: string) {
    super(message);
    this.name = 'WalletError';
    this.kind = kind;
  }

  get isTransient(): boolean {
    return this.kind === 'rpc';
  }
}
