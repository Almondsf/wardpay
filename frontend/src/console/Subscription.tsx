import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { BRAND } from '../lib/brand';
import { saveBlob } from '../lib/download';
import { formatDate, formatKobo } from '../lib/format';
import { Empty, ErrorAlert, Loading, Notice, PageHeader } from '../lib/ui';

type Invoice = Schemas['SubscriptionInvoiceOut'];
const REFERENCE = /^WS[0-9A-F]{20}$/;

/** Back from the card payment page: what the gateway says happened (never what the URL says). */
function CardResult({ reference }: { reference: string }) {
  const queryClient = useQueryClient();
  const result = useQuery({
    queryKey: ['subscription-card', reference],
    retry: false,
    refetchInterval: (query) => (query.state.data?.status === 'pending' ? 3000 : false),
    queryFn: async () => unwrap(await api.POST('/api/v1/subscription/checkouts/verify', { body: { reference } })),
  });
  const status = result.data?.status;
  useEffect(() => {
    if (status === 'succeeded') void queryClient.invalidateQueries({ queryKey: ['subscription'] });
  }, [status, queryClient]);
  if (result.isPending) return <Notice>Checking your payment…</Notice>;
  if (result.isError) return <ErrorAlert error={result.error} />;
  if (status === 'succeeded') return <Notice>Thank you. We received {formatKobo(result.data.amount_kobo)}.</Notice>;
  if (status === 'pending') return <Notice>We're confirming your payment with the bank. This updates by itself.</Notice>;
  if (status === 'failed')
    return (
      <div className="alert warn" role="status">
        The payment didn't go through and you haven't been charged.
      </div>
    );
  return (
    <div className="alert warn" role="status">
      We received your payment but it needs a quick check on our side. We'll be in touch.
    </div>
  );
}

/** What the school pays for WardPay: its plan and its termly bills. */
export function Subscription() {
  const sub = useQuery({ queryKey: ['subscription'], queryFn: async () => unwrap(await api.GET('/api/v1/subscription')) });
  const { can } = useAuth();
  const [params] = useSearchParams();
  // Paystack sends ?reference= (and trxref=); Flutterwave sends ?tx_ref=
  const returned = params.get('reference') ?? params.get('trxref') ?? params.get('tx_ref') ?? '';
  const payByCard = useMutation({
    mutationFn: async (invoice: Invoice) =>
      unwrap(await api.POST('/api/v1/subscription/invoices/{invoice_id}/checkout', { params: { path: { invoice_id: invoice.id } } })),
    onSuccess: (checkout) => window.location.assign(checkout.authorization_url),
  });
  const pdf = useMutation({
    mutationFn: async (invoice: Invoice) => {
      const { data, response } = await api.GET('/api/v1/subscription/invoices/{invoice_id}/pdf', {
        params: { path: { invoice_id: invoice.id } },
        parseAs: 'blob',
      });
      if (!response.ok || !data) throw new Error('Could not download the invoice.');
      saveBlob(data as Blob, `wardpay-invoice-${invoice.number}.pdf`);
    },
  });
  if (sub.isPending) return <Loading />;
  if (sub.isError) return <ErrorAlert error={sub.error} />;
  const { plan, pay_to: payTo } = sub.data;
  const owed = sub.data.invoices.reduce((sum, i) => sum + i.balance_kobo, 0);
  return (
    <>
      <PageHeader title="Subscription" subtitle={`What your school pays for ${BRAND}. Parents never pay anything extra.`} />
      {REFERENCE.test(returned) && <CardResult reference={returned} />}
      <div className="grid-2">
        <section className="card" aria-label="Your plan">
          <h2>{plan.name} plan</h2>
          <dl className="details">
            <dt>Set-up fee</dt>
            <dd>{formatKobo(plan.setup_fee_kobo)}, once</dd>
            <dt>Per pupil</dt>
            <dd>
              {formatKobo(plan.per_pupil_term_kobo)} each term ({formatKobo(plan.per_pupil_year_kobo)} a year)
            </dd>
            {plan.instalment_plan_fee_kobo > 0 && (
              <>
                <dt>Instalment plans</dt>
                <dd>{formatKobo(plan.instalment_plan_fee_kobo)} for each family put on a plan, on your next bill</dd>
              </>
            )}
          </dl>
          <p className="muted small">
            With {sub.data.pupils_this_term} pupils this term, the pupil charge for a term is {formatKobo(sub.data.next_term_estimate_kobo)}.
          </p>
          {sub.data.plans_since_last_bill > 0 && (
            <p className="muted small">
              {sub.data.plans_since_last_bill} instalment {sub.data.plans_since_last_bill === 1 ? 'plan' : 'plans'} made since your last
              bill: {formatKobo(sub.data.plans_since_last_bill_kobo)} on the next one.
            </p>
          )}
        </section>
        <section className="card" aria-label="How to pay">
          <h2>How to pay</h2>
          {can('payments.record') && <p>By card: press Pay by card next to a bill.</p>}
          {payTo ? (
            <p>
              Or by bank transfer to <strong>{payTo.account_name}</strong>, account <strong>{payTo.account_number}</strong> ({payTo.bank_name}).
              Please quote the invoice number as the reference.
            </p>
          ) : (
            <p>Reply to the invoice email and we'll send our bank details.</p>
          )}
          {owed > 0 && (
            <p>
              Still to pay: <strong>{formatKobo(owed)}</strong>
            </p>
          )}
        </section>
      </div>
      <h2 className="section-title">Bills</h2>
      <ErrorAlert error={pdf.error ?? payByCard.error} />
      {sub.data.invoices.length === 0 ? (
        <Empty>No bills yet. You'll get one by email at the start of each term.</Empty>
      ) : (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Term</th>
                <th className="num">Pupils</th>
                <th className="num">Total</th>
                <th className="num">Still owed</th>
                <th>Due</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sub.data.invoices.map((i) => (
                <tr key={i.id} className={i.status === 'void' ? 'retired' : ''}>
                  <td>
                    <button className="link" onClick={() => pdf.mutate(i)}>
                      {i.number} (PDF)
                    </button>
                    {i.status !== 'unpaid' && (
                      <span className={`badge ${i.status === 'paid' ? 'ok' : i.status === 'overdue' ? 'danger' : ''}`}>
                        {i.status === 'paid' ? 'Paid' : i.status === 'overdue' ? 'Overdue' : 'Void'}
                      </span>
                    )}
                    {i.lines.length > 0 && (
                      <details className="bill-lines">
                        <summary className="small">What it’s for</summary>
                        <ul className="small">
                          {i.lines.map((ln, n) => (
                            <li key={n}>
                              {ln.description}: {ln.quantity > 1 ? `${ln.quantity} × ${formatKobo(ln.unit_kobo)} = ` : ''}
                              <strong>{formatKobo(ln.amount_kobo)}</strong>
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </td>
                  <td className="small">{i.period}</td>
                  <td className="num">{i.pupils}</td>
                  <td className="num">{formatKobo(i.total_kobo)}</td>
                  <td className="num">{formatKobo(i.balance_kobo)}</td>
                  <td className="nowrap small">{formatDate(i.due_date)}</td>
                  <td>
                    {can('payments.record') && i.balance_kobo > 0 && (i.status === 'unpaid' || i.status === 'overdue') && (
                      <button className="small" onClick={() => payByCard.mutate(i)} disabled={payByCard.isPending}>
                        {payByCard.isPending && payByCard.variables?.id === i.id ? 'Opening…' : 'Pay by card'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
