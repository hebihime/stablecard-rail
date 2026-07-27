/**
 * The backend's DTOs, in TypeScript.
 *
 * Hand-written rather than generated, and the trade is worth stating. Generating
 * from `/openapi.json` would remove the risk of these drifting, and would add a
 * build step plus a generated file nobody reads to a repo whose whole argument is
 * that decisions are legible. At four screens the drift risk is a failing test
 * (`__tests__/contract.test.ts` checks these against the live schema when a backend
 * is reachable, and skips when it is not), so the cheap version wins.
 *
 * Two rules carried over from the backend, and neither is cosmetic:
 *
 * - **Money is integer minor units.** There is no `number` here that is a currency
 *   amount with a decimal point in it. `formatMinor` is the only place a division
 *   by 100 happens, and it produces a string for display and never a value to
 *   compute with.
 * - **Identifiers are opaque strings.** No parsing, no assuming `card_` prefixes,
 *   no sorting by id. Three providers number their objects three different ways.
 */

/** SPEC.md §3.1's two funding models. */
export type FundingModel = 'fiat_rail' | 'crypto_deposit';

export type CardState = 'unactivated' | 'active' | 'frozen' | 'canceled';

export interface Provider {
  provider_id: string;
  funding_model: FundingModel;
}

export interface Cardholder {
  provider_id: string;
  cardholder_id: string;
  email: string;
  state: string;
  created_at: string;
  raw: Record<string, unknown>;
}

export interface Card {
  provider_id: string;
  card_id: string;
  cardholder_id: string;
  state: CardState;
  /** The only card-number material the backend holds. */
  last_four: string;
  exp_month: number;
  exp_year: number;
  currency: string;
  spend_limit_minor: number | null;
  /** Where a `crypto_deposit` provider expects funds. `null` for fiat rails. */
  deposit_address: string | null;
  created_at: string;
  raw: Record<string, unknown>;
}

export interface Balance {
  card_id: string;
  amount_minor: number;
  currency: string;
}

// --- the reveal (SPEC.md §9.2) ---------------------------------------------

export interface RevealToken {
  token: string;
  provider_id: string;
  card_id: string;
  expires_at: string;
  /** Seconds, from the server — the countdown does not trust the device clock. */
  expires_in: number;
}

/**
 * What a redeemed token yields. **There is no PAN field, and that is deliberate**
 * rather than an omission: under the PSE pattern this models, the card number never
 * reaches the partner backend at all, so there is nothing for one to hold.
 * `rendered_in` names the surface where the real number would appear.
 */
export interface RevealedCard {
  provider_id: string;
  card_id: string;
  last_four: string;
  exp_month: number;
  exp_year: number;
  rendered_in: string;
  raw: Record<string, unknown>;
}

// --- funding intents (SPEC.md §9.3) ----------------------------------------

export type FundingState =
  | 'PENDING'
  | 'DEPOSIT_CONFIRMED'
  | 'BRIDGING'
  | 'BRIDGED'
  | 'FUNDING'
  | 'FUNDED'
  | 'SETTLED'
  | 'FAILED_DEPOSIT'
  | 'FAILED_BRIDGE'
  | 'FAILED_FUNDING'
  | 'FAILED_SETTLEMENT';

/**
 * Where an intent is, as the backend describes it.
 *
 * `sequence` comes down the wire rather than being a constant in this file. The
 * state machine belongs to the backend and has already changed twice; a copy here
 * would be a second source of truth updated by hand, in another language.
 */
export interface IntentProgress {
  sequence: FundingState[];
  /** Index into `sequence`, or `null` for a failure — which is not a later stage. */
  position: number | null;
  is_terminal: boolean;
  is_failure: boolean;
}

export interface FundingIntent {
  id: string;
  state: FundingState;
  provider_id: string;
  card_id: string;
  /** What arrived on the source chain. Never adjusted. */
  amount_minor: number;
  currency: string;
  /** What the bridge delivered, net of fee. The fee is the difference. */
  bridged_amount_minor: number | null;
  deposit_tx_ref: string | null;
  bridge_ref: string | null;
  issuer_funding_ref: string | null;
  retry_count: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  state_changed_at: string;
  progress: IntentProgress;
}

export interface FundingIntentPage {
  count: number;
  intents: FundingIntent[];
}

/**
 * Where to send money so that it reaches one card (SPEC.md §9.3).
 *
 * `deposit_address` is the *source* — the token account the backend watches. The
 * card's own Safe is the destination and lives on `Card.deposit_address`. They are
 * different addresses and confusing them is the mistake ARCHITECTURE §9.8 records.
 */
export interface DepositRoute {
  chain: string;
  deposit_address: string;
  owner_address: string;
  mint: string;
  decimals: number;
  provider_id: string;
  card_id: string;
}

// --- 3DS / OTP (SPEC.md §6) -------------------------------------------------

export interface PendingChallenge {
  provider_id: string;
  challenge_id: string;
  card_id: string | null;
  cardholder_id: string | null;
  /** The code to show. The one value in this API that is meant to be read aloud. */
  code: string;
  /** `true` when the backend minted it rather than reading it from the provider. */
  derived: boolean;
  delivered_at: string;
  expires_at: string;
  /** From the server, so the countdown does not depend on the device's clock. */
  seconds_remaining: number;
  amount_minor: number | null;
  currency: string | null;
}

export interface PendingChallenges {
  count: number;
  challenges: PendingChallenge[];
}

export type ChallengeDecision = 'approve' | 'decline';

export interface ChallengeResponse {
  provider_id: string;
  challenge_id: string;
  decision: ChallengeDecision;
  /** `false` when the provider has no endpoint. The decision is ledgered anyway. */
  delivered: boolean;
  provider_ref: string | null;
  detail: string | null;
}

// --- the ledger (SPEC.md §7) ------------------------------------------------

export interface LedgerEvent {
  id: number;
  occurred_at: string;
  recorded_at: string;
  event_type: string;
  provider_id: string | null;
  cardholder_id: string | null;
  card_id: string | null;
  intent_id: string | null;
  state_before: string | null;
  state_after: string | null;
  amount_minor: number | null;
  currency: string | null;
  idempotency_key: string | null;
  payload: Record<string, unknown>;
}

export interface LedgerPage {
  count: number;
  events: LedgerEvent[];
}

// --- errors -----------------------------------------------------------------

/** The shape `app/api/errors.py` returns: a stable code beside the prose. */
export interface ApiProblem {
  code: string;
  detail: string;
}
