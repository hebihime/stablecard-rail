/**
 * The in-app wallet (SPEC.md §9.3), against the real `@solana/web3.js`.
 *
 * Real keypairs, real address derivation, real instruction encoding — the library
 * is loaded rather than stubbed, because the parts worth testing are exactly the
 * ones a stub would define away. What is faked is the network: a `Connection` whose
 * RPC methods answer from this file, so the transaction is built and signed for
 * real and simply never leaves.
 *
 * The chain-facing half of this was written knowing the wallet is unfunded and will
 * stay that way until a faucet lands. That makes the *translation* of failures the
 * most valuable thing here: "insufficient funds" and "no lamports" mean different
 * things to whoever has to fix it, and only one of them is about USDC.
 */

import { Keypair } from '@solana/web3.js';

import { __setCardVaultForTesting, VAULT_KEYS, cardVault } from '../../../modules/card-vault';
import { MemoryCardVault } from '../../../modules/card-vault/src/memoryVault';
import type { CardVault } from '../../../modules/card-vault';
import { WalletError } from '../errors';
import type { RpcClient } from '../wallet';
import { loadWallet, readWallet, sendUsdc } from '../wallet';

const DEVNET_USDC = '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU';

/**
 * A vault that stores like the memory one and *reports* like a phone's.
 *
 * `MemoryCardVault` alone will not do: it reports `protection: 'none'`, and
 * `loadWallet` deliberately refuses to persist a signing key into storage that
 * admits it protects nothing. Using it here would make every persistence test
 * assert the refusal — which has a test of its own, below.
 */
class SecureLookingVault extends MemoryCardVault {
  override describe() {
    return { backend: 'keychain', protection: 'device-keystore' } as const;
  }
}

beforeEach(() => {
  __setCardVaultForTesting(new SecureLookingVault());
});
afterEach(() => {
  __setCardVaultForTesting(null);
  jest.restoreAllMocks();
});

/** A `Connection` stand-in: real transaction building and signing, no network. */
function fakeRpc(answers: {
  balance?: number;
  tokenAmount?: string | Error;
  accountInfo?: object | null;
  send?: string | Error;
}) {
  // Cast at the boundary: `RpcClient` is `Connection`'s real signatures, and
  // reproducing `RpcResponseAndContext<TokenAmount>` in full here would be pages of
  // shape nothing reads. The members the wallet actually touches are exact.
  return {
    getBalance: jest.fn(async () => answers.balance ?? 0),
    getTokenAccountBalance: jest.fn(async () => {
      if (answers.tokenAmount instanceof Error) {
        throw answers.tokenAmount;
      }
      return { value: { amount: answers.tokenAmount ?? '0' } };
    }),
    // `in` rather than `??`: `null` is a meaningful answer here — it is how the
    // RPC says "no such account" — and `null ?? default` would erase it.
    getAccountInfo: jest.fn(async () =>
      'accountInfo' in answers ? answers.accountInfo : { lamports: 1 },
    ),
    sendTransaction: jest.fn(async () => {
      if (answers.send instanceof Error) {
        throw answers.send;
      }
      return answers.send ?? 'sig_1';
    }),
  } as unknown as RpcClient & {
    getBalance: jest.Mock;
    getTokenAccountBalance: jest.Mock;
    getAccountInfo: jest.Mock;
    sendTransaction: jest.Mock;
  };
}



describe('loading the wallet', () => {
  it('generates a keypair on first use and keeps it', async () => {
    const first = await loadWallet();
    const second = await loadWallet();

    expect(first.keypair.publicKey.toBase58()).toBe(second.keypair.publicKey.toBase58());
    expect(first.persisted).toBe(true);
  });

  it('stores the secret in the format solana-keygen writes', async () => {
    // So a secret recovered from the vault can be pasted straight into the CLI to
    // fund it — which is the single most likely thing anyone will want to do with
    // it, given the wallet starts empty.
    await loadWallet();

    const stored = await cardVault().getItem(VAULT_KEYS.walletSecret);
    expect(stored).not.toBeNull();
    const parsed = JSON.parse(stored!) as number[];
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed).toHaveLength(64);
  });

  it('round-trips to the same keypair the chain would see', async () => {
    const { keypair } = await loadWallet();
    const stored = JSON.parse((await cardVault().getItem(VAULT_KEYS.walletSecret))!) as number[];

    expect(Keypair.fromSecretKey(Uint8Array.from(stored)).publicKey.toBase58()).toBe(
      keypair.publicKey.toBase58(),
    );
  });

  it('replaces a stored secret that will not parse', async () => {
    // A value from an older build, or a partly-written record. Replacing it loses
    // nothing that was not already lost; refusing would leave a device on which
    // the wallet can never be used again.
    await cardVault().setItem(VAULT_KEYS.walletSecret, 'not-json');

    const { keypair } = await loadWallet();

    expect(keypair.publicKey.toBase58()).toHaveLength(44);
  });

  it('refuses to persist a signing key into storage that protects nothing', async () => {
    // The `MemoryCardVault` reports `protection: 'none'`, which is what Expo Go
    // gets. Writing a signing key there and calling it saved would be worse than
    // not saving it — the screen says "session only" on the strength of this.
    const nowhere: CardVault = {
      setItem: jest.fn(async () => undefined),
      getItem: jest.fn(async () => null),
      deleteItem: jest.fn(async () => undefined),
      describe: () => ({ backend: 'memory', protection: 'none' }),
    };
    __setCardVaultForTesting(nowhere);

    const { persisted } = await loadWallet();

    expect(persisted).toBe(false);
    expect(nowhere.setItem).not.toHaveBeenCalled();
  });
});

describe('reading a balance', () => {
  it('reports lamports and USDC minor units', async () => {
    const rpc = fakeRpc({ balance: 1_500_000, tokenAmount: '2500000' });
    const { keypair } = await loadWallet();

    const snapshot = await readWallet(keypair, DEVNET_USDC, { rpc });

    expect(snapshot.solLamports).toBe(1_500_000);
    expect(snapshot.usdcMinor).toBe(2_500_000);
  });

  it('tells "no token account" apart from "zero balance"', async () => {
    // Different situations: one has never received USDC, the other has spent it.
    // A fund screen that showed $0.00 for the first would be describing an account
    // that does not exist.
    const rpc = fakeRpc({ tokenAmount: new Error('could not find account') });
    const { keypair } = await loadWallet();

    expect((await readWallet(keypair, DEVNET_USDC, { rpc })).usdcMinor).toBeNull();
  });

  it('gives up on a node that never answers, rather than waiting forever', async () => {
    // `@solana/web3.js` applies no timeout of its own, so a node that accepts the
    // connection and goes quiet leaves the caller hanging — on the fund screen that
    // is a button spinning with nothing to stop it, which is what made jp ask
    // whether the transfer was real at all.
    const rpc = fakeRpc({});
    rpc.getBalance.mockRejectedValueOnce(
      Object.assign(new Error('Aborted'), { name: 'AbortError' }),
    );
    const { keypair } = await loadWallet();

    await expect(readWallet(keypair, DEVNET_USDC, { rpc })).rejects.toMatchObject({
      kind: 'rpc',
      message: expect.stringContaining('did not answer'),
    });
  });

  it('recognises the devnet node rate-limiting, which it does readily', async () => {
    const rpc = fakeRpc({});
    rpc.getBalance.mockRejectedValueOnce(new Error('429 Too Many Requests'));
    const { keypair } = await loadWallet();

    await expect(readWallet(keypair, DEVNET_USDC, { rpc })).rejects.toMatchObject({
      kind: 'rpc',
      message: expect.stringContaining('rate-limiting'),
    });
  });
});

describe('sending', () => {
  it('signs and submits a transfer', async () => {
    const rpc = fakeRpc({ send: 'sig_abc' });
    const { keypair } = await loadWallet();

    const signature = await sendUsdc(keypair, {
      to: Keypair.generate().publicKey.toBase58(),
      mint: DEVNET_USDC,
      decimals: 6,
      amountMinor: 1_000_000,
      rpc,
    });

    expect(signature).toBe('sig_abc');
    expect(rpc.sendTransaction).toHaveBeenCalled();
  });

  it('refuses before submitting when there is no source token account', async () => {
    // Cheaper and clearer than letting the chain reject it: the transaction would
    // fail with an account-not-found the user cannot act on.
    const rpc = fakeRpc({ accountInfo: null });
    const { keypair } = await loadWallet();

    await expect(
      sendUsdc(keypair, {
        to: Keypair.generate().publicKey.toBase58(),
        mint: DEVNET_USDC,
        decimals: 6,
        amountMinor: 1_000_000,
        rpc,
      }),
    ).rejects.toMatchObject({ kind: 'no-usdc' });
  });

  it('refuses to claim an aborted send failed, because it may not have', async () => {
    // A send that timed out may still have reached the node. Reporting it as a
    // failure invites a second transfer of the same money.
    const rpc = fakeRpc({ send: Object.assign(new Error('Aborted'), { name: 'AbortError' }) });
    const { keypair } = await loadWallet();

    const failure = await sendUsdc(keypair, {
      to: Keypair.generate().publicKey.toBase58(),
      mint: DEVNET_USDC,
      decimals: 6,
      amountMinor: 1_000_000,
      rpc,
    })
      .then(() => null)
      .catch((raised: unknown) => raised as WalletError);

    expect(failure?.kind).toBe('rpc');
    expect(failure?.message).toMatch(/may or may not have been submitted/);
  });

  it.each([
    ['Attempt to debit an account but found no record of a prior credit', 'no-sol'],
    ['Transfer: insufficient funds', 'no-usdc'],
    ['Blockhash not found', 'unknown'],
  ])('translates %s into %s', async (chainMessage, kind) => {
    // The distinction that matters most today, with the wallet unfunded: no SOL is
    // a fee problem and no USDC is a balance problem, and they send whoever is
    // reading to two different faucets.
    const rpc = fakeRpc({ send: new Error(chainMessage) });
    const { keypair } = await loadWallet();

    const failure = await sendUsdc(keypair, {
      to: Keypair.generate().publicKey.toBase58(),
      mint: DEVNET_USDC,
      decimals: 6,
      amountMinor: 1_000_000,
      rpc,
    })
      // `.then(() => null)` so the type is `WalletError | null` rather than a union
      // with the signature string — a resolved promise here is a failed test either
      // way, and this keeps the assertion below readable.
      .then(() => null)
      .catch((raised: unknown) => raised as WalletError);

    expect(failure).toBeInstanceOf(WalletError);
    expect(failure?.kind).toBe(kind);
  });
});
