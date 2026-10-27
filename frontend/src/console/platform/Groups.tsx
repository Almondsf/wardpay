import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api, unwrap } from '../../api/client';
import { ROLE_LABELS, formatDateTime } from '../../lib/format';
import { BackLink, ErrorAlert, Loading, Notice, PageHeader, StatusBadge } from '../../lib/ui';

/** School groups: one owner's branches, and the head-office staff who work in all of them. */
export function PlatformGroups() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const groups = useQuery({ queryKey: ['platform-groups'], queryFn: async () => unwrap(await api.GET('/api/v1/platform/groups')) });
  const create = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/platform/groups', { body: { name } })),
    onSuccess: (group) => navigate(`/console/platform/groups/${group.id}`),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <>
      <PageHeader
        title="School groups"
        subtitle="One owner with several branches or campuses. Each branch stays its own school; head-office staff sign in once and switch between them."
      />
      <form className="card row wrap" onSubmit={submit} aria-label="Add a group">
        <label className="field">
          <span className="field-label">New group</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Bright Stars Schools" minLength={2} required />
        </label>
        <button type="submit" disabled={create.isPending}>
          Add group
        </button>
        <ErrorAlert error={create.error} />
      </form>
      {groups.isPending ? (
        <Loading />
      ) : groups.isError ? (
        <ErrorAlert error={groups.error} />
      ) : groups.data.length === 0 ? (
        <p className="muted">No groups yet. Most schools don't need one: only owners with more than one branch.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Group</th>
              <th className="num">Branches</th>
              <th className="num">Head-office staff</th>
            </tr>
          </thead>
          <tbody>
            {groups.data.map((g) => (
              <tr key={g.id}>
                <td>
                  <Link to={`/console/platform/groups/${g.id}`}>{g.name}</Link>
                </td>
                <td className="num">{g.branches}</td>
                <td className="num">{g.staff}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

type GroupDetail = Awaited<ReturnType<typeof fetchGroup>>;

async function fetchGroup(groupId: string) {
  return unwrap(await api.GET('/api/v1/platform/groups/{group_id}', { params: { path: { group_id: groupId } } }));
}

export function PlatformGroupDetail() {
  const { groupId = '' } = useParams();
  const group = useQuery({ queryKey: ['platform-group', groupId], queryFn: () => fetchGroup(groupId) });
  if (group.isPending) return <Loading />;
  if (group.isError) return <ErrorAlert error={group.error} />;
  const g = group.data;
  return (
    <>
      <BackLink to="/console/platform/groups">School groups</BackLink>
      <PageHeader title={g.name} subtitle="Each branch keeps its own pupils, bills, bank account and WardPay bill." />
      <div className="grid-2">
        <Branches group={g} />
        <HeadOffice group={g} />
      </div>
    </>
  );
}

function useGroupUpdate(groupId: string) {
  const queryClient = useQueryClient();
  return (data: GroupDetail) => {
    queryClient.setQueryData(['platform-group', groupId], data);
    void queryClient.invalidateQueries({ queryKey: ['platform-groups'] });
  };
}

function Branches({ group }: { group: GroupDetail }) {
  const [search, setSearch] = useState('');
  const [removing, setRemoving] = useState<string | null>(null);
  const updated = useGroupUpdate(group.id);
  const found = useQuery({
    queryKey: ['platform-schools', 'group-search', search],
    queryFn: async () => unwrap(await api.GET('/api/v1/platform/schools', { params: { query: { search, limit: 10 } } })),
    enabled: search.trim().length >= 2,
  });
  const add = useMutation({
    mutationFn: async (schoolId: string) =>
      unwrap(
        await api.POST('/api/v1/platform/groups/{group_id}/branches', {
          params: { path: { group_id: group.id } },
          body: { school_id: schoolId },
        }),
      ),
    onSuccess: (data) => {
      updated(data);
      setSearch('');
    },
  });
  const remove = useMutation({
    mutationFn: async (schoolId: string) =>
      unwrap(
        await api.DELETE('/api/v1/platform/groups/{group_id}/branches/{school_id}', {
          params: { path: { group_id: group.id, school_id: schoolId } },
        }),
      ),
    onSuccess: (data) => {
      updated(data);
      setRemoving(null);
    },
  });
  const inGroup = new Set(group.branches.map((b) => b.id));
  return (
    <section className="card" aria-label="Branches">
      <h2>Branches</h2>
      {group.branches.length === 0 && <p className="muted">No branches yet. Find each school below and add it.</p>}
      <ul className="plain">
        {group.branches.map((b) => (
          <li key={b.id} className="list-item row wrap">
            <Link to={`/console/platform/schools/${b.id}`}>{b.name}</Link>
            <StatusBadge status={b.status} />
            {removing === b.id ? (
              <>
                <span className="small">Take {b.name} out? Head office loses access to it at once.</span>
                <button className="small danger" onClick={() => remove.mutate(b.id)} disabled={remove.isPending}>
                  Yes, take it out
                </button>
                <button className="small secondary" onClick={() => setRemoving(null)}>
                  No
                </button>
              </>
            ) : (
              <button className="small secondary" onClick={() => setRemoving(b.id)}>
                Take out
              </button>
            )}
          </li>
        ))}
      </ul>
      <label className="field">
        <span className="field-label">Add a school</span>
        <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Name or short name" />
      </label>
      {found.data && (
        <ul className="plain">
          {found.data.items
            .filter((s) => !inGroup.has(s.id))
            .map((s) => (
              <li key={s.id} className="list-item row wrap">
                <span>
                  {s.name} <span className="muted small">{s.slug}</span>
                </span>
                <button className="small" onClick={() => add.mutate(s.id)} disabled={add.isPending}>
                  Add as a branch
                </button>
              </li>
            ))}
        </ul>
      )}
      <ErrorAlert error={add.error ?? remove.error} />
    </section>
  );
}

function HeadOffice({ group }: { group: GroupDetail }) {
  const [form, setForm] = useState({ full_name: '', email: '', role: 'proprietor' as 'proprietor' | 'bursar' });
  const updated = useGroupUpdate(group.id);
  const invite = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/platform/groups/{group_id}/staff', { params: { path: { group_id: group.id } }, body: form })),
    onSuccess: (data) => {
      updated(data);
      setForm({ full_name: '', email: '', role: 'proprietor' });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    invite.mutate();
  };
  return (
    <section className="card" aria-label="Head office">
      <h2>Head office</h2>
      <p className="muted small">
        The owner works in every branch as its proprietor; a central bursar as its bursar. They choose the branch from a menu after signing
        in.
      </p>
      <ul className="plain">
        {group.staff.map((u) => (
          <li key={u.id} className="list-item">
            <strong>{u.full_name}</strong> <span className="muted">({u.role === 'proprietor' ? 'Owner' : 'Central bursar'})</span>
            <div className="small">
              {u.email} · {u.invited ? 'invited, no password yet' : u.is_active ? 'active' : 'deactivated'} · two-factor{' '}
              {u.mfa_enabled ? 'on' : 'off'}
            </div>
            <div className="muted small">Last sign-in: {u.last_login_at ? formatDateTime(u.last_login_at) : 'never'}</div>
            <HeadOfficeHelp groupId={group.id} staff={u} />
          </li>
        ))}
      </ul>
      <form onSubmit={submit} aria-label="Invite head-office staff">
        <label className="field">
          <span className="field-label">Full name</span>
          <input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required minLength={2} />
        </label>
        <label className="field">
          <span className="field-label">Email</span>
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        </label>
        <label className="field">
          <span className="field-label">Role</span>
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as 'proprietor' | 'bursar' })}>
            <option value="proprietor">Owner ({ROLE_LABELS.proprietor} in every branch)</option>
            <option value="bursar">Central bursar ({ROLE_LABELS.bursar} in every branch)</option>
          </select>
        </label>
        <button type="submit" disabled={invite.isPending}>
          Send invite
        </button>
        <ErrorAlert error={invite.error} />
      </form>
    </section>
  );
}

type Staff = GroupDetail['staff'][number];

function HeadOfficeHelp({ groupId, staff }: { groupId: string; staff: Staff }) {
  const updated = useGroupUpdate(groupId);
  const queryClient = useQueryClient();
  const path = { params: { path: { group_id: groupId, user_id: staff.id } } };
  const setActive = useMutation({
    mutationFn: async (active: boolean) =>
      unwrap(
        active
          ? await api.POST('/api/v1/platform/groups/{group_id}/staff/{user_id}/reactivate', path)
          : await api.POST('/api/v1/platform/groups/{group_id}/staff/{user_id}/deactivate', path),
      ),
    onSuccess: updated,
  });
  const link = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/platform/groups/{group_id}/staff/{user_id}/password-link', path)),
  });
  const reset = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/platform/groups/{group_id}/staff/{user_id}/reset-mfa', path)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['platform-group', groupId] }),
  });
  return (
    <div className="row wrap small">
      {staff.is_active && (
        <button className="small secondary" onClick={() => link.mutate()} disabled={link.isPending}>
          Send password link
        </button>
      )}
      {staff.is_active && staff.mfa_enabled && (
        <button className="small secondary" onClick={() => reset.mutate()} disabled={reset.isPending}>
          Reset two-factor
        </button>
      )}
      <button className="small secondary" onClick={() => setActive.mutate(!staff.is_active)} disabled={setActive.isPending}>
        {staff.is_active ? 'Deactivate' : 'Reactivate'}
      </button>
      {link.isSuccess && <Notice>{link.data.sent === 'invite' ? 'Invite sent again.' : 'Password-reset link sent.'}</Notice>}
      {reset.isSuccess && <Notice>Two-factor sign-in turned off.</Notice>}
      <ErrorAlert error={link.error ?? reset.error ?? setActive.error} />
    </div>
  );
}
