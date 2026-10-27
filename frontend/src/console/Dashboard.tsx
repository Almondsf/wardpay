import { useQuery } from '@tanstack/react-query';
import { Banknote, CircleCheck, Clock, FileText } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { formatDate, formatKobo } from '../lib/format';
import { Empty, ErrorAlert, Loading, Notice, PageHeader, Stat, Tabs } from '../lib/ui';
import { GettingStarted } from './GettingStarted';
import { useSessions } from './hooks';
import { useUnusualActivity } from './reports/Unusual';

type Overview = Schemas['OverviewOut'];
type Roll = Schemas['RollSummaryOut'];

export const METHOD_LABELS: Record<Schemas['PaymentMethod'], string> = {
  virtual_account: 'Transfer to student account',
  card: 'Card',
  checkout_transfer: 'Bank transfer (online)',
  ussd: 'USSD',
  teller: 'Bank teller',
  cash: 'Cash',
  pos: 'POS',
  bank_transfer: 'Transfer to school account',
};

export const AGING_LABELS: Record<Schemas['AgingBucket'], string> = {
  not_due: 'Not yet due',
  '1_30': '1–30 days late',
  '31_60': '31–60 days late',
  '61_90': '61–90 days late',
  over_90: 'Over 90 days late',
};

/** Whole percent, rounded down so 99.6% never shows as 100% while money is still owed. */
export function percent(part: number, whole: number): number {
  return whole > 0 ? Math.floor((part * 100) / whole) : 0;
}

/**
 * Staff who see money get collections at a glance; everyone else (school admins)
 * gets the school at a glance: pupils, classes and records needing attention.
 */
export function Dashboard() {
  const { user, can } = useAuth();
  const first = user?.full_name.split(' ')[0] ?? '';
  if (!can('reports.view')) {
    return (
      <>
        <PageHeader title={`Welcome, ${first}`} subtitle={user?.school?.name} />
        <GettingStarted />
        <SchoolAtAGlance />
      </>
    );
  }
  return <MoneyDashboard first={first} schoolName={user?.school?.name ?? ''} />;
}

function MoneyDashboard({ first, schoolName }: { first: string; schoolName: string }) {
  const { can } = useAuth();
  const [termId, setTermId] = useState('');
  const sessions = useSessions();
  const overview = useQuery({
    queryKey: ['reports-overview', termId],
    staleTime: 0, // money figures are re-read every time the page is shown
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/reports/overview', {
          params: { query: { term_id: termId || undefined } },
        }),
      ),
  });
  const terms = (sessions.data ?? []).flatMap((s) =>
    s.terms.map((t) => ({
      id: t.id,
      label: `${s.name} ${t.label}`,
      current: t.is_current,
    })),
  );

  return (
    <>
      <PageHeader
        title={`Welcome, ${first}`}
        subtitle={schoolName}
        actions={
          terms.length > 0 && (
            <select aria-label="Term" value={termId || overview.data?.term_id || ''} onChange={(e) => setTermId(e.target.value)}>
              {terms.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                  {t.current ? ' (current)' : ''}
                </option>
              ))}
            </select>
          )
        }
      />
      <GettingStarted />
      {can('staff.manage') && <UnusualNote />}
      {overview.isPending ? (
        <Loading />
      ) : overview.isError ? (
        <ErrorAlert error={overview.error} />
      ) : (
        <Figures data={overview.data} pupils={can('students.view') ? <SchoolAtAGlance withTerm={false} /> : null} />
      )}
    </>
  );
}

/** For the proprietor: a line when there's something in Unusual activity worth a look. */
function UnusualNote() {
  const found = useUnusualActivity(30);
  if (!found.data || found.data.count === 0) return null;
  const serious = found.data.signals.filter((s) => s.serious);
  return (
    <Notice>
      {serious.length > 0
        ? `Worth a look: ${serious.map((s) => s.title.toLowerCase()).join(', ')}.`
        : 'Some activity this month is worth a glance.'}{' '}
      <Link to="/console/reports?tab=unusual">See unusual activity →</Link>
    </Notice>
  );
}

type Panel = 'money' | 'pace' | 'classes' | 'aging' | 'pupils';

function Figures({ data, pupils }: { data: Overview; pupils: ReactNode }) {
  const [panel, setPanel] = useState<Panel>('money');
  if (data.invoices === 0) {
    return (
      <>
        <Empty>
          No bills for {data.term_label} yet. <Link to="/console/billing">Generate the term's bills</Link> and this page fills in as
          payments arrive.
        </Empty>
        {pupils && (
          <>
            <h2 className="section-title">Pupils</h2>
            {pupils}
          </>
        )}
      </>
    );
  }
  const rate = percent(data.collected_kobo, data.billed_kobo);
  const tabs: { key: Panel; label: string }[] = [
    { key: 'money', label: 'Money in' },
    { key: 'pace', label: 'Pace and forecast' },
    { key: 'classes', label: 'By class' },
    { key: 'aging', label: 'How late' },
    ...(pupils ? [{ key: 'pupils' as const, label: 'Pupils' }] : []),
  ];
  return (
    <>
      <div className="stats">
        <Stat
          label="Billed"
          value={formatKobo(data.billed_kobo)}
          note={`${data.invoices} bills · ${data.term_label}`}
          icon={<FileText size={18} />}
        />
        <Stat label="Collected" value={formatKobo(data.collected_kobo)} tone="ok" icon={<CircleCheck size={18} />}>
          <Progress value={rate} label={`${rate}% of the term's bills`} />
        </Stat>
        <Stat
          label="Outstanding"
          value={formatKobo(data.outstanding_kobo)}
          tone={data.outstanding_kobo > 0 ? 'warn' : undefined}
          icon={<Clock size={18} />}
          note={<Link to="/console/reports">{data.students_owing} students owe</Link>}
        />
        <Stat
          label="Received today"
          value={formatKobo(data.received_today_kobo)}
          tone="info"
          icon={<Banknote size={18} />}
          note={`${formatKobo(data.received_7_days_kobo)} in the last 7 days`}
        />
      </div>

      <div className="card mini-stats">
        <div>
          <span className="stat-label">Older debts</span>
          <span className="stat-value">{formatKobo(data.older_outstanding_kobo)}</span>
          <span className="stat-note">Earlier terms, balances brought forward, one-off charges</span>
        </div>
        <div>
          <span className="stat-label">Credit held</span>
          <span className="stat-value">{formatKobo(data.credit_kobo)}</span>
          <span className="stat-note">Paid ahead; used on the next bill</span>
        </div>
        <div className={data.pending_approval > 0 ? 'stat warn' : 'stat'}>
          <span className="stat-label">Awaiting approval</span>
          <span className="stat-value">{formatKobo(data.pending_approval_kobo)}</span>
          <span className="stat-note">
            {data.pending_approval > 0 ? (
              <Link to="/console/payments">{data.pending_approval} cash or teller entries to check</Link>
            ) : (
              'Nothing waiting'
            )}
          </span>
        </div>
      </div>

      <section className="card panel">
        <div className="panel-head">
          <Tabs tabs={tabs} value={panel} onChange={setPanel} label="Details" />
          {panel === 'classes' && (
            <Link to="/console/reports" className="small">
              Debtors list →
            </Link>
          )}
          {panel === 'pupils' && (
            <Link to="/console/classes" className="small">
              Classes →
            </Link>
          )}
        </div>
        {panel === 'money' && (
          <div className="panel-body split">
            <section aria-label="Money received per day">
              <h2>Money received per day</h2>
              <p className="muted small">
                {formatKobo(data.received_this_term_kobo)} since {formatDate(data.term_start)}, after any reversals.
              </p>
              <DailyChart days={data.by_day} />
            </section>
            <section aria-label="How parents paid">
              <h2>How parents paid</h2>
              {data.by_method.length === 0 ? (
                <p className="muted">No payments this term yet.</p>
              ) : (
                <ul className="bars">
                  {data.by_method.map((m) => (
                    <li key={m.method}>
                      <div className="row between small">
                        <span>{METHOD_LABELS[m.method]}</span>
                        <span>
                          {formatKobo(m.amount_kobo)} <span className="muted">· {m.payments}</span>
                        </span>
                      </div>
                      <Progress value={percent(m.amount_kobo, data.received_this_term_kobo)} />
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
        {panel === 'pace' && <PaceAndForecast />}
        {panel === 'classes' && (
          <section className="panel-body flush table-scroll" aria-label="By class">
            <table>
              <thead>
                <tr>
                  <th>Class</th>
                  <th className="num">Billed</th>
                  <th className="num">Collected</th>
                  <th className="num">Outstanding</th>
                  <th style={{ width: '22%' }}>Collected</th>
                  <th className="num">Owing</th>
                </tr>
              </thead>
              <tbody>
                {data.by_class.map((c) => {
                  const classRate = percent(c.collected_kobo, c.billed_kobo);
                  return (
                    <tr key={c.class_level_id ?? 'none'}>
                      <td>{c.class_name}</td>
                      <td className="num">{formatKobo(c.billed_kobo)}</td>
                      <td className="num">{formatKobo(c.collected_kobo)}</td>
                      <td className="num">{formatKobo(c.outstanding_kobo)}</td>
                      <td>
                        <Progress value={classRate} label={`${classRate}%`} />
                      </td>
                      <td className="num">
                        {c.students_owing} of {c.invoices}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        )}
        {panel === 'aging' && (
          <section className="panel-body" aria-label="How late the debts are">
            <p className="muted small">Everything still owed, by how long ago each bill was due.</p>
            <div className="aging">
              {data.aging.map((a) => (
                <div
                  key={a.bucket}
                  className={`aging-cell ${a.amount_kobo === 0 || a.bucket === 'not_due' ? '' : a.bucket === '1_30' ? 'warn' : 'danger'}`}
                >
                  <div className="small muted">{AGING_LABELS[a.bucket]}</div>
                  <div className="stat-value">{formatKobo(a.amount_kobo)}</div>
                  <div className="small muted">{a.invoices} bills</div>
                </div>
              ))}
            </div>
          </section>
        )}
        {panel === 'pupils' && <div className="panel-body nested">{pupils}</div>}
      </section>
    </>
  );
}

type Insights = Schemas['InsightsOut'];
type Curve = Schemas['TermCurveOut'];

/** This term's collection curve against last term's, and what's due (and likely to come in) next. */
function PaceAndForecast() {
  const insights = useQuery({
    queryKey: ['reports-insights'],
    staleTime: 0,
    queryFn: async () => unwrap(await api.GET('/api/v1/reports/insights')),
  });
  if (insights.isPending) return <Loading />;
  if (insights.isError) return <ErrorAlert error={insights.error} />;
  return <PaceFigures data={insights.data} />;
}

function share(curve: Curve, day: number): number {
  const values = curve.collected_kobo;
  if (values.length === 0) return 0;
  return percent(values[Math.min(day, values.length - 1)]!, curve.billed_kobo);
}

function PaceFigures({ data }: { data: Insights }) {
  const { current, previous } = data;
  const comparable = previous && previous.billed_kobo > 0 ? previous : null;
  return (
    <div className="panel-body split">
      <section aria-label="Collection curve">
        <h2>Collection curve</h2>
        {!current || current.billed_kobo === 0 ? (
          <p className="muted">No bills for the current term yet.</p>
        ) : (
          <>
            <p className="small">
              Day {current.days} of {current.label}: <strong>{share(current, current.days)}%</strong> of the term's bills collected.
              {comparable && (
                <>
                  {' '}
                  By day {current.days} last term: <strong>{share(comparable, current.days)}%</strong>.
                </>
              )}
            </p>
            <CurveChart current={current} previous={comparable} />
          </>
        )}
      </section>
      <section aria-label="Cash forecast">
        <h2>Cash forecast</h2>
        <p className="muted small">
          {formatKobo(data.overdue_kobo)} is overdue now. On a payment plan, only the instalments that fall due count.
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Next</th>
                <th className="num">Falling due</th>
                <th className="num">In at the recent pace</th>
                <th className="num">Spending at the recent pace</th>
              </tr>
            </thead>
            <tbody>
              {data.windows.map((w) => (
                <tr key={w.days}>
                  <td className="nowrap">{w.days} days</td>
                  <td className="num">{formatKobo(w.due_kobo)}</td>
                  <td className="num">{formatKobo(w.pace_in_kobo)}</td>
                  <td className="num">{formatKobo(w.pace_out_kobo)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted small">
          The recent pace is the last 30 days of money in and approved spending, carried forward. A guide, not a promise.
        </p>
      </section>
    </div>
  );
}

/** Percent of each term's bills collected by day since the term began. Plain SVG. */
function CurveChart({ current, previous }: { current: Curve; previous: Curve | null }) {
  const width = 600;
  const height = 160;
  const span = Math.max(current.days, previous?.days ?? 0, 1);
  const line = (curve: Curve) =>
    curve.collected_kobo
      .map(
        (v, day) =>
          `${((day / span) * width).toFixed(1)},${(height - (Math.min(percent(v, curve.billed_kobo), 100) / 100) * height).toFixed(1)}`,
      )
      .join(' ');
  return (
    <>
      <svg viewBox={`0 0 ${width} ${height}`} className="chart" role="img" aria-label="Share of bills collected by day of term">
        <line x1={0} x2={width} y1={height} y2={height} className="chart-axis" />
        <line x1={0} x2={width} y1={0} y2={0} className="chart-axis" />
        {previous && <polyline points={line(previous)} className="chart-line previous" />}
        <polyline points={line(current)} className="chart-line" />
      </svg>
      <div className="row between small muted">
        <span>Day 0</span>
        <span>
          <span className="legend-swatch" /> This term
          {previous && (
            <>
              {' '}
              <span className="legend-swatch previous" /> {previous.label}
            </>
          )}
        </span>
        <span>Day {span}</span>
      </div>
    </>
  );
}

function SchoolAtAGlance({ withTerm = true }: { withTerm?: boolean }) {
  const roll = useQuery({
    queryKey: ['school-roll'],
    staleTime: 0,
    queryFn: async () => unwrap(await api.GET('/api/v1/school/roll')),
  });
  if (roll.isPending) return <Loading />;
  if (roll.isError) return <ErrorAlert error={roll.error} />;
  return <RollFigures data={roll.data} withTerm={withTerm} />;
}

function RollFigures({ data, withTerm }: { data: Roll; withTerm: boolean }) {
  if (data.on_roll === 0) {
    return (
      <Empty>
        No pupils yet. <Link to="/console/students/import">Import your student spreadsheet</Link> and this page shows your classes.
      </Empty>
    );
  }
  const placedRate = percent(data.placed, data.on_roll);
  const attention = (
    [
      ['unplaced', data.unplaced, 'have no class this term, so they get no bill'],
      ['parent', data.no_parent, 'have no parent or guardian recorded, so nobody gets their bills or receipts'],
      ['consent', data.no_consent, 'have no parent whose consent is recorded (NDPA)'],
      ['account_number', data.no_account_number, 'have no bank account number yet (the bursar or proprietor sets these up)'],
    ] as const
  ).filter(([, count]) => count > 0);
  return (
    <>
      {withTerm && data.term_label && <h2 className="section-title">{data.term_label}</h2>}
      <div className="stats">
        <Stat
          label="Pupils on roll"
          value={data.on_roll.toLocaleString()}
          note={`${data.joined_this_term} joined this term · ${data.left_this_term} left`}
        />
        <Stat label="In a class this term" value={data.placed.toLocaleString()} tone={data.unplaced > 0 ? 'warn' : 'ok'}>
          <Progress value={placedRate} label={`${placedRate}% of pupils`} />
        </Stat>
        <Stat label="Parents and guardians" value={data.parents.toLocaleString()} note="Of pupils on roll" />
      </div>

      <section className="card section" aria-label="Needs attention">
        <h2>Needs attention</h2>
        {attention.length === 0 ? (
          <p className="muted">Every pupil has a class, a parent with consent recorded, and an account number.</p>
        ) : (
          <ul className="attention">
            {attention.map(([show, count, why]) => (
              <li key={show}>
                <Link to={`/console/students?show=${show}`}>
                  {count} {count === 1 ? 'pupil' : 'pupils'}
                </Link>{' '}
                {why}.
              </li>
            ))}
          </ul>
        )}
      </section>

      {data.by_class.length > 0 && (
        <section className="card table-card" aria-label="Pupils by class">
          <div className="row between card-pad">
            <h2>Pupils by class</h2>
            <Link to="/console/classes" className="small">
              Classes →
            </Link>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Class</th>
                  <th>Arms</th>
                  <th className="num">Boys</th>
                  <th className="num">Girls</th>
                  <th className="num">Pupils</th>
                </tr>
              </thead>
              <tbody>
                {data.by_class.map((c) => (
                  <tr key={c.class_level_id}>
                    <td className="nowrap">
                      <Link to={`/console/students?class=${c.class_level_id}`}>{c.class_name}</Link>
                    </td>
                    <td className="small">{c.arms.map((a) => `${a.arm_name}: ${a.pupils}`).join(' · ')}</td>
                    <td className="num">{c.boys}</td>
                    <td className="num">{c.girls}</td>
                    <td className="num">{c.pupils}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th>Total</th>
                  <td />
                  <td className="num">{data.by_class.reduce((n, c) => n + c.boys, 0)}</td>
                  <td className="num">{data.by_class.reduce((n, c) => n + c.girls, 0)}</td>
                  <td className="num">{data.by_class.reduce((n, c) => n + c.pupils, 0)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

function Progress({ value, label }: { value: number; label?: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className="progress-row">
      <div className="progress" role="progressbar" aria-valuenow={clamped} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
        <div className="progress-fill" style={{ width: `${clamped}%` }} />
      </div>
      {label && <span className="small muted nowrap">{label}</span>}
    </div>
  );
}

/** Bars per day; negative days (reversals) dip below the line. Plain SVG, no chart library. */
export function DailyChart({ days }: { days: { day: string; amount_kobo: number; payments?: number }[] }) {
  if (days.length === 0) return <p className="muted">No payments yet.</p>;
  const width = 600;
  const height = 160;
  const max = Math.max(...days.map((d) => d.amount_kobo), 1);
  const min = Math.min(...days.map((d) => d.amount_kobo), 0);
  const range = max - min;
  const zero = height * (max / range);
  const step = width / days.length;
  const best = days.reduce((a, b) => (b.amount_kobo > a.amount_kobo ? b : a));
  return (
    <>
      <svg viewBox={`0 0 ${width} ${height}`} className="chart" role="img" aria-label={`Money received on ${days.length} days`}>
        <line x1={0} x2={width} y1={zero} y2={zero} className="chart-axis" />
        {days.map((d, i) => {
          const h = (Math.abs(d.amount_kobo) / range) * height;
          return (
            <rect
              key={d.day}
              x={i * step + step * 0.15}
              width={step * 0.7}
              y={d.amount_kobo >= 0 ? zero - h : zero}
              height={Math.max(h, 1)}
              className={d.amount_kobo >= 0 ? 'chart-bar' : 'chart-bar negative'}
            >
              <title>{`${formatDate(d.day)}: ${formatKobo(d.amount_kobo)}${d.payments === undefined ? '' : ` (${d.payments} payments)`}`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="row between small muted">
        <span>{formatDate(days[0]!.day)}</span>
        <span>
          Best day: {formatDate(best.day)}, {formatKobo(best.amount_kobo)}
        </span>
        <span>{formatDate(days[days.length - 1]!.day)}</span>
      </div>
    </>
  );
}
