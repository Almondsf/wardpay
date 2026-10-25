import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { INVOICE_KIND_LABELS, formatDate, formatDateTime, formatKobo, lagosToday, parseNairaToKobo } from '../../lib/format';
import { PlanSchedule, splitEvenly, stepDates } from '../../lib/PlanSchedule';
import { BackLink, ErrorAlert, Field, Loading, Modal, PageHeader, StatusBadge, fieldError } from '../../lib/ui';
import { describeInstalments } from '../../public/PlanChoices';

type Invoice = Schemas['InvoiceOut'];

const LINE_KINDS: Record<Schemas['LineKind'], string> = {
  fee: 'charge',
  discount: 'discount',
  adjustment: 'correction',
  write_off: 'written off',
};

export function InvoiceDetail() {
  const { invoiceId = '' } = useParams();
  const { can } = useAuth();
  const issue = can('invoices.issue');
  const invoice = useQuery({
    queryKey: ['invoice', invoiceId],
    queryFn: async () => unwrap(await api.GET('/api/v1/invoices/{invoice_id}', { params: { path: { invoice_id: invoiceId } } })),
  });
  const [dialog, setDialog] = useState<'charge' | 'adjust' | 'void' | 'plan' | 'school-plan' | 'cancel-plan' | 'write-off' | null>(null);
  const offers = useQuery({
    queryKey: ['plan-offers', invoiceId],
    enabled: issue,
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/invoices/{invoice_id}/plan-offers', { params: { path: { invoice_id: invoiceId } } })),
  });

  if (invoice.isPending) return <Loading />;
  if (invoice.isError) return <ErrorAlert error={invoice.error} />;
  const i = invoice.data;
  const live = i.status !== 'void';

  return (
    <>
      <BackLink to="/console/billing">Billing</BackLink>
      <PageHeader
        title={`Invoice #${i.number}`}
        subtitle={
          <>
            <Link to={`/console/students/${i.student.id}`}>{i.student.full_name}</Link> ({i.student.admission_number}) · {INVOICE_KIND_LABELS[i.kind]} · issued{' '}
            {formatDate(i.issued_at)}
            {i.due_date && ` · due ${formatDate(i.due_date)}`} · <StatusBadge status={i.status} />
          </>
        }
        actions={
          live &&
          (issue || can('payments.approve')) && (
            <>
              {issue && (
                <>
                  <button className="secondary" onClick={() => setDialog('charge')}>
                    Add charge
                  </button>
                  <button className="secondary" onClick={() => setDialog('adjust')}>
                    Correct amount
                  </button>
                  {i.paid_kobo === 0 && (
                    <button className="secondary" onClick={() => setDialog('void')}>
                      Void
                    </button>
                  )}
                </>
              )}
              {can('payments.approve') && i.balance_kobo > 0 && !i.plan && (
                <button className="secondary" onClick={() => setDialog('write-off')}>
                  Write off
                </button>
              )}
            </>
          )
        }
      />
      {i.void_reason && <div className="alert error">Voided: {i.void_reason}</div>}
      <div className="card table-card">
        <table>
          <thead>
            <tr>
              <th>Item</th>
              <th>Added</th>
              <th className="num">Amount</th>
            </tr>
          </thead>
          <tbody>
            {i.lines.map((line) => (
              <tr key={line.id}>
                <td>
                  {line.description}
                  {line.kind !== 'fee' && <span className="muted small"> ({LINE_KINDS[line.kind]})</span>}
                  {line.reason && <div className="muted small">{line.reason}</div>}
                </td>
                <td className="muted small">{formatDateTime(line.created_at)}</td>
                <td className="num">{formatKobo(line.amount_kobo)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th colSpan={2}>Total</th>
              <th className="num">{formatKobo(i.total_kobo)}</th>
            </tr>
            <tr>
              <td colSpan={2}>Paid</td>
              <td className="num">{formatKobo(i.paid_kobo)}</td>
            </tr>
            <tr>
              <th colSpan={2}>Balance</th>
              <th className="num">{formatKobo(i.balance_kobo)}</th>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="hint">Invoice lines are never edited or deleted. Corrections are added as new lines, so the history is always visible.</p>
      {(i.plan || (issue && live && i.balance_kobo > 0)) && (
        <section className="card section" aria-label="Payment plan">
          <div className="row between">
            <h2>Payment plan</h2>
            {issue && live && (i.plan ? (
              <button className="secondary small" onClick={() => setDialog('cancel-plan')}>
                Cancel plan
              </button>
            ) : (
                <>
                  {(offers.data?.length ?? 0) > 0 && (
                    <button className="secondary small" onClick={() => setDialog('school-plan')}>
                      Use a school plan
                    </button>
                  )}{' '}
                  <button className="secondary small" onClick={() => setDialog('plan')}>
                    Pay in instalments
                  </button>
                </>
            ))}
          </div>
          {i.plan ? (
            <>
              {i.plan.note && <p className="muted small">{i.plan.note}</p>}
              <PlanSchedule plan={i.plan} />
              <p className="hint">
                While the plan is kept to, this bill isn't counted as overdue, and reminders ask for the next instalment around its date.
              </p>
            </>
          ) : (
            <p className="muted small">
              Agree with the parent to pay the {formatKobo(i.balance_kobo)} still owed in instalments. Reminders then follow the instalment
              dates, and the bill only counts as overdue if an instalment is missed.
            </p>
          )}
        </section>
      )}
      {dialog === 'void' && <VoidInvoice invoice={i} onClose={() => setDialog(null)} />}
      {dialog === 'write-off' && <WriteOff invoice={i} approves={can('debts.write_off')} onClose={() => setDialog(null)} />}
      {dialog === 'plan' && <NewPlan invoice={i} onClose={() => setDialog(null)} />}
      {dialog === 'school-plan' && <SchoolPlan invoice={i} offers={offers.data ?? []} onClose={() => setDialog(null)} />}
      {dialog === 'cancel-plan' && <CancelPlan invoice={i} onClose={() => setDialog(null)} />}
      {(dialog === 'charge' || dialog === 'adjust') && <AddLine invoice={i} kind={dialog === 'charge' ? 'fee' : 'adjustment'} onClose={() => setDialog(null)} />}
    </>
  );
}

function useRefresh(invoice: Invoice) {
  const queryClient = useQueryClient();
  return (data: Invoice) => {
    queryClient.setQueryData(['invoice', invoice.id], data);
    void queryClient.invalidateQueries({ queryKey: ['invoices'] });
    void queryClient.invalidateQueries({ queryKey: ['billing-summary'] });
    void queryClient.invalidateQueries({ queryKey: ['account', invoice.student.id] });
  };
}

function AddLine({ invoice, kind, onClose }: { invoice: Invoice; kind: 'fee' | 'adjustment'; onClose: () => void }) {
  const refresh = useRefresh(invoice);
  const [description, setDescription] = useState('');
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const kobo = parseNairaToKobo(amount, { allowNegative: kind === 'adjustment' });
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/invoices/{invoice_id}/lines', {
          params: { path: { invoice_id: invoice.id } },
          body: { kind, description, amount_kobo: kobo ?? 0, reason },
        }),
      ),
    onSuccess: (data) => {
      refresh(data);
      onClose();
    },
  });
  return (
    <Modal title={kind === 'fee' ? 'Add a charge' : 'Correct the amount'} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <ErrorAlert error={save.error} />
        <Field label="Description" hint={kind === 'fee' ? 'e.g. Replacement ID card' : 'e.g. PTA levy waived'}>
          <input value={description} onChange={(e) => setDescription(e.target.value)} required minLength={2} />
        </Field>
        <Field
          label="Amount (₦)"
          hint={kind === 'adjustment' ? 'Negative to reduce the invoice (e.g. -5000), positive to increase it.' : undefined}
          error={amount && kobo === null ? 'Not a valid amount' : undefined}
        >
          <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" required />
        </Field>
        <Field label="Reason (kept with the invoice)">
          <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={!kobo || save.isPending}>
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

function VoidInvoice({ invoice, onClose }: { invoice: Invoice; onClose: () => void }) {
  const refresh = useRefresh(invoice);
  const [reason, setReason] = useState('');
  const save = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/invoices/{invoice_id}/void', { params: { path: { invoice_id: invoice.id } }, body: { reason } })),
    onSuccess: (data) => {
      refresh(data);
      onClose();
    },
  });
  return (
    <Modal title={`Void invoice #${invoice.number}`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p>Use this for an invoice issued by mistake. It stays on record, marked void, and no longer counts as owed.</p>
        <ErrorAlert error={save.error} />
        <Field label="Reason">
          <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={save.isPending}>
            Void invoice
          </button>
        </div>
      </form>
    </Modal>
  );
}

/** Stop chasing debt the school won't collect. The proprietor approves (theirs applies at once). */
function WriteOff({ invoice, approves, onClose }: { invoice: Invoice; approves: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState(String(invoice.balance_kobo / 100));
  const [reason, setReason] = useState('');
  const kobo = parseNairaToKobo(amount);
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/invoices/{invoice_id}/write-off', {
          params: { path: { invoice_id: invoice.id } },
          body: { amount_kobo: kobo ?? 0, reason },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['invoice', invoice.id] });
      void queryClient.invalidateQueries({ queryKey: ['account', invoice.student.id] });
      void queryClient.invalidateQueries({ queryKey: ['write-offs'] });
      onClose();
    },
  });
  return (
    <Modal title={`Write off debt on invoice #${invoice.number}`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p>
          For money the school won’t collect, such as a family that left owing. The bill stays on record with a “written off” line.{' '}
          {approves ? 'As the proprietor, yours takes effect now.' : 'The proprietor approves it before anything changes.'}
        </p>
        <ErrorAlert error={save.error} />
        <Field
          label="Amount (₦)"
          hint={`Up to ${formatKobo(invoice.balance_kobo)}, what’s still owed`}
          error={fieldError(save.error, 'amount_kobo')}
        >
          <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} required />
        </Field>
        <Field label="Why" error={fieldError(save.error, 'reason')}>
          <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} maxLength={300} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={!kobo || reason.trim().length < 3 || save.isPending}>
            {approves ? 'Write off' : 'Ask the proprietor'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/** Put the bill on one of the school's own plans (staff also see the staff-only ones). */
function SchoolPlan({ invoice, offers, onClose }: { invoice: Invoice; offers: Schemas['PlanOfferOut'][]; onClose: () => void }) {
  const refresh = useRefresh(invoice);
  const queryClient = useQueryClient();
  const [chosen, setChosen] = useState(offers[0]?.template_id ?? '');
  const offer = offers.find((o) => o.template_id === chosen);
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/invoices/{invoice_id}/plan/school-plan', {
          params: { path: { invoice_id: invoice.id } },
          body: { template_id: chosen },
        }),
      ),
    onSuccess: (data) => {
      refresh(data);
      void queryClient.invalidateQueries({ queryKey: ['plan-offers', invoice.id] });
      onClose();
    },
  });
  return (
    <Modal title="Use a school plan" onClose={onClose}>
      <ErrorAlert error={save.error} />
      <Field label="Plan">
        <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
          {offers.map((o) => (
            <option key={o.template_id} value={o.template_id}>
              {o.name}
            </option>
          ))}
        </select>
      </Field>
      {offer && (
        <>
          <p className="small">{describeInstalments(offer)}</p>
          {offer.charge_kobo > 0 && <p className="small muted">A plan charge of {formatKobo(offer.charge_kobo)} is added to the bill.</p>}
        </>
      )}
      <div className="row end">
        <button type="button" className="secondary" onClick={onClose}>
          Cancel
        </button>
        <button onClick={() => save.mutate()} disabled={!offer || save.isPending}>
          Use this plan
        </button>
      </div>
    </Modal>
  );
}

const isoToday = lagosToday; // the server rejects a first instalment before today in Lagos

/** Split what's owed into instalments: start from an even split on regular dates, then adjust any row. */
function NewPlan({ invoice, onClose }: { invoice: Invoice; onClose: () => void }) {
  const refresh = useRefresh(invoice);
  const [count, setCount] = useState(3);
  const [first, setFirst] = useState(isoToday());
  const [every, setEvery] = useState<'month' | 'fortnight' | 'week'>('month');
  const [note, setNote] = useState('');
  const [rows, setRows] = useState(() => build(3, isoToday(), 'month'));

  function build(n: number, start: string, step: typeof every) {
    const amounts = splitEvenly(invoice.balance_kobo, n);
    return stepDates(start, n, step).map((date, k) => ({ date, amount: String(amounts[k]! / 100) }));
  }
  const regenerate = (n = count, start = first, step = every) => setRows(build(n, start, step));
  const kobo = rows.map((r) => parseNairaToKobo(r.amount));
  const total = kobo.reduce<number>((sum, k) => sum + (k ?? 0), 0);
  const valid = kobo.every((k) => k !== null && k > 0) && total === invoice.balance_kobo;

  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/invoices/{invoice_id}/plan', {
          params: { path: { invoice_id: invoice.id } },
          body: { note, instalments: rows.map((r, k) => ({ due_date: r.date, amount_kobo: kobo[k] ?? 0 })) },
        }),
      ),
    onSuccess: (data) => {
      refresh(data);
      onClose();
    },
  });

  return (
    <Modal title={`Pay ${formatKobo(invoice.balance_kobo)} in instalments`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="grid-2 tight">
          <Field label="Instalments">
            <select
              value={count}
              onChange={(e) => {
                setCount(Number(e.target.value));
                regenerate(Number(e.target.value));
              }}
            >
              {Array.from({ length: 11 }, (_, k) => k + 2).map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Every">
            <select
              value={every}
              onChange={(e) => {
                const step = e.target.value as typeof every;
                setEvery(step);
                regenerate(count, first, step);
              }}
            >
              <option value="month">Month</option>
              <option value="fortnight">Two weeks</option>
              <option value="week">Week</option>
            </select>
          </Field>
        </div>
        <Field label="First instalment due">
          <input
            type="date"
            value={first}
            min={isoToday()}
            onChange={(e) => {
              setFirst(e.target.value);
              if (e.target.value) regenerate(count, e.target.value);
            }}
          />
        </Field>
        <table className="compact">
          <thead>
            <tr>
              <th>#</th>
              <th>Due</th>
              <th className="num">Amount (₦)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, k) => (
              <tr key={k}>
                <td>{k + 1}</td>
                <td>
                  <input
                    type="date"
                    aria-label={`Instalment ${k + 1} due`}
                    value={r.date}
                    onChange={(e) => setRows(rows.map((x, j) => (j === k ? { ...x, date: e.target.value } : x)))}
                  />
                </td>
                <td className="num">
                  <input
                    aria-label={`Instalment ${k + 1} amount`}
                    inputMode="decimal"
                    value={r.amount}
                    onChange={(e) => setRows(rows.map((x, j) => (j === k ? { ...x, amount: e.target.value } : x)))}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={total === invoice.balance_kobo ? 'muted small' : 'error-text small'}>
          Adds up to {formatKobo(total)} of {formatKobo(invoice.balance_kobo)} owed.
        </p>
        <Field label="Note (optional)" hint="e.g. what was agreed and with whom">
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
        </Field>
        <ErrorAlert error={save.error && !fieldError(save.error, 'instalments') ? save.error : null} />
        {fieldError(save.error, 'instalments') && <p className="error-text">{fieldError(save.error, 'instalments')}</p>}
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={!valid || save.isPending}>
            Agree plan
          </button>
        </div>
      </form>
    </Modal>
  );
}

function CancelPlan({ invoice, onClose }: { invoice: Invoice; onClose: () => void }) {
  const refresh = useRefresh(invoice);
  const [reason, setReason] = useState('');
  const cancel = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/invoices/{invoice_id}/plan/cancel', { params: { path: { invoice_id: invoice.id } }, body: { reason } })),
    onSuccess: (data) => {
      refresh(data);
      onClose();
    },
  });
  return (
    <Modal title="Cancel the payment plan" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          cancel.mutate();
        }}
      >
        <p>The bill goes back to its own due date. Payments already made stay applied. You can agree a new plan afterwards.</p>
        <ErrorAlert error={cancel.error} />
        <Field label="Reason (kept in the activity log)">
          <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Keep the plan
          </button>
          <button type="submit" className="danger" disabled={cancel.isPending}>
            Cancel plan
          </button>
        </div>
      </form>
    </Modal>
  );
}
