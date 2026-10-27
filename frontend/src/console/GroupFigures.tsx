import { useMutation, useQuery } from '@tanstack/react-query';

import { api, unwrap } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { errorMessage, saveBlob } from '../lib/download';
import { formatKobo } from '../lib/format';
import { ErrorAlert, Loading, PageHeader } from '../lib/ui';
import { DailyChart } from './Dashboard';
import { Card } from './platform/Overview';

/** Head office of a school group: every branch side by side and the group's totals. Figures only. */
export function GroupFigures() {
  const { user, switchBranch } = useAuth();
  const report = useQuery({ queryKey: ['group-figures'], queryFn: async () => unwrap(await api.GET('/api/v1/reports/group')) });
  const download = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.GET('/api/v1/reports/group/export', { parseAs: 'blob' });
      if (error) throw new Error(errorMessage(error) ?? 'The download failed. Please try again.');
      saveBlob(data as Blob, 'all-branches.xlsx');
    },
  });
  const open = useMutation({ mutationFn: switchBranch });

  if (report.isPending) return <Loading />;
  if (report.isError) return <ErrorAlert error={report.error} />;
  const r = report.data;
  const t = r.totals;
  return (
    <>
      <PageHeader
        title="All branches"
        subtitle={`${r.group_name}: each branch’s current term, side by side. Money only; open a branch for its pupils.`}
        actions={
          <button className="secondary" onClick={() => download.mutate()} disabled={download.isPending}>
            {download.isPending ? 'Preparing…' : 'Download Excel'}
          </button>
        }
      />
      <ErrorAlert error={download.error} />
      <div className="stats">
        <Card label="Billed this term" value={formatKobo(t.billed_kobo)} note={`${t.pupils} pupils in ${r.branches.length} branches`} />
        <Card label="Collected" value={formatKobo(t.collected_kobo)} />
        <Card
          label="Still owed"
          value={formatKobo(t.total_outstanding_kobo)}
          note={`${formatKobo(t.older_outstanding_kobo)} from before this term · ${t.students_owing} pupils`}
          tone={t.total_outstanding_kobo > 0 ? 'warn' : undefined}
        />
        <Card
          label="Received, last 7 days"
          value={formatKobo(t.received_7_days_kobo)}
          note={`${formatKobo(t.received_today_kobo)} today · ${formatKobo(t.received_30_days_kobo)} in 30 days`}
        />
      </div>
      <section className="card" aria-label="Money received across the group">
        <h2>Money received, last 30 days</h2>
        <DailyChart days={r.by_day} />
      </section>
      <div className="table-wrap">
        <table className="table" aria-label="Branches">
          <thead>
            <tr>
              <th>Branch</th>
              <th className="num">Pupils</th>
              <th className="num">Billed this term</th>
              <th className="num">Collected</th>
              <th className="num">Still owed</th>
              <th className="num">Received, 7 days</th>
              <th className="num">Awaiting approval</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {r.branches.map((b) => (
              <tr key={b.school_id}>
                <td>
                  <strong>{b.name}</strong>
                  <div className="muted small">{b.term_label || 'No current term set'}</div>
                </td>
                <td className="num">{b.pupils}</td>
                <td className="num">{formatKobo(b.billed_kobo)}</td>
                <td className="num">{formatKobo(b.collected_kobo)}</td>
                <td className="num">
                  {formatKobo(b.total_outstanding_kobo)}
                  <div className="muted small">{b.students_owing} pupils</div>
                </td>
                <td className="num">{formatKobo(b.received_7_days_kobo)}</td>
                <td className="num">{b.pending_approval ? `${b.pending_approval} · ${formatKobo(b.pending_approval_kobo)}` : '—'}</td>
                <td>
                  {b.school_id !== user?.school?.id && (
                    <button className="small secondary" onClick={() => b.school_id && open.mutate(b.school_id)} disabled={open.isPending}>
                      Work in this branch
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th>All branches</th>
              <th className="num">{t.pupils}</th>
              <th className="num">{formatKobo(t.billed_kobo)}</th>
              <th className="num">{formatKobo(t.collected_kobo)}</th>
              <th className="num">{formatKobo(t.total_outstanding_kobo)}</th>
              <th className="num">{formatKobo(t.received_7_days_kobo)}</th>
              <th className="num">{t.pending_approval ? `${t.pending_approval} · ${formatKobo(t.pending_approval_kobo)}` : '—'}</th>
              <th />
            </tr>
          </tfoot>
        </table>
      </div>
      <ErrorAlert error={open.error} />
    </>
  );
}
