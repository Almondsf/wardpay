import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../../api/client';
import { AuthProvider } from '../../auth/AuthContext';
import { json, me, mockApi, signedIn } from '../../test/fetchMock';
import { renderApp } from '../../test/render';
import { ConsoleApp } from '../ConsoleApp';

const platformAdmin = me({ role: 'platform_admin', school: null, capabilities: ['platform.manage'], mfa_enabled: true });
const empty = { id: 'g1', name: 'Bright Stars Schools', branches: [], staff: [] };
const ikeja = { id: 's1', name: 'Ikeja Campus', slug: 'ikeja', status: 'active' };
const school = {
  ...ikeja,
  address: '',
  phone: '',
  email: '',
  created_at: '2026-10-01T09:00:00Z',
  origin: 'platform',
  plan_name: 'Standard',
  pupils: 3,
  proprietor_email: 'p@ikeja.ng',
  payments_connected: true,
  subscription_owed_kobo: 0,
  last_sign_in_at: null,
};

function renderAt(path: string) {
  return renderApp(
    <Routes>
      <Route
        path="/console/*"
        element={
          <AuthProvider>
            <ConsoleApp />
          </AuthProvider>
        }
      />
    </Routes>,
    path,
  );
}

describe('platform: school groups', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('adds a group, a branch and the owner', async () => {
    let group: Record<string, unknown> = empty;
    const calls = mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn(platformAdmin)),
      'GET /api/v1/platform/groups': () => json([]),
      'POST /api/v1/platform/groups': () => json(group, 201),
      'GET /api/v1/platform/groups/g1': () => json(group),
      'GET /api/v1/platform/schools': () => json({ items: [school], total: 1, limit: 10, offset: 0 }),
      'POST /api/v1/platform/groups/g1/branches': () => json((group = { ...group, branches: [ikeja] })),
      'POST /api/v1/platform/groups/g1/staff': () =>
        json(
          (group = {
            ...group,
            staff: [
              {
                id: 'u1',
                full_name: 'Chief Obi',
                email: 'chief@stars.ng',
                role: 'proprietor',
                is_active: true,
                mfa_enabled: false,
                invited: true,
                last_login_at: null,
              },
            ],
          }),
          201,
        ),
    });
    renderAt('/console/platform/groups');
    await userEvent.type(await screen.findByLabelText('New group'), 'Bright Stars Schools');
    await userEvent.click(screen.getByRole('button', { name: 'Add group' }));
    expect(await screen.findByRole('heading', { name: 'Bright Stars Schools' })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Add a school'), 'Ikeja');
    await userEvent.click(await screen.findByRole('button', { name: 'Add as a branch' }));
    const branches = screen.getByRole('region', { name: 'Branches' });
    expect(await within(branches).findByRole('link', { name: 'Ikeja Campus' })).toBeInTheDocument();

    const office = screen.getByRole('region', { name: 'Head office' });
    await userEvent.type(within(office).getByLabelText('Full name'), 'Chief Obi');
    await userEvent.type(within(office).getByLabelText('Email'), 'chief@stars.ng');
    await userEvent.click(within(office).getByRole('button', { name: 'Send invite' }));
    expect(await within(office).findByText(/invited, no password yet/)).toBeInTheDocument();
    const invite = calls.find((c) => c.method === 'POST' && c.url.endsWith('/staff'))!;
    expect(await invite.json()).toEqual({ full_name: 'Chief Obi', email: 'chief@stars.ng', role: 'proprietor' });
  });
});
