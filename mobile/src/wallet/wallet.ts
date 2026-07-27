/**
 * The in-app devnet wallet (SPEC.md §9.3).
 *
 * "Triggers a devnet USDC transfer via the in-app wallet (local keypair for the
 * demo)". A real keypair, real SPL transfer instructions, and a real transaction
 * submitted to a real devnet node. Nothing here is simulated.
 *
 * **It will fail for want of money, and that is the honest state of the demo.**
 * A freshly generated wallet holds no SOL and no USDC, and jp's faucet attempts
 * were rate-limited. So the button surfaces the actual error — which is
 * `insufficient funds`, from the chain, not a placeholder — and the screen offers
 * the recorded path alongside it. Nothing changes here when a faucet does land.
 *
 * The secret key is the one durable secret this app holds, and it goes in
 * `CardVault`: the iOS Keychain, the Android Keystore, or an encrypted IndexedDB
 * record. On a platform reporting `protection: 'none'` the wallet is kept in memory
 * for the session only and the screen says so — persisting a signing key into
 * something that admits it protects nothing would be worse than not persisting it.
 */

import './polyfills';

import { Connection, Keypair, PublicKey, Transaction } from '@solana/web3.js';
import {
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountInstruction,
  createTransferCheckedInstruction,
  getAssociatedTokenAddressSync,
} from '@solana/spl-token';

import { VAULT_KEYS, cardVault } from '../../modules/card-vault';
import { WalletError } from './errors';

export { FEE_LAMPORTS, WalletError } from './errors';

/** Devnet. Named here rather than configured: this app signs nothing else. */
export const DEVNET_RPC_URL = 'https://api.devnet.solana.com';

export interface WalletSnapshot {
  address: string;
  /** Lamports. Pays fees only; a transfer needs a few thousand of them. */
  solLamports: number;
  /** USDC in minor units, or `null` when the wallet has no token account yet. */
  usdcMinor: number | null;
  /** True when the secret survived a reload rather than being regenerated. */
  persisted: boolean;
}

/**
 * Load the wallet, or make one.
 *
 * Generated on first use rather than at install: a keypair nobody has funded is
 * worth nothing, and creating one lazily means the screens that never touch the
 * chain never touch the CSPRNG either.
 */
export async function loadWallet(): Promise<{ keypair: Keypair; persisted: boolean }> {
  const stored = await cardVault().getItem(VAULT_KEYS.walletSecret);
  if (stored !== null) {
    try {
      return { keypair: Keypair.fromSecretKey(decodeSecret(stored)), persisted: true };
    } catch {
      // A stored value that will not parse is a value from an older build or a
      // partly-written record. Replacing it is right — the alternative is a wallet
      // that can never be used again on this device — and it loses nothing that
      // was not already lost.
    }
  }
  const keypair = Keypair.generate();
  const vault = cardVault();
  const durable = vault.describe().protection !== 'none';
  if (durable) {
    await vault.setItem(VAULT_KEYS.walletSecret, encodeSecret(keypair.secretKey));
  }
  return { keypair, persisted: durable };
}

/**
 * The subset of `Connection` this module uses.
 *
 * Named so it can be supplied. A test that wants to build and sign a real
 * transaction without sending one has to replace the network and nothing else, and
 * spying on the exported `connection` does not work — these functions call it
 * through a module-internal binding, so the spy is never consulted. Injection is
 * the honest version of what that spy was pretending to do.
 */
export type RpcClient = Pick<
  Connection,
  'getBalance' | 'getTokenAccountBalance' | 'getAccountInfo' | 'sendTransaction'
>;

export function connection(rpcUrl: string = DEVNET_RPC_URL): Connection {
  // `confirmed` rather than `finalized`: the deposit watcher on the backend waits
  // for finality itself, and making the app wait for it too would put thirty
  // seconds of nothing between the button and the first state change on screen.
  return new Connection(rpcUrl, 'confirmed');
}

export async function readWallet(
  keypair: Keypair,
  mint: string,
  options: { persisted?: boolean; rpcUrl?: string; rpc?: RpcClient } = {},
): Promise<WalletSnapshot> {
  const rpc = options.rpc ?? connection(options.rpcUrl);
  const owner = keypair.publicKey;
  try {
    const solLamports = await rpc.getBalance(owner);
    const tokenAccount = getAssociatedTokenAddressSync(new PublicKey(mint), owner);
    let usdcMinor: number | null = null;
    try {
      const balance = await rpc.getTokenAccountBalance(tokenAccount);
      usdcMinor = Number(balance.value.amount);
    } catch {
      // No token account. Distinct from a zero balance, and worth keeping distinct:
      // one means "never received any USDC" and the other means "spent it all".
      usdcMinor = null;
    }
    return {
      address: owner.toBase58(),
      solLamports,
      usdcMinor,
      persisted: options.persisted ?? false,
    };
  } catch (raised) {
    throw new WalletError('rpc', describeRpcFailure(raised));
  }
}

/**
 * Send USDC to the address the backend watches.
 *
 * `createTransferCheckedInstruction` rather than `createTransferInstruction`: the
 * checked form carries the mint and the decimal count, and the token program
 * verifies both. An unchecked transfer of the wrong mint, or of an amount scaled by
 * the wrong power of ten, is a valid transaction — it simply moves the wrong money.
 */
export async function sendUsdc(
  keypair: Keypair,
  {
    to,
    mint,
    decimals,
    amountMinor,
    rpcUrl,
    rpc: injected,
  }: {
    to: string;
    mint: string;
    decimals: number;
    amountMinor: number;
    rpcUrl?: string;
    rpc?: RpcClient;
  },
): Promise<string> {
  const rpc = injected ?? connection(rpcUrl);
  const mintKey = new PublicKey(mint);
  const source = getAssociatedTokenAddressSync(mintKey, keypair.publicKey);
  const destination = new PublicKey(to);

  const transaction = new Transaction();
  if ((await rpc.getAccountInfo(source)) === null) {
    throw new WalletError(
      'no-usdc',
      'this wallet has no USDC token account yet — fund it at faucet.circle.com',
    );
  }
  transaction.add(
    createTransferCheckedInstruction(
      source,
      mintKey,
      destination,
      keypair.publicKey,
      amountMinor,
      decimals,
      [],
      TOKEN_PROGRAM_ID,
    ),
  );

  try {
    return await rpc.sendTransaction(transaction, [keypair]);
  } catch (raised) {
    throw translateSendFailure(raised);
  }
}

/**
 * The instruction that would create a destination token account.
 *
 * Unused by the fund screen and exported because it is the one piece of the flow
 * that changes if the backend's deposit account is ever unfunded: the watched
 * address is an ATA the faucet created, and a transfer to an ATA that does not
 * exist fails rather than creating it.
 */
export function createDestinationAccountInstruction(
  payer: PublicKey,
  owner: PublicKey,
  mint: PublicKey,
) {
  return createAssociatedTokenAccountInstruction(
    payer,
    getAssociatedTokenAddressSync(mint, owner),
    owner,
    mint,
  );
}

function encodeSecret(secret: Uint8Array): string {
  // JSON rather than base58: it is what `solana-keygen` writes, so a secret
  // recovered from the vault can be pasted straight into the CLI to fund it.
  return JSON.stringify(Array.from(secret));
}

function decodeSecret(stored: string): Uint8Array {
  const parsed: unknown = JSON.parse(stored);
  if (!Array.isArray(parsed) || parsed.length !== 64) {
    throw new Error('stored wallet secret is not 64 bytes');
  }
  return Uint8Array.from(parsed as number[]);
}

function describeRpcFailure(raised: unknown): string {
  const because = raised instanceof Error ? raised.message : String(raised);
  // The public devnet endpoint rate-limits readily — a recorded fact on the backend
  // side too (`tests/fixtures/solana/error_rate_limited.json`).
  return /429|rate/i.test(because)
    ? 'the public devnet node is rate-limiting; try again in a moment'
    : `could not reach the devnet node: ${because}`;
}

function translateSendFailure(raised: unknown): WalletError {
  const because = raised instanceof Error ? raised.message : String(raised);
  if (/insufficient lamports|attempt to debit an account but found no record/i.test(because)) {
    return new WalletError(
      'no-sol',
      'this wallet has no devnet SOL to pay the fee — airdrop some at faucet.solana.com',
    );
  }
  if (/insufficient funds/i.test(because)) {
    return new WalletError(
      'no-usdc',
      'this wallet has no devnet USDC to send — get some at faucet.circle.com',
    );
  }
  return new WalletError('unknown', because);
}
