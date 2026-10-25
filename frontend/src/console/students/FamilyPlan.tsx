import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { api, unwrap } from '../../api/client';
import { formatKobo } from '../../lib/format';
import { ErrorAlert, Loading, Modal, Notice } from '../../lib/ui';
import { describeInstalments } from '../../public/PlanChoices';

/**
 * Put all of a parent's children on one of the school's instalment plans, for parents who asked (most
 * won't use the website to choose one themselves), and tell them by WhatsApp or text.
 */
export function FamilyPlanDialog({
  guardianId,
  name,
  remindersOn,
  onClose,
}: {
  guardianId: string;
  name: string;
  remindersOn: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const children = useQuery({
    queryKey: ['family-plan-offers', guardianId],
    staleTime: 0,
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/guardians/{guardian_id}/plan-offers', { params: { path: { guardian_id: guardianId } } })),
  });
  const plans = new Map((children.data ?? []).flatMap((c) => c.offers.map((o) => [o.template_id, o.name] as const)));
  const [chosen, setChosen] = useState('');
  const templateId = chosen || [...plans.keys()][0] || '';
  const [tell, setTell] = useState(remindersOn);
  const apply = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/guardians/{guardian_id}/school-plan', {
          params: { path: { guardian_id: guardianId } },
          body: { template_id: templateId, tell_parent: tell },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['account'] });
      void queryClient.invalidateQueries({ queryKey: ['invoices'] });
      void queryClient.invalidateQueries({ queryKey: ['family-plan-offers', guardianId] });
    },
  });

  return (
    <Modal title={`Instalment plan for ${name}`} onClose={onClose}>
      {children.isPending ? (
        <Loading />
      ) : children.isError ? (
        <ErrorAlert error={children.error} />
      ) : apply.isSuccess ? (
        <>
          <Notice>
            {apply.data.applied.length === 1 ? 'One child is' : `${apply.data.applied.length} children are`} now on the plan.{' '}
            {apply.data.messages > 0
              ? `${name} has been sent the plan by WhatsApp or text.`
              : apply.data.told
                ? ''
                : `${name} has turned off reminders, so no message was sent: let them know yourself.`}
          </Notice>
          <div className="row end">
            <button onClick={onClose}>Done</button>
          </div>
        </>
      ) : plans.size === 0 ? (
        <>
          <p>
            No school plan is available for {name}’s children right now: nothing is owed on this term’s bills, they’re already on a plan, or
            the school has no plans set up (Setup → Fees → Instalment plans).
          </p>
          <div className="row end">
            <button className="secondary" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="muted small">For a parent who asked to spread their payments. Every child below goes on the same plan.</p>
          <label className="field">
            <span className="field-label">Plan</span>
            <select value={templateId} onChange={(e) => setChosen(e.target.value)}>
              {[...plans].map(([id, planName]) => (
                <option key={id} value={id}>
                  {planName}
                </option>
              ))}
            </select>
          </label>
          <ul className="plan-family">
            {(children.data ?? []).map((c) => {
              const offer = c.offers.find((o) => o.template_id === templateId);
              return (
                <li key={c.student_id}>
                  <strong>{c.first_name}</strong>{' '}
                  {offer ? (
                    <>
                      <span className="small">{describeInstalments(offer)}</span>
                      {offer.charge_kobo > 0 && <span className="small muted"> (plan charge {formatKobo(offer.charge_kobo)} added)</span>}
                    </>
                  ) : (
                    <span className="small muted">not on this plan (nothing owed this term, or already on a plan)</span>
                  )}
                </li>
              );
            })}
          </ul>
          <label className="checkbox">
            <input type="checkbox" checked={tell} disabled={!remindersOn} onChange={(e) => setTell(e.target.checked)} />
            {remindersOn ? `Tell ${name} now by WhatsApp or text` : `${name} has turned off reminders, so no message will be sent`}
          </label>
          <ErrorAlert error={apply.error} />
          <div className="row end">
            <button className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button onClick={() => apply.mutate()} disabled={!templateId || apply.isPending}>
              Put on the plan
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
