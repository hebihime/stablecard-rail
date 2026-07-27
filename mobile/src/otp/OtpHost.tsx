/**
 * Mounts the 3DS modal for the selected card.
 *
 * A component of its own so `_layout.tsx` stays free of session logic, and so the
 * modal is not mounted at all before a card has been chosen — polling `/otp/pending`
 * for a card that does not exist yet is a request every three seconds for an empty
 * list.
 */

import { useSession } from '../session';
import { OtpModal } from './OtpModal';

export function OtpHost() {
  const { selection } = useSession();
  if (selection === null) {
    return null;
  }
  return <OtpModal cardId={selection.cardId} />;
}
