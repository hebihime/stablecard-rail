/**
 * Open 3DS challenges, by both routes SPEC.md §6.3 asks for.
 *
 * "Polling is the reliable fallback; push is the demo-quality path" — and the
 * ordering in that sentence is the design rather than prose. **Polling is the
 * contract here too.** The socket is opened, and if it never connects, drops, or is
 * blocked by something between the phone and the backend, nothing breaks: the poll
 * finds the same challenge a second or two later. A hook that treated the socket as
 * primary would work perfectly in a demo and strand a cardholder on hotel wifi.
 *
 * Both routes carry the identical shape — the backend sends `PendingChallengeOut`
 * over the socket and lists it from `/otp/pending` — so merging is a matter of
 * `challenge_id` and nothing has to know which arrived first.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { useClient } from '../api/ApiProvider';
import type { ChallengeDecision, PendingChallenge } from '../api/types';

/** Fast enough that a cardholder is not left waiting, slow enough to be polite. */
export const POLL_MS = 3_000;

export interface ChallengeFeed {
  /** Soonest deadline first, so the modal shows the most urgent one. */
  challenges: PendingChallenge[];
  /** True while the socket is connected. Shown as a badge, never depended on. */
  pushConnected: boolean;
  answer: (challenge: PendingChallenge, decision: ChallengeDecision) => Promise<AnswerResult>;
  /**
   * Stop showing a challenge.
   *
   * Separate from `answer` on purpose. Dismissing inside `answer` would close the
   * modal the instant the request returns — including when it returns
   * `delivered: false`, which is the one case with something left to say
   * (SPEC.md §6.5). The caller decides when the cardholder has seen enough.
   */
  dismiss: (challenge: PendingChallenge) => void;
}

export interface AnswerResult {
  /** False when the provider had nowhere to send it (SPEC.md §6.5). Not a failure. */
  delivered: boolean;
  detail: string | null;
}

export function useChallenges(cardId?: string): ChallengeFeed {
  const client = useClient();
  const [polled, setPolled] = useState<PendingChallenge[]>([]);
  const [pushed, setPushed] = useState<PendingChallenge[]>([]);
  const [pushConnected, setPushConnected] = useState(false);
  // Answered locally, so the modal closes on the button rather than on the next
  // poll. Without it the code stays on screen for up to three seconds after the
  // cardholder has already decided, which reads as the tap not registering.
  const [answered, setAnswered] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const page = await client.pendingChallenges(cardId);
        if (!cancelled) {
          setPolled(page.challenges);
        }
      } catch {
        // Deliberately silent. A failed poll for challenges is not worth a banner:
        // there is usually nothing open, and the next tick is three seconds away.
        // A real outage is already visible on whichever screen is behind the modal.
      } finally {
        if (!cancelled) {
          timer = setTimeout(() => void poll(), POLL_MS);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [client, cardId]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    try {
      socket = new WebSocket(client.challengeSocketUrl(cardId));
    } catch {
      // No WebSocket on this runtime, or a malformed URL. The poll above is the
      // contract; this is the courtesy, and losing it costs latency only.
      return;
    }
    socket.onopen = () => {
      if (!closed) {
        setPushConnected(true);
      }
    };
    socket.onmessage = (event: { data: unknown }) => {
      try {
        const challenge = JSON.parse(String(event.data)) as PendingChallenge;
        setPushed((current) => [
          ...current.filter((held) => held.challenge_id !== challenge.challenge_id),
          challenge,
        ]);
      } catch {
        // A frame that is not the shape we expect. Dropping it is right: the poll
        // will produce the same challenge, correctly parsed, shortly.
      }
    };
    socket.onerror = socket.onclose = () => {
      if (!closed) {
        setPushConnected(false);
      }
    };
    return () => {
      closed = true;
      socket?.close();
    };
  }, [client, cardId]);

  const answer = useCallback(
    async (challenge: PendingChallenge, decision: ChallengeDecision): Promise<AnswerResult> => {
      const response = await client.respondToChallenge(
        challenge.provider_id,
        challenge.challenge_id,
        decision,
      );
      return { delivered: response.delivered, detail: response.detail };
    },
    [client],
  );

  const dismiss = useCallback((challenge: PendingChallenge) => {
    // Locally, so the modal closes on the tap rather than on the next poll. Three
    // seconds of a code still on screen after a decision reads as the tap not
    // registering, and invites a second one.
    setAnswered((current) => new Set(current).add(key(challenge)));
  }, []);

  const challenges = useMemo(() => {
    const merged = new Map<string, PendingChallenge>();
    // Polled first, then pushed: where both have a challenge the pushed copy is the
    // newer of the two, and its `seconds_remaining` is closer to the truth.
    for (const challenge of [...polled, ...pushed]) {
      merged.set(key(challenge), challenge);
    }
    return [...merged.values()]
      .filter((challenge) => !answered.has(key(challenge)))
      .sort((a, b) => a.expires_at.localeCompare(b.expires_at));
  }, [polled, pushed, answered]);

  return { challenges, pushConnected, answer, dismiss };
}

/**
 * Two providers numbering their challenges from 1 is normal.
 *
 * The backend keys its store on the pair for the same reason, and returns both
 * fields in every payload precisely so a client can do this.
 */
function key(challenge: Pick<PendingChallenge, 'provider_id' | 'challenge_id'>): string {
  return `${challenge.provider_id}:${challenge.challenge_id}`;
}
