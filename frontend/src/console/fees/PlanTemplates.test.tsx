import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../../api/client';
import { AuthProvider } from '../../auth/AuthContext';
import { json, me, mockApi, signedIn } from '../../test/fetchMock';
import { renderApp } from '../../test/render';
import { ConsoleApp } from '../ConsoleApp';
import { PlanTemplates } from './PlanTemplates';

const bursar = me({ role: 'bursar', capabilities: ['fees.view', 'fees.manage', 'invoices.issue', 'students.view', 'payments.approve'] });
const payIn3 = {
  id: 'pt1',
  name: 'Pay in 3',
  is_active: true,
  eligibility: 'anyone',
  charge_kind: 'fixed',
  charge_value: 150_000,
  parts: [
    { percent_bp: 5000, day: 0 },
    { percent_bp: 2500, day: 42 },
    { percent_bp: 2500, day: 70 },
  ],
};

describe('school instalment plans', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('sets up a plan, only once the shares add up to 100%', async () => {
    const calls = mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn(bursar)),
      'GET /api/v1/plan-templates': () => json([]),
      'POST /api/v1/plan-templates': async (request) => json({ ...payIn3, ...(await request.json()), id: 'pt2' }, 201),
    });
    renderApp(
      <AuthProvider>
        <PlanTemplates />
      </AuthProvider>,
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Add plan' }));
    const dialog = within(screen.getByRole('dialog'));
    await userEvent.type(dialog.getByLabelText(/^Name/), 'Pay in 3');
    await userEvent.selectOptions(dialog.getByLabelText('Who can choose it'), 'anyone');
    await userEvent.clear(dialog.getByLabelText('Payment 3 share'));
    await userEvent.type(dialog.getByLabelText('Payment 3 share'), '20');
    expect(dialog.getByText('Shares add up to 95% (must be 100%)')).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: 'Save plan' })).toBeDisabled();
    await userEvent.clear(dialog.getByLabelText('Payment 3 share'));
    await userEvent.type(dialog.getByLabelText('Payment 3 share'), '25');
    await userEvent.selectOptions(dialog.getByLabelText(/^Plan charge/), 'fixed');
    await userEvent.type(dialog.getByLabelText('Amount (₦)'), '1500');
    await userEvent.click(dialog.getByRole('button', { name: 'Save plan' }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/plan-templates'))).toBe(true));
    expect(await calls.find((c) => c.method === 'POST' && c.url.endsWith('/plan-templates'))!.json()).toEqual({
      name: 'Pay in 3',
      is_active: true,
      eligibility: 'anyone',
      charge_kind: 'fixed',
      charge_value: 150_000,
      parts: payIn3.parts,
    });
  });

  it('puts a bill on a school plan from the bill page', async () => {
    const invoice = {
      id: 'inv1',
      number: 12,
      kind: 'term',
      term_id: 't1',
      student: { id: 's1', admission_number: 'DMC/1', full_name: 'Tolu Adeyemi' },
      issued_at: '2026-09-07T08:00:00Z',
      due_date: '2026-09-30',
      total_kobo: 15_500_000,
      paid_kobo: 0,
      balance_kobo: 15_500_000,
      status: 'unpaid',
      void_reason: null,
      lines: [],
      plan: null,
    };
    const offer = {
      invoice_id: 'inv1',
      template_id: 'pt9',
      name: 'By arrangement',
      charge_kobo: 0,
      total_kobo: 15_500_000,
      instalments: [
        { due_date: '2030-01-10', amount_kobo: 7_750_000 },
        { due_date: '2030-02-10', amount_kobo: 7_750_000 },
      ],
    };
    const calls = mockApi({
      'POST /api/v1/auth/refresh': () => json(signedIn(bursar)),
      'GET /api/v1/invoices/inv1': () => json(invoice),
      'GET /api/v1/invoices/inv1/plan-offers': () => json([offer]),
      'POST /api/v1/invoices/inv1/plan/school-plan': () => json(invoice, 201),
    });
    renderApp(
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
      '/console/invoices/inv1',
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Use a school plan' }, { timeout: 5000 }));
    const dialog = within(screen.getByRole('dialog'));
    expect(dialog.getByText(/₦77,500.00 by 10 Jan 2030/)).toBeInTheDocument();
    await userEvent.click(dialog.getByRole('button', { name: 'Use this plan' }));
    await vi.waitFor(() => expect(calls.some((c) => c.url.endsWith('/school-plan'))).toBe(true));
    expect(await calls.find((c) => c.url.endsWith('/school-plan'))!.json()).toEqual({ template_id: 'pt9' });
  });
});
