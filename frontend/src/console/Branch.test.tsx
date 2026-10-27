import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../api/client';
import { AuthProvider } from '../auth/AuthContext';
import { error, json, me, mockApi, signedIn } from '../test/fetchMock';
import { renderApp } from '../test/render';
import { ConsoleApp } from './ConsoleApp';

const ikeja = { id: 's1', name: 'Ikeja Campus', slug: 'ikeja' };
const lekki = { id: 's2', name: 'Lekki Campus', slug: 'lekki' };
const group = { id: 'g1', name: 'Bright Stars Schools' };
const owner = (school: typeof ikeja | null) =>
  me({ role: 'proprietor', school, group, branches: [ikeja, lekki], capabilities: ['school.view', 'staff.view'] });
const dashboard = {
  'GET /api/v1/school/roll': () => error(403, 'permission_denied'),
  'GET /api/v1/reports/overview': () => error(403, 'permission_denied'),
  'GET /api/v1/reports/insights': () => error(403, 'permission_denied'),
  'GET /api/v1/school/onboarding': () => error(403, 'permission_denied'),
};

function renderConsole(path: string) {
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

describe('school groups: head-office staff', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('switches branch from the menu', async () => {
    const calls = mockApi({
      ...dashboard,
      'POST /api/v1/auth/refresh': () => json(signedIn(owner(ikeja))),
      'GET /api/v1/staff': () => json({ items: [], total: 0, limit: 50, offset: 0 }),
      'POST /api/v1/auth/branch': () => json(owner(lekki)),
    });
    renderConsole('/console/staff');
    const branch = await screen.findByRole('combobox', { name: 'Branch' });
    expect(branch).toHaveValue('s1');
    expect(
      within(branch)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Ikeja Campus', 'Lekki Campus']);
    await userEvent.selectOptions(branch, 's2');
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Branch' })).toHaveValue('s2'));
    const post = calls.find((c) => c.method === 'POST' && c.url.endsWith('/auth/branch'))!;
    expect(await post.json()).toEqual({ school_id: 's2' });
  });

  it('asks head office to choose a branch when they have none', async () => {
    mockApi({
      ...dashboard,
      'POST /api/v1/auth/refresh': () => json(signedIn(owner(null))),
      'POST /api/v1/auth/branch': () => json(owner(ikeja)),
    });
    renderConsole('/console/staff');
    expect(await screen.findByRole('heading', { name: 'Bright Stars Schools' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Ikeja Campus' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Branch' })).toHaveValue('s1'));
  });

  it('shows no branch menu to a single school’s staff', async () => {
    mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn()),
      'GET /api/v1/staff': () => json({ items: [], total: 0, limit: 50, offset: 0 }),
    });
    renderConsole('/console/staff');
    expect(await screen.findByRole('heading', { name: 'Staff' })).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Branch' })).not.toBeInTheDocument();
  });
});
