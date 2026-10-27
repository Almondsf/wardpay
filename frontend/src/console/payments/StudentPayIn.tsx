import { useMutation, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { formatDate, formatKobo, parseNairaToKobo } from '../../lib/format';
import { METHOD_LABELS } from '../Dashboard';
import { ErrorAlert, Field, Modal } from '../../lib/ui';
import { PayCardButton } from './PayCards';
import { ReceiptLink } from './ReceiptLink';

/** How a student's fees can be paid: their own account number, or a card payment link for the parent. */
export function StudentPayIn({ studentId }: { studentId: string }) {
  const { can } = useAuth();
  const [linking, setLinking] = useState(false);
  const account = useQuery({
    queryKey: ['virtual-account', studentId],
    queryFn: async () => unwrap(await api.GET('/api/v1/students/{student_id}/virtual-account', { params: { path: { student_id: studentId } } })),
  });
  return (
    <div className="card section">
      <div className="row between">
        <h2>Paying in</h2>
        {can('payments.record') && (
          <div className="row">
            <PayCardButton studentId={studentId} />
            <button className="secondary small" onClick={() => setLinking(true)}>
              Card payment link
            </button>
          </div>
        )}
      </div>
      {account.data ? (
        <div className="pay-in">
          <div className="muted small">Transfer to</div>
          <div className="account-number">{account.data.account_number}</div>
          <div>
            {account.data.bank_name} · {account.data.account_name}
          </div>
          <p className="hint">Any transfer into this account is matched to this student automatically.</p>
        </div>
      ) : (
        <p className="muted">No account number yet. Issue one from Payment settings.</p>
      )}
      <RecentPayments studentId={studentId} />
      {linking && <PaymentLink studentId={studentId} onClose={() => setLinking(false)} />}
    </div>
  );
}

function RecentPayments({ studentId }: { studentId: string }) {
  const { can } = useAuth();
  const payments = useQuery({
    queryKey: ['payments', 'student', studentId],
    enabled: can('payments.view'),
    queryFn: async () => unwrap(await api.GET('/api/v1/payments', { params: { query: { student_id: studentId, limit: 10 } } })),
  });
  if (!payments.data || payments.data.items.length === 0) return null;
  return (
    <>
      <h3>Recent payments</h3>
      <table className="compact">
        <tbody>
          {payments.data.items.map((p) => (
            <tr key={p.id} className={p.reversed || p.kind === 'reversal' ? 'retired' : ''}>
              <td>{formatDate(p.received_at)}</td>
              <td>{p.kind === 'reversal' ? 'Reversal' : METHOD_LABELS[p.method]}</td>
              <td className="num">{formatKobo(p.amount_kobo)}</td>
              <td>{p.receipt_id && p.receipt_number && <ReceiptLink receiptId={p.receipt_id} number={p.receipt_number} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function PaymentLink({ studentId, onClose }: { studentId: string; onClose: () => void }) {
  const [email, setEmail] = useState('');
  const [amount, setAmount] = useState('');
  const kobo = amount ? parseNairaToKobo(amount) : null;
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/students/{student_id}/payment-link', {
          params: { path: { student_id: studentId } },
          body: { email, amount_kobo: kobo },
        }),
      ),
  });
  return (
    <Modal title="Card payment link" onClose={onClose}>
      {create.data ? (
        <>
          <p>
            Send this link to the parent. It's for {formatKobo(create.data.amount_kobo)}
            {create.data.charge_kobo > 0 && (
              <>
                , plus a {formatKobo(create.data.charge_kobo)} payment charge the parent pays (your school's setting): {formatKobo(create.data.amount_kobo + create.data.charge_kobo)} in all
              </>
            )}
            .
          </p>
          <input readOnly value={create.data.authorization_url} onFocus={(e) => e.target.select()} aria-label="Payment link" />
          <div className="row end">
            <button onClick={() => void navigator.clipboard?.writeText(create.data.authorization_url)}>Copy link</button>
            <button className="secondary" onClick={onClose}>
              Done
            </button>
          </div>
        </>
      ) : (
        <form
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <ErrorAlert error={create.error} />
          <Field label="Parent's email" hint="The payment provider sends its receipt here.">
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          <Field label="Amount (₦)" hint="Leave empty for everything owed." error={amount && kobo === null ? 'Not a valid amount' : undefined}>
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
          </Field>
          <div className="row end">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" disabled={create.isPending || (!!amount && kobo === null)}>
              Create link
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
