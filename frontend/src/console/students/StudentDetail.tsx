import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Ellipsis } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { formatDate, formatKobo, humanize, lagosToday } from '../../lib/format';
import { ErrorAlert, Field, Loading, Menu, Modal, Notice, StatusBadge, Tabs, fieldError } from '../../lib/ui';
import { StudentAccount } from '../billing/StudentAccount';
import { StudentPayIn } from '../payments/StudentPayIn';
import { armOptions, useClassLevels } from '../hooks';
import { FamilyLinkDialog } from './FamilyLink';
import { FamilyPlanDialog } from './FamilyPlan';
import { LANGUAGE_LABELS, ParentCallsDialog } from './ParentCalls';
import { saveBlob } from '../../lib/download';

type Detail = Schemas['StudentDetailOut'];
type Guardian = Schemas['GuardianOut'];

export function StudentDetail() {
  const { studentId = '' } = useParams();
  const { can } = useAuth();
  const manage = can('students.manage');
  const queryClient = useQueryClient();
  const student = useQuery({
    queryKey: ['student', studentId],
    queryFn: async () => unwrap(await api.GET('/api/v1/students/{student_id}', { params: { path: { student_id: studentId } } })),
  });
  const [dialog, setDialog] = useState<'edit' | 'class' | 'withdraw' | 'guardian' | null>(null);
  const [editingGuardian, setEditingGuardian] = useState<Guardian | null>(null);
  const [erasing, setErasing] = useState<EraseTarget | null>(null);
  const [payLink, setPayLink] = useState<Guardian | null>(null);
  const [calls, setCalls] = useState<Guardian | null>(null);
  const [familyPlan, setFamilyPlan] = useState<Guardian | null>(null);
  const [tab, setTab] = useState<'overview' | 'fees' | 'payments'>('overview');
  // Same query as the Fees tab, so the balance in the header costs no extra request
  const account = useQuery({
    queryKey: ['account', studentId],
    enabled: can('fees.view'),
    queryFn: async () => unwrap(await api.GET('/api/v1/students/{student_id}/account', { params: { path: { student_id: studentId } } })),
  });
  const exportData = useMutation({
    mutationFn: async (g: Guardian) => {
      const { data, response } = await api.GET('/api/v1/guardians/{guardian_id}/export', {
        params: { path: { guardian_id: g.id } },
        parseAs: 'blob',
      });
      if (!response.ok || !data) throw new Error('Could not export the data.');
      saveBlob(data as Blob, `parent-data-${g.full_name.replace(/\W+/g, '-').toLowerCase()}.json`);
    },
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['student', studentId] });
    void queryClient.invalidateQueries({ queryKey: ['students'] });
  };
  const path = { params: { path: { student_id: studentId } } };
  const reinstate = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/students/{student_id}/reinstate', path)),
    onSuccess: refresh,
  });
  const guardianAction = useMutation({
    mutationFn: async ({ kind, guardianId }: { kind: 'primary' | 'remove'; guardianId: string }) => {
      const p = { params: { path: { student_id: studentId, guardian_id: guardianId } } };
      if (kind === 'primary') return unwrap(await api.POST('/api/v1/students/{student_id}/guardians/{guardian_id}/primary', p));
      return unwrap(await api.DELETE('/api/v1/students/{student_id}/guardians/{guardian_id}', p));
    },
    onSuccess: refresh,
  });
  const setReminders = useMutation({
    mutationFn: async ({ guardianId, reminders }: { guardianId: string; reminders: boolean }) =>
      unwrap(
        await api.PUT('/api/v1/guardians/{guardian_id}/reminders', { params: { path: { guardian_id: guardianId } }, body: { reminders } }),
      ),
    onSuccess: refresh,
  });
  const remind = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/students/{student_id}/remind', path)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['messages'] }),
  });

  if (student.isPending) return <Loading />;
  if (student.isError) return <ErrorAlert error={student.error} />;
  const s = student.data;
  const current = s.enrollments.find((e) => e.is_current);

  const place = current ? `${current.placement.class_level_name} ${current.placement.arm_name}` : 'Not placed this term';
  const initials = `${s.first_name[0] ?? ''}${s.last_name[0] ?? ''}`.toUpperCase();
  const tabs = [
    { key: 'overview' as const, label: 'Overview' },
    ...(can('fees.view') ? [{ key: 'fees' as const, label: 'Fees' }] : []),
    ...(can('payments.view') ? [{ key: 'payments' as const, label: 'Payments' }] : []),
  ];
  const more = manage && (s.status === 'active' || !s.erased);

  return (
    <>
      <Link to="/console/students" className="back-link">
        <ArrowLeft size={16} /> Students
      </Link>
      <div className="profile-head">
        <span className="avatar" aria-hidden="true">
          {initials}
        </span>
        <div className="who">
          <h1>{s.full_name}</h1>
          <div className="meta">
            <span>{s.admission_number}</span>
            <span className="dot" />
            <span>{place}</span>
            <StatusBadge status={s.status} />
          </div>
        </div>
        <div className="profile-actions">
          {can('messaging.manage') && s.status === 'active' && (
            <button onClick={() => remind.mutate()} disabled={remind.isPending || remind.isSuccess}>
              {remind.isSuccess ? 'Reminder sent' : 'Send reminder'}
            </button>
          )}
          {manage && (
            <button className="secondary" onClick={() => setDialog('edit')}>
              Edit details
            </button>
          )}
          {more && (
            <Menu label="More">
              {s.status === 'active' ? (
                <>
                  <button onClick={() => setDialog('class')}>Change class</button>
                  <button onClick={() => setDialog('withdraw')}>Withdraw</button>
                </>
              ) : (
                <>
                  <button onClick={() => reinstate.mutate()}>Reinstate</button>
                  {can('school.manage') && (
                    <button className="danger-item" onClick={() => setErasing({ kind: 'student', id: s.id, name: s.full_name })}>
                      Erase personal details
                    </button>
                  )}
                </>
              )}
            </Menu>
          )}
        </div>
      </div>
      {remind.isSuccess && <Notice>A reminder is on its way to {s.first_name}'s main parent. You can follow it under Messages.</Notice>}
      {s.erased && (
        <div className="alert info" role="status">
          This former pupil's personal details were erased. Their bills and payments are kept, anonymised, so the accounts still add up.
        </div>
      )}
      <ErrorAlert error={reinstate.error ?? guardianAction.error ?? setReminders.error ?? remind.error ?? exportData.error} />

      {account.data && (
        <div className="card mini-stats">
          <div className={account.data.outstanding_kobo > 0 ? 'stat warn' : 'stat ok'}>
            <span className="stat-label">Owed</span>
            <span className="stat-value">{formatKobo(account.data.outstanding_kobo)}</span>
          </div>
          <div>
            <span className="stat-label">Billed</span>
            <span className="stat-value">{formatKobo(account.data.billed_kobo)}</span>
          </div>
          <div>
            <span className="stat-label">Paid</span>
            <span className="stat-value">{formatKobo(account.data.paid_kobo)}</span>
          </div>
        </div>
      )}

      {tabs.length > 1 && <Tabs tabs={tabs} value={tab} onChange={setTab} label="Pupil" />}

      {tab === 'overview' && (
        <div className="grid-2">
          <div className="card">
            <h2>Details</h2>
            <dl className="details">
              <dt>Gender</dt>
              <dd>{s.gender ? humanize(s.gender) : '—'}</dd>
              <dt>Date of birth</dt>
              <dd>{formatDate(s.date_of_birth)}</dd>
              {s.left_on && (
                <>
                  <dt>Left on</dt>
                  <dd>{formatDate(s.left_on)}</dd>
                </>
              )}
            </dl>
            <h3>Class history</h3>
            {s.enrollments.length === 0 ? (
              <p className="muted">Not placed in any term yet.</p>
            ) : (
              <ul className="plain">
                {s.enrollments.map((e) => (
                  <li key={e.term_id} className="row between">
                    <span>
                      <span className="muted">
                        {e.session_name} {e.term_label}
                      </span>{' '}
                      · <strong>{`${e.placement.class_level_name} ${e.placement.arm_name}`}</strong>
                    </span>
                    {e.is_current && <span className="badge ok">Current</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="card">
            <div className="row between">
              <h2>Parents and guardians</h2>
              {manage && (
                <button className="secondary small" onClick={() => setDialog('guardian')}>
                  Add
                </button>
              )}
            </div>
            {s.guardians.length === 0 && <p className="muted">No guardian yet. Add one so they get receipts and reminders.</p>}
            {s.guardians.map((g) => (
              <div className="person" key={g.id}>
                <span className="avatar" aria-hidden="true">
                  {g.full_name
                    .split(/\s+/)
                    .filter((w) => !/^(mr|mrs|ms|miss|dr|chief|prof)\.?$/i.test(w))
                    .slice(0, 2)
                    .map((w) => w[0]?.toUpperCase())
                    .join('')}
                </span>
                <div className="person-main">
                  <strong>{g.full_name}</strong> <span className="muted">· {humanize(g.relationship)}</span>
                  <div className="small muted">
                    {g.phone}
                    {g.email && ` · ${g.email}`}
                  </div>
                  <div className="badges">
                    {g.is_primary && <span className="badge ok">Gets fee notices</span>}
                    {!g.reminders && !g.erased && <span className="badge warn">Reminders off</span>}
                    {g.prefers_calls && !g.erased && <span className="badge">Phone calls ({LANGUAGE_LABELS[g.language]})</span>}
                    {g.erased && <span className="badge">Details erased</span>}
                  </div>
                </div>
                {!g.erased && can('payments.record') && (
                  <button className="secondary small" onClick={() => setPayLink(g)}>
                    Payment link
                  </button>
                )}
                {!g.erased && can('invoices.issue') && (
                  <button className="secondary small" onClick={() => setFamilyPlan(g)}>
                    Instalment plan
                  </button>
                )}
                {manage && !g.erased && (
                  <Menu label={`Actions for ${g.full_name}`} icon={<Ellipsis size={18} />}>
                    <button onClick={() => setEditingGuardian(g)}>Edit</button>
                    {!g.is_primary && (
                      <button onClick={() => guardianAction.mutate({ kind: 'primary', guardianId: g.id })}>Make main contact</button>
                    )}
                    <button onClick={() => setReminders.mutate({ guardianId: g.id, reminders: !g.reminders })}>
                      {g.reminders ? 'Stop reminders' : 'Restart reminders'}
                    </button>
                    <button onClick={() => setCalls(g)}>Phone calls</button>
                    <button onClick={() => exportData.mutate(g)} disabled={exportData.isPending}>
                      Export data
                    </button>
                    <hr />
                    <button
                      onClick={() =>
                        window.confirm(`Unlink ${g.full_name} from ${s.first_name}?`) &&
                        guardianAction.mutate({ kind: 'remove', guardianId: g.id })
                      }
                    >
                      Unlink
                    </button>
                    {can('school.manage') && (
                      <button className="danger-item" onClick={() => setErasing({ kind: 'guardian', id: g.id, name: g.full_name })}>
                        Erase
                      </button>
                    )}
                  </Menu>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {tab === 'fees' && <StudentAccount studentId={s.id} />}
      {tab === 'payments' && <StudentPayIn studentId={s.id} />}
      {dialog === 'edit' && <EditStudent student={s} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === 'class' && <ChangeClass student={s} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === 'withdraw' && <Withdraw student={s} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === 'guardian' && <AddGuardian student={s} onClose={() => setDialog(null)} onDone={refresh} />}
      {editingGuardian && <EditGuardian guardian={editingGuardian} onClose={() => setEditingGuardian(null)} onDone={refresh} />}
      {erasing && <Erase target={erasing} onClose={() => setErasing(null)} onDone={refresh} />}
      {calls && <ParentCallsDialog guardian={calls} onClose={() => setCalls(null)} onDone={refresh} />}
      {familyPlan && (
        <FamilyPlanDialog
          guardianId={familyPlan.id}
          name={familyPlan.full_name}
          remindersOn={familyPlan.reminders}
          onClose={() => setFamilyPlan(null)}
        />
      )}
      {payLink && <FamilyLinkDialog guardianId={payLink.id} name={payLink.full_name} onClose={() => setPayLink(null)} />}
    </>
  );
}

interface DialogProps {
  student: Detail;
  onClose: () => void;
  onDone: () => void;
}

function EditStudent({ student, onClose, onDone }: DialogProps) {
  const [form, setForm] = useState({
    admission_number: student.admission_number,
    first_name: student.first_name,
    middle_name: student.middle_name,
    last_name: student.last_name,
    gender: (student.gender ?? '') as '' | 'male' | 'female',
    date_of_birth: student.date_of_birth ?? '',
  });
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PATCH('/api/v1/students/{student_id}', {
          params: { path: { student_id: student.id } },
          body: { ...form, gender: form.gender || null, date_of_birth: form.date_of_birth || null },
        }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });
  return (
    <Modal title="Edit details" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <ErrorAlert error={save.error} />
        <div className="grid-2 tight">
          <Field label="Admission number" error={fieldError(save.error, 'admission_number')}>
            <input value={form.admission_number} onChange={set('admission_number')} required />
          </Field>
          <Field label="First name">
            <input value={form.first_name} onChange={set('first_name')} required />
          </Field>
          <Field label="Middle name">
            <input value={form.middle_name} onChange={set('middle_name')} />
          </Field>
          <Field label="Surname">
            <input value={form.last_name} onChange={set('last_name')} required />
          </Field>
          <Field label="Gender">
            <select value={form.gender} onChange={set('gender')}>
              <option value="">—</option>
              <option value="female">Female</option>
              <option value="male">Male</option>
            </select>
          </Field>
          <Field label="Date of birth">
            <input type="date" value={form.date_of_birth} onChange={set('date_of_birth')} />
          </Field>
        </div>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={save.isPending}>
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ChangeClass({ student, onClose, onDone }: DialogProps) {
  const levels = useClassLevels();
  const current = student.enrollments.find((e) => e.is_current);
  const [armId, setArmId] = useState(current?.placement.arm_id ?? '');
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PUT('/api/v1/students/{student_id}/class', { params: { path: { student_id: student.id } }, body: { arm_id: armId } }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Modal title="Change class for this term" onClose={onClose}>
      <ErrorAlert error={save.error} />
      <Field label="Class">
        <select value={armId} onChange={(e) => setArmId(e.target.value)}>
          <option value="">Choose…</option>
          {armOptions(levels.data).map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      <div className="row end">
        <button className="secondary" onClick={onClose}>
          Cancel
        </button>
        <button onClick={() => save.mutate()} disabled={!armId || save.isPending}>
          Save
        </button>
      </div>
    </Modal>
  );
}

function Withdraw({ student, onClose, onDone }: DialogProps) {
  const [leftOn, setLeftOn] = useState(lagosToday());
  const [reason, setReason] = useState('');
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/students/{student_id}/withdraw', {
          params: { path: { student_id: student.id } },
          body: { left_on: leftOn, reason },
        }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Modal title={`Withdraw ${student.first_name}`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p>Their records and any unpaid balance are kept. They won't be billed for new terms.</p>
        <ErrorAlert error={save.error} />
        <Field label="Date they left">
          <input type="date" value={leftOn} onChange={(e) => setLeftOn(e.target.value)} required />
        </Field>
        <Field label="Reason">
          <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={save.isPending}>
            Withdraw
          </button>
        </div>
      </form>
    </Modal>
  );
}

function AddGuardian({ student, onClose, onDone }: DialogProps) {
  const [form, setForm] = useState({ full_name: '', phone: '', email: '', relationship: 'guardian' as Schemas['Relationship'] });
  const [consent, setConsent] = useState(false);
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/students/{student_id}/guardians', {
          params: { path: { student_id: student.id } },
          body: { ...form, email: form.email || null, consent_confirmed: consent },
        }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Modal title="Add parent or guardian" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p className="muted">
          If this phone number already belongs to a parent at the school (e.g. a sibling's), they're linked instead of duplicated.
        </p>
        <ErrorAlert error={save.error} />
        <Field label="Name">
          <input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required minLength={2} />
        </Field>
        <Field label="Phone" error={fieldError(save.error, 'phone')}>
          <input value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} required inputMode="tel" />
        </Field>
        <Field label="Relationship">
          <select value={form.relationship} onChange={(e) => setForm({ ...form, relationship: e.target.value as Schemas['Relationship'] })}>
            <option value="mother">Mother</option>
            <option value="father">Father</option>
            <option value="guardian">Guardian</option>
            <option value="other">Other</option>
          </select>
        </Field>
        <Field label="Email (optional)">
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        <label className="checkbox">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} required />
          They agreed to the school storing these contact details for fees and school messages.
        </label>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={save.isPending}>
            Add
          </button>
        </div>
      </form>
    </Modal>
  );
}

function EditGuardian({ guardian, onClose, onDone }: { guardian: Guardian; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ full_name: guardian.full_name, phone: guardian.phone, email: guardian.email ?? '' });
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PATCH('/api/v1/guardians/{guardian_id}', {
          params: { path: { guardian_id: guardian.id } },
          body: { full_name: form.full_name, phone: form.phone, email: form.email || null, clear_email: !form.email },
        }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Modal title={`Edit ${guardian.full_name}`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p className="muted">
          Changes apply to all of their children at this school. Their phone number is also how they sign in to the parent portal.
        </p>
        <ErrorAlert error={save.error} />
        <Field label="Name">
          <input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required />
        </Field>
        <Field label="Phone" error={fieldError(save.error, 'phone')}>
          <input value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} required inputMode="tel" />
        </Field>
        <Field label="Email">
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={save.isPending}>
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

type EraseTarget = { kind: 'student' | 'guardian'; id: string; name: string };

/** Anonymise a former pupil or a parent (NDPA erasure). Bills and payments stay, under "Erased". */
function Erase({ target, onClose, onDone }: { target: EraseTarget; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState('');
  const erase = useMutation({
    mutationFn: async () =>
      unwrap(
        target.kind === 'student'
          ? await api.POST('/api/v1/students/{student_id}/erase', { params: { path: { student_id: target.id } }, body: { reason } })
          : await api.POST('/api/v1/guardians/{guardian_id}/erase', { params: { path: { guardian_id: target.id } }, body: { reason } }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Modal title={`Erase ${target.name}'s personal details`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          erase.mutate();
        }}
      >
        <p>
          {target.kind === 'student'
            ? 'Their name, date of birth and gender are removed, their account number is closed, and message texts about them are deleted.'
            : 'Their name, phone number and email are removed, they can no longer sign in to the parent portal, and message texts sent to them are deleted.'}{' '}
          Bills, payments and issued receipts are kept as financial records. This can't be undone.
        </p>
        <ErrorAlert error={erase.error} />
        <Field label="Reason (kept in the activity log)">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required
            minLength={3}
            placeholder="e.g. Parent's request of 12 Oct"
          />
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={erase.isPending}>
            Erase
          </button>
        </div>
      </form>
    </Modal>
  );
}
