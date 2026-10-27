import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../api/client';
import { AuthProvider } from '../auth/AuthContext';
import { json, me, mockApi, signedIn } from '../test/fetchMock';
import { renderApp } from '../test/render';
import { ConsoleApp } from './ConsoleApp';

const ikeja = { id: 's1', name: 'Ikeja Campus', slug: 'ikeja' };
const lekki = { id: 's2', name: 'Lekki Campus', slug: 'lekki' };
const owner = me({
  role: 'proprietor',
  school: ikeja,
  group: { id: 'g1', name: 'Bright Stars Schools' },
  branches: [ikeja, lekki],
  capabilities: ['school.view', 'reports.view'],
});
const figures = (school_id: string | null, name: string, kobo: number) => ({
  school_id,
  name,
  term_label: school_id ? '2026/2027 First term' : '',
  pupils: 10,
  billed_kobo: kobo,
  collected_kobo: kobo / 2,
  outstanding_kobo: kobo / 2,
  older_outstanding_kobo: 0,
  total_outstanding_kobo: kobo / 2,
  students_owing: 3,
  credit_kobo: 0,
  received_today_kobo: 0,
  received_7_days_kobo: 0,
  received_30_days_kobo: kobo / 2,
  pending_approval: 0,
  pending_approval_kobo: 0,
});
const report = {
  group_name: 'Bright Stars Schools',
  today: '2026-10-27',
  branches: [figures('s1', 'Ikeja Campus', 100_000_00), figures('s2', 'Lekki Campus', 300_000_00)],
  totals: { ...figures(null, 'All branches', 400_000_00), pupils: 20 },
  by_day: [{ day: '2026-10-27', amount_kobo: 0 }],
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

describe('head office: all branches', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('shows each branch and the totals, and opens another branch', async () => {
    const calls = mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn(owner)),
      'GET /api/v1/reports/group': () => json(report),
      'POST /api/v1/auth/branch': () => json({ ...owner, school: lekki }),
      'GET /api/v1/school/roll': () => json({}, 403),
      'GET /api/v1/reports/overview': () => json({}, 403),
      'GET /api/v1/reports/insights': () => json({}, 403),
      'GET /api/v1/school/onboarding': () => json({}, 403),
    });
    renderConsole('/console/group');
    const table = await screen.findByRole('table', { name: 'Branches' });
    const rows = within(table).getAllByRole('row');
    expect(rows[1]).toHaveTextContent('Ikeja Campus');
    expect(rows[3]).toHaveTextContent('All branches');
    expect(rows[3]).toHaveTextContent('₦400,000');
    expect(screen.getByRole('link', { name: 'All branches' })).toBeInTheDocument();
    // The branch they're in has no button; the other one opens
    expect(within(rows[1]!).queryByRole('button')).not.toBeInTheDocument();
    await userEvent.click(within(rows[2]!).getByRole('button', { name: 'Work in this branch' }));
    const post = calls.find((c) => c.method === 'POST' && c.url.endsWith('/auth/branch'))!;
    expect(await post.json()).toEqual({ school_id: 's2' });
  });

  it('is not in the menu for a single school', async () => {
    mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn(me({ capabilities: ['reports.view', 'staff.view'] }))),
      'GET /api/v1/staff': () => json({ items: [], total: 0, limit: 50, offset: 0 }),
    });
    renderConsole('/console/staff');
    expect(await screen.findByRole('heading', { name: 'Staff' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'All branches' })).not.toBeInTheDocument();
  });
});
