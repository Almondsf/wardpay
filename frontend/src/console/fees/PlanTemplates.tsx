import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { BRAND } from '../../lib/brand';
import { formatKobo, koboToInput, parseNairaToKobo } from '../../lib/format';
import { Empty, ErrorAlert, Field, Loading, Modal, fieldError } from '../../lib/ui';

type Template = Schemas['PlanTemplateOut'];
type Eligibility = Schemas['PlanEligibility'];
type ChargeKind = Schemas['PlanChargeKind'];

export const ELIGIBILITY_LABELS: Record<Eligibility, string> = {
  anyone: 'Any parent',
  no_old_debts: 'Parents with nothing owed from before',
  staff_only: 'Only staff can put a family on it',
};

const percent = (bp: number) => `${Number.isInteger(bp / 100) ? bp / 100 : (bp / 100).toFixed(2)}%`;

export function describeParts(t: Pick<Template, 'parts'>): string {
  return t.parts.map((p) => `${percent(p.percent_bp)} ${p.day === 0 ? 'at the start of term' : `by day ${p.day}`}`).join(' · ');
}

type Rates = Schemas['PlanRatesOut'];

/** WardPay's standard plan charge for so many payments: schools may only charge more. */
export const standardCharge = (rates: Rates | undefined, payments: number) =>
  rates ? payments * rates.per_payment_kobo + rates.plan_fee_kobo : 0;

export function describeCharge(t: Pick<Template, 'charge_kind' | 'charge_value' | 'parts'>, rates: Rates | undefined): string {
  const standard = standardCharge(rates, t.parts.length);
  if (t.charge_kind === 'fixed') return formatKobo(Math.max(t.charge_value, standard));
  if (t.charge_kind === 'percent') return `${percent(t.charge_value)} of what’s owed, at least ${formatKobo(standard)}`;
  return `Standard: ${formatKobo(standard)}`;
}

const useRates = () => useQuery({ queryKey: ['plan-rates'], queryFn: async () => unwrap(await api.GET('/api/v1/plan-templates/rates')) });

/** The school's own instalment plans, which parents can choose themselves. */
export function PlanTemplates() {
  const { can } = useAuth();
  const manage = can('fees.manage');
  const queryClient = useQueryClient();
  const templates = useQuery({ queryKey: ['plan-templates'], queryFn: async () => unwrap(await api.GET('/api/v1/plan-templates')) });
  const rates = useRates();
  const [editing, setEditing] = useState<Template | 'new' | null>(null);
  const toggle = useMutation({
    mutationFn: async (t: Template) =>
      unwrap(
        await api.PUT('/api/v1/plan-templates/{template_id}', {
          params: { path: { template_id: t.id } },
          body: { ...t, is_active: !t.is_active },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['plan-templates'] }),
  });
  if (templates.isPending) return <Loading />;
  if (templates.isError) return <ErrorAlert error={templates.error} />;
  return (
    <div className="card section" aria-label="Instalment plans">
      <p className="muted small">
        Plans parents can choose for themselves on the payment link or the parent portal, for example “Pay in 3”. Each payment is a share of
        the bill, due a number of days after the term starts. A plan charge (optional) is added to the bill to cover the extra payment
        charges.
      </p>
      <ErrorAlert error={toggle.error} />
      {manage && (
        <div className="row end">
          <button onClick={() => setEditing('new')}>Add plan</button>
        </div>
      )}
      {templates.data.length === 0 ? (
        <Empty>No plans yet. Add one and parents can spread their payments without calling the bursary.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Plan</th>
                <th>Payments</th>
                <th>Who can choose it</th>
                <th>Plan charge</th>
                {manage && <th aria-label="Actions" />}
              </tr>
            </thead>
            <tbody>
              {templates.data.map((t) => (
                <tr key={t.id} className={t.is_active ? '' : 'retired'}>
                  <td>{t.name}</td>
                  <td className="small">{describeParts(t)}</td>
                  <td className="small">{ELIGIBILITY_LABELS[t.eligibility]}</td>
                  <td className="small">{describeCharge(t, rates.data)}</td>
                  {manage && (
                    <td className="actions nowrap">
                      <button className="secondary small" onClick={() => setEditing(t)}>
                        Edit
                      </button>{' '}
                      <button className="secondary small" onClick={() => toggle.mutate(t)} disabled={toggle.isPending}>
                        {t.is_active ? 'Switch off' : 'Switch on'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && <PlanForm template={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

interface Row {
  share: string; // percent, as typed
  day: string;
}

function PlanForm({ template, onClose }: { template: Template | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(template?.name ?? '');
  const [eligibility, setEligibility] = useState<Eligibility>(template?.eligibility ?? 'no_old_debts');
  const rates = useRates();
  const [chargeKind, setChargeKind] = useState<ChargeKind>(template?.charge_kind ?? 'none');
  const [charge, setCharge] = useState(
    template?.charge_kind === 'fixed'
      ? koboToInput(template.charge_value)
      : template?.charge_kind === 'percent'
        ? String(template.charge_value / 100)
        : '',
  );
  const [rows, setRows] = useState<Row[]>(
    template?.parts.map((p) => ({ share: String(p.percent_bp / 100), day: String(p.day) })) ?? [
      { share: '50', day: '0' },
      { share: '25', day: '42' },
      { share: '25', day: '70' },
    ],
  );
  const shares = rows.map((r) => Math.round(Number(r.share) * 100));
  const totalBp = shares.reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);
  const chargeValue =
    chargeKind === 'fixed' ? (parseNairaToKobo(charge) ?? 0) : chargeKind === 'percent' ? Math.round(Number(charge) * 100) || 0 : 0;
  const standard = standardCharge(rates.data, rows.length);
  const valid =
    name.trim().length >= 2 &&
    totalBp === 10_000 &&
    rows.length >= 2 &&
    (chargeKind === 'none' || chargeValue > 0) &&
    (chargeKind !== 'fixed' || chargeValue >= standard);
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name,
        is_active: template?.is_active ?? true,
        eligibility,
        charge_kind: chargeKind,
        charge_value: chargeValue,
        parts: rows.map((r, n) => ({ percent_bp: shares[n]!, day: Number(r.day) })),
      };
      return unwrap(
        template
          ? await api.PUT('/api/v1/plan-templates/{template_id}', { params: { path: { template_id: template.id } }, body })
          : await api.POST('/api/v1/plan-templates', { body }),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['plan-templates'] });
      onClose();
    },
  });
  const setRow = (n: number, change: Partial<Row>) => setRows(rows.map((r, i) => (i === n ? { ...r, ...change } : r)));
  return (
    <Modal title={template ? `Edit ${template.name}` : 'Add an instalment plan'} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <ErrorAlert error={save.error} />
        <Field label="Name" hint="What parents see, e.g. Pay in 3" error={fieldError(save.error, 'name')}>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />
        </Field>
        <Field label="Who can choose it">
          <select value={eligibility} onChange={(e) => setEligibility(e.target.value as Eligibility)}>
            {(Object.keys(ELIGIBILITY_LABELS) as Eligibility[]).map((k) => (
              <option key={k} value={k}>
                {ELIGIBILITY_LABELS[k]}
              </option>
            ))}
          </select>
        </Field>
        <fieldset className="plan-parts">
          <legend>Payments</legend>
          {rows.map((r, n) => (
            <div className="row" key={n}>
              <label className="inline-field">
                Share (%)
                <input
                  aria-label={`Payment ${n + 1} share`}
                  inputMode="decimal"
                  value={r.share}
                  onChange={(e) => setRow(n, { share: e.target.value })}
                  size={5}
                />
              </label>
              <label className="inline-field">
                Days after the term starts
                <input
                  aria-label={`Payment ${n + 1} day`}
                  inputMode="numeric"
                  value={r.day}
                  onChange={(e) => setRow(n, { day: e.target.value })}
                  size={4}
                />
              </label>
              {rows.length > 2 && (
                <button type="button" className="secondary small" onClick={() => setRows(rows.filter((_, i) => i !== n))}>
                  Remove
                </button>
              )}
            </div>
          ))}
          <div className="row between">
            {rows.length < 12 && (
              <button type="button" className="secondary small" onClick={() => setRows([...rows, { share: '', day: '' }])}>
                Add a payment
              </button>
            )}
            <span className={totalBp === 10_000 ? 'small muted' : 'small error-text'}>
              Shares add up to {percent(totalBp)} (must be 100%)
            </span>
          </div>
          {fieldError(save.error, 'parts') && <p className="small error-text">{fieldError(save.error, 'parts')}</p>}
        </fieldset>
        <Field
          label="Plan charge"
          hint={`Added to the bill when a family goes on the plan, to cover the extra payment charges and reminders. ${BRAND}’s standard is ${
            rates.data ? `${formatKobo(rates.data.per_payment_kobo)} a payment plus ${formatKobo(rates.data.plan_fee_kobo)}` : '…'
          }; the school can charge more, not less.`}
        >
          <select value={chargeKind} onChange={(e) => setChargeKind(e.target.value as ChargeKind)}>
            <option value="none">
              {BRAND}’s standard ({formatKobo(standard)} for {rows.length} payments)
            </option>
            <option value="fixed">A fixed amount (at least {formatKobo(standard)})</option>
            <option value="percent">A percentage of what’s owed (never less than the standard)</option>
          </select>
        </Field>
        {chargeKind !== 'none' && (
          <Field label={chargeKind === 'fixed' ? 'Amount (₦)' : 'Percentage (%)'} error={fieldError(save.error, 'charge_value')}>
            <input inputMode="decimal" value={charge} onChange={(e) => setCharge(e.target.value)} required />
          </Field>
        )}
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
