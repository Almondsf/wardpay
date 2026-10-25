import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap, type Schemas } from '../../api/client';
import { BRAND } from '../../lib/brand';
import { formatKobo, koboToInput, parseNairaToKobo } from '../../lib/format';
import { ErrorAlert, Field, Loading, Modal, PageHeader, fieldError } from '../../lib/ui';

type Plan = Schemas['PlatformPlanOut'];

/** What schools pay: a set-up fee, and a fee per pupil per year billed a third each term. No licence fee. */
export function PlatformPlans() {
  const [editing, setEditing] = useState<Plan | 'new' | null>(null);
  const plans = useQuery({ queryKey: ['platform-plans'], queryFn: async () => unwrap(await api.GET('/api/v1/platform/plans')) });
  return (
    <>
      <PageHeader
        title="Plans"
        subtitle={`The school pays; parents use ${BRAND} free. Price changes apply to bills made afterwards.`}
        actions={<button onClick={() => setEditing('new')}>New plan</button>}
      />
      {plans.isPending ? (
        <Loading />
      ) : plans.isError ? (
        <ErrorAlert error={plans.error} />
      ) : (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Plan</th>
                <th className="num">Set-up (once)</th>
                <th className="num">Per pupil, per term</th>
                <th className="num">Per instalment plan</th>
                <th className="num">Schools</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {plans.data.map((p) => (
                <tr key={p.id} className={p.is_active ? '' : 'retired'}>
                  <td>
                    <strong>{p.name}</strong> {p.is_default && <span className="badge ok">Default</span>}
                    {!p.is_active && <span className="badge">Retired</span>}
                    {p.description && <div className="muted small">{p.description}</div>}
                  </td>
                  <td className="num">{formatKobo(p.setup_fee_kobo)}</td>
                  <td className="num">
                    {formatKobo(p.per_pupil_term_kobo)}
                    <div className="muted small">{formatKobo(p.per_pupil_year_kobo)} a year</div>
                  </td>
                  <td className="num">{formatKobo(p.instalment_plan_fee_kobo)}</td>
                  <td className="num">{p.schools}</td>
                  <td className="actions">
                    <button className="secondary small" onClick={() => setEditing(p)}>
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && <EditPlan plan={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </>
  );
}

function EditPlan({ plan, onClose }: { plan: Plan | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    name: plan?.name ?? '',
    description: plan?.description ?? '',
    setup: plan ? koboToInput(plan.setup_fee_kobo) : '',
    perPupil: plan ? koboToInput(plan.per_pupil_year_kobo) : '',
    planFee: plan ? koboToInput(plan.instalment_plan_fee_kobo) : '200',
    perPayment: plan ? koboToInput(plan.plan_charge_per_payment_kobo) : '300',
    is_active: plan?.is_active ?? true,
    is_default: plan?.is_default ?? false,
  });
  const prices = {
    setup: parseNairaToKobo(form.setup),
    perPupil: parseNairaToKobo(form.perPupil),
    planFee: parseNairaToKobo(form.planFee),
    perPayment: parseNairaToKobo(form.perPayment),
  };
  const valid = Object.values(prices).every((v) => v !== null && v >= 0);
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: form.name,
        description: form.description,
        setup_fee_kobo: prices.setup ?? 0,
        per_pupil_year_kobo: prices.perPupil ?? 0,
        instalment_plan_fee_kobo: prices.planFee ?? 0,
        plan_charge_per_payment_kobo: prices.perPayment ?? 0,
        is_active: form.is_active,
        is_default: form.is_default,
      };
      return unwrap(
        plan
          ? await api.PUT('/api/v1/platform/plans/{plan_id}', { params: { path: { plan_id: plan.id } }, body })
          : await api.POST('/api/v1/platform/plans', { body }),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-plans'] });
      onClose();
    },
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value });
  const termly = prices.perPupil !== null ? Math.round(prices.perPupil / 3) : null;
  return (
    <Modal title={plan ? `Edit ${plan.name}` : 'New plan'} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <ErrorAlert error={fieldError(save.error, 'name') ? null : save.error} />
        <Field label="Name" error={fieldError(save.error, 'name')}>
          <input value={form.name} onChange={set('name')} required minLength={2} maxLength={80} />
        </Field>
        <Field label="Description (optional)">
          <input value={form.description} onChange={set('description')} maxLength={300} />
        </Field>
        <Field label="Set-up fee (₦, once)">
          <input value={form.setup} onChange={set('setup')} inputMode="decimal" required />
        </Field>
        <Field label="Per pupil (₦ per school year)" hint={termly !== null ? `Billed ${formatKobo(termly)} per pupil each term.` : undefined}>
          <input value={form.perPupil} onChange={set('perPupil')} inputMode="decimal" required />
        </Field>
        <Field
          label="Per instalment plan (₦)"
          hint="Charged to the school for each instalment plan made (a parent's choice or the bursar's), on its next termly bill. 0 to charge nothing."
        >
          <input value={form.planFee} onChange={set('planFee')} inputMode="decimal" required />
        </Field>
        <Field
          label="Standard plan charge per payment (₦)"
          hint="What a family on an instalment plan is charged per payment (Paystack's transfer cap), plus the per-plan fee above. Schools can charge more, not less."
        >
          <input value={form.perPayment} onChange={set('perPayment')} inputMode="decimal" required />
        </Field>
        <label className="checkbox">
          <input type="checkbox" checked={form.is_active} onChange={set('is_active')} disabled={plan?.is_default} />
          Available for schools
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={form.is_default} onChange={set('is_default')} disabled={plan?.is_default} />
          Default plan (for schools without one of their own)
        </label>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={!valid || save.isPending}>
            Save plan
          </button>
        </div>
      </form>
    </Modal>
  );
}
