import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';

import type { Schemas } from '../api/client';
import { formatDate, formatKobo, lagosToday } from '../lib/format';
import { ErrorAlert } from '../lib/ui';

export type PlanOffer = Schemas['PlanOfferOut'];

/** When each payment falls due, in plain words: "₦77,500 today, ₦38,750 by 3 Dec…". */
export function describeInstalments(offer: PlanOffer): string {
  const today = lagosToday();
  return offer.instalments
    .map((i) => `${formatKobo(i.amount_kobo)} ${i.due_date === today ? 'today' : `by ${formatDate(i.due_date)}`}`)
    .join(', ');
}

/** The school's instalment plans a parent can choose for one child's bill. */
export function PlanChoices({
  firstName,
  offers,
  choose,
  onChosen,
}: {
  firstName: string;
  offers: PlanOffer[];
  choose: (offer: PlanOffer) => Promise<unknown>;
  onChosen: () => void;
}) {
  const [confirming, setConfirming] = useState<PlanOffer | null>(null);
  const save = useMutation({ mutationFn: choose, onSuccess: onChosen });
  if (offers.length === 0) return null;
  return (
    <div className="plan-choices" role="group" aria-label={`Spread ${firstName}'s fees`}>
      <div className="muted small">Can’t pay it all at once? Spread {firstName}’s fees with one of the school’s plans:</div>
      <ul>
        {offers.map((o) => (
          <li key={o.template_id}>
            <div>
              <strong>{o.name}</strong>
              <div className="small">{describeInstalments(o)}</div>
              {o.charge_kobo > 0 && <div className="small muted">Plan charge {formatKobo(o.charge_kobo)}, added to the bill</div>}
            </div>
            {confirming?.template_id === o.template_id ? (
              <div className="row">
                <button className="small" disabled={save.isPending} onClick={() => save.mutate(o)}>
                  {save.isPending ? 'Saving…' : `Yes, choose ${o.name}`}
                </button>
                <button className="secondary small" onClick={() => setConfirming(null)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button className="secondary small" onClick={() => setConfirming(o)}>
                Choose
              </button>
            )}
          </li>
        ))}
      </ul>
      <ErrorAlert error={save.error} />
    </div>
  );
}
