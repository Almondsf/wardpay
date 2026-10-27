import { useMutation } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { ErrorAlert, PageHeader } from '../lib/ui';

function useSwitch() {
  const { switchBranch } = useAuth();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: switchBranch,
    onSuccess: () => navigate('/console'),
  });
}

/** Head-office staff of a school group: which branch they're working in. Nothing shows for anyone else. */
export function BranchSwitcher() {
  const { user } = useAuth();
  const change = useSwitch();
  const branches = user?.branches ?? [];
  if (!user?.group || branches.length === 0) return null;
  return (
    <label className="branch-switcher">
      <span className="field-label">Branch</span>
      <select
        aria-label="Branch"
        value={user.school?.id ?? ''}
        onChange={(e) => e.target.value && change.mutate(e.target.value)}
        disabled={change.isPending}
      >
        {!user.school && <option value="">Choose…</option>}
        {branches.map((b) => (
          <option key={b.id} value={b.id}>
            {b.name}
          </option>
        ))}
      </select>
      <ErrorAlert error={change.error} />
    </label>
  );
}

/** Shown to head-office staff until they're working in a branch (none chosen, or theirs left the group). */
export function ChooseBranch() {
  const { user } = useAuth();
  const change = useSwitch();
  const branches = user?.branches ?? [];
  if (!user?.group) return null;
  return (
    <>
      <PageHeader title={user.group.name} subtitle="Choose the branch to work in. You can switch any time from the menu." />
      {branches.length === 0 ? (
        <p className="muted">There are no branches you can work in right now. Please contact WardPay support.</p>
      ) : (
        <ul className="plain">
          {branches.map((b) => (
            <li key={b.id} className="list-item">
              <button onClick={() => change.mutate(b.id)} disabled={change.isPending}>
                {b.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      <ErrorAlert error={change.error} />
    </>
  );
}
