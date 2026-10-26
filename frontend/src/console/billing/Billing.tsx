import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { formatKobo } from '../../lib/format';
import { Empty, ErrorAlert, Field, Loading, Modal, PageHeader, Pager, StatusBadge } from '../../lib/ui';
import { useClassLevels, useCurrentTerm, useSessions } from '../hooks';
import { ClassCharge } from './ClassCharge';

const PAGE = 50;
type InvoiceStatus = 'unpaid' | 'part_paid' | 'paid' | 'void' | 'outstanding';

export function Billing() {
  const { can } = useAuth();
  const sessions = useSessions();
  const current = useCurrentTerm();
  const levels = useClassLevels();
  const [termId, setTermId] = useState('');
  const [status, setStatus] = useState<InvoiceStatus | ''>('');
  const [levelId, setLevelId] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [charging, setCharging] = useState(false);
  const term = termId || current.data?.id || '';

  const summary = useQuery({
    queryKey: ['billing-summary', term],
    enabled: !!term,
    queryFn: async () => unwrap(await api.GET('/api/v1/terms/{term_id}/billing-summary', { params: { path: { term_id: term } } })),
  });
  const invoices = useQuery({
    queryKey: ['invoices', term, status, levelId, search, offset],
    enabled: !!term,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/invoices', {
          params: {
            query: { term_id: term, status: status || undefined, class_level_id: levelId || undefined, search: search || undefined, limit: PAGE, offset },
          },
        }),
      ),
  });
  const reset = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setOffset(0);
  };

  if (current.isPending) return <Loading />;
  if (!term) return <Empty>Set up the school calendar first.</Empty>;
  const s = summary.data;

  return (
    <>
      <PageHeader
        title="Billing"
        subtitle="The term's bills and what's still owed."
        actions={
          <>
            <select aria-label="Term" value={term} onChange={(e) => reset(setTermId)(e.target.value)}>
              {(sessions.data ?? []).flatMap((ses) =>
                ses.terms.map((t) => (
                  <option key={t.id} value={t.id}>
                    {ses.name} · {t.label}
                    {t.is_current ? ' (current)' : ''}
                  </option>
                )),
              )}
            </select>
            {can('invoices.issue') && (
              <>
                <button className="secondary" onClick={() => setCharging(true)}>
                  Add a fee to classes
                </button>
                <button onClick={() => setGenerating(true)}>Generate invoices</button>
              </>
            )}
          </>
        }
      />
      {s && (
        <div className="card mini-stats">
          <div>
            <span className="stat-label">Billed</span>
            <span className="stat-value">{formatKobo(s.billed_kobo)}</span>
            <span className="stat-note">{s.invoices} bills</span>
          </div>
          <div className="stat ok">
            <span className="stat-label">Paid</span>
            <span className="stat-value">{formatKobo(s.paid_kobo)}</span>
            <div className="progress-row">
              <div className="progress">
                <div className="progress-fill" style={{ width: `${s.billed_kobo > 0 ? Math.min(100, Math.floor((s.paid_kobo * 100) / s.billed_kobo)) : 0}%` }} />
              </div>
              <span className="small muted">{s.billed_kobo > 0 ? Math.floor((s.paid_kobo * 100) / s.billed_kobo) : 0}%</span>
            </div>
          </div>
          <div className={s.outstanding_kobo > 0 ? 'stat warn' : 'stat'}>
            <span className="stat-label">Outstanding</span>
            <span className="stat-value">{formatKobo(s.outstanding_kobo)}</span>
          </div>
          <div>
            <span className="stat-label">Not billed yet</span>
            <span className="stat-value">{Math.max(0, s.students_enrolled - s.students_invoiced)}</span>
            <span className="stat-note">of {s.students_enrolled} pupils in a class</span>
          </div>
        </div>
      )}
      <div className="filters">
        <input type="search" placeholder="Student name, admission or invoice number" value={search} onChange={(e) => reset(setSearch)(e.target.value)} />
        <select aria-label="Status" value={status} onChange={(e) => reset(setStatus)(e.target.value as InvoiceStatus | '')}>
          <option value="">Any status</option>
          <option value="outstanding">Owing</option>
          <option value="unpaid">Unpaid</option>
          <option value="part_paid">Part paid</option>
          <option value="paid">Paid</option>
          <option value="void">Void</option>
        </select>
        <select aria-label="Class" value={levelId} onChange={(e) => reset(setLevelId)(e.target.value)}>
          <option value="">All classes</option>
          {(levels.data ?? []).map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      </div>
      {invoices.isPending ? (
        <Loading />
      ) : invoices.isError ? (
        <ErrorAlert error={invoices.error} />
      ) : invoices.data.items.length === 0 ? (
        <Empty>No invoices match.</Empty>
      ) : (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Pupil</th>
                <th>Bill</th>
                <th className="num">Total</th>
                <th className="num">Left to pay</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {invoices.data.items.map((i) => (
                <tr key={i.id}>
                  <td>
                    <Link to={`/console/students/${i.student.id}`}>{i.student.full_name}</Link>
                    <div className="muted small">{i.student.admission_number}</div>
                  </td>
                  <td>
                    <Link to={`/console/invoices/${i.id}`} className="small">
                      #{i.number}
                    </Link>
                  </td>
                  <td className="num">{formatKobo(i.total_kobo)}</td>
                  <td className="num">{i.balance_kobo > 0 ? <strong>{formatKobo(i.balance_kobo)}</strong> : <span className="muted">—</span>}</td>
                  <td>
                    <StatusBadge status={i.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager total={invoices.data.total} limit={PAGE} offset={offset} onChange={setOffset} />
        </div>
      )}
      {generating && <Generate termId={term} onClose={() => setGenerating(false)} />}
      {charging && <ClassCharge termId={term} onClose={() => setCharging(false)} />}
    </>
  );
}


export function Generate({ termId, onClose }: { termId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [dueDate, setDueDate] = useState('');
  const [notify, setNotify] = useState(true);
  const run = useMutation({
    mutationFn: async (dryRun: boolean) => {
      let report = unwrap(
        await api.POST('/api/v1/terms/{term_id}/invoices/generate', {
          params: { path: { term_id: termId } },
          body: { dry_run: dryRun, due_date: dueDate || null, notify_parents: notify },
        }),
      );
      // A big school's bills are made in the background: check back until they're done
      while (report.pending && report.run_id) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        report = unwrap(await api.GET('/api/v1/billing-runs/{run_id}', { params: { path: { run_id: report.run_id! } } }));
      }
      return report;
    },
    onSuccess: (report) => {
      if (!report.dry_run) {
        void queryClient.invalidateQueries({ queryKey: ['invoices'] });
        void queryClient.invalidateQueries({ queryKey: ['billing-summary'] });
      }
    },
  });
  const report: Schemas['GenerationOut'] | undefined = run.data;
  const finished = report && !report.dry_run;

  return (
    <Modal title="Generate invoices" onClose={onClose}>
      <p>
        Every current student placed in a class this term gets one invoice: their class's fees, optional items they've signed up for, and their
        discounts. Students who already have an invoice for the term are skipped, so this is safe to run again after adding students.
      </p>
      <Field label="Payment due by (optional)">
        <input type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} disabled={!!finished} />
      </Field>
      <label className="checkbox">
        <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} disabled={!!finished} />
        Send each child's main parent a message with the bill (WhatsApp or text, during the day)
      </label>
      <ErrorAlert error={run.error} />
      {report && (
        <div className="card inset">
          <strong>
            {finished ? 'Done: ' : 'Preview: '}
            {report.invoices} invoices totalling {formatKobo(report.total_kobo)}
          </strong>
          {finished && report.notices_queued > 0 && <div>{report.notices_queued} parents will get a message about their bill.</div>}
          {report.discounts_kobo > 0 && <div>Including {formatKobo(report.discounts_kobo)} of discounts.</div>}
          {report.already_invoiced > 0 && <div className="muted">{report.already_invoiced} students already have an invoice.</div>}
          {report.no_fees_set.length > 0 && <div className="error-text">No fees set for: {report.no_fees_set.join(', ')}. Those students are skipped.</div>}
          {report.by_class.length > 0 && (
            <table className="compact">
              <tbody>
                {report.by_class.map((c) => (
                  <tr key={c.class_level_id}>
                    <td>{c.class_name}</td>
                    <td className="num">{c.invoices}</td>
                    <td className="num">{formatKobo(c.total_kobo)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <div className="row end">
        <button className="secondary" onClick={onClose}>
          {finished ? 'Close' : 'Cancel'}
        </button>
        {!finished && (
          <>
            <button className="secondary" onClick={() => run.mutate(true)} disabled={run.isPending}>
              Preview
            </button>
            <button onClick={() => run.mutate(false)} disabled={run.isPending || !report || report.invoices === 0}>
              {run.isPending && run.variables === false ? 'Making the bills…' : `Generate ${report ? report.invoices : ''} invoices`}
            </button>
          </>
        )}
      </div>
    </Modal>
  );
}
