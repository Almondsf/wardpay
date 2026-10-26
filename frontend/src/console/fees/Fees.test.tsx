import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../../api/client';
import { AuthProvider } from '../../auth/AuthContext';
import { json, me, mockApi, signedIn } from '../../test/fetchMock';
import { renderApp } from '../../test/render';
import { ConsoleApp } from '../ConsoleApp';

const bursar = me({
  role: 'bursar',
  capabilities: ['school.view', 'students.view', 'fees.view', 'fees.manage', 'invoices.issue', 'payments.view'],
});
const term = { id: 't1', session_id: 's1', number: 1, label: 'First term', start_date: '2026-09-07', end_date: '2026-12-11', is_current: true };
const noBills = { dry_run: true, fees: [], bills_changed: 0, total_kobo: 0 };
const base = {
  'POST /api/v1/auth/refresh': () => json(signedIn(bursar)),
  'GET /api/v1/terms/current': () => json({ ...term, session_name: '2026/2027' }),
  'GET /api/v1/academic-sessions': () => json([{ id: 's1', name: '2026/2027', start_date: '2026-09-01', end_date: '2027-07-31', terms: [term] }]),
  'GET /api/v1/class-levels': () =>
    json([
      { id: 'l1', name: 'JSS 1', rank: 1, is_active: true, arms: [] },
      { id: 'l2', name: 'JSS 2', rank: 2, is_active: true, arms: [] },
    ]),
  'GET /api/v1/fee-items': () => json([{ id: 'f1', name: 'Tuition', category: 'tuition', is_active: true }]),
};

function renderAt(path: string) {
  return renderApp(
    <Routes>
      <Route path="/console/*" element={<AuthProvider><ConsoleApp /></AuthProvider>} />
    </Routes>,
    path,
  );
}

describe('fees and billing', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('saves only the changed amounts, in kobo', async () => {
    const calls = mockApi({
      ...base,
      'GET /api/v1/terms/t1/fee-schedule': () => json([{ fee_item_id: 'f1', class_level_id: 'l1', amount_kobo: 15_000_000, is_optional: false }]),
      'PUT /api/v1/terms/t1/fee-schedule': async (request) => json({ entries: (await request.json()).entries, bills: noBills }),
    });
    renderAt('/console/fees');
    const jss1 = await screen.findByLabelText('Tuition for JSS 1');
    await waitFor(() => expect(jss1).toHaveValue('150000'));
    await userEvent.type(screen.getByLabelText('Tuition for JSS 2'), '175,000.50');
    await userEvent.click(screen.getByRole('button', { name: 'Save 1 change' }));
    const put = calls.find((c) => c.method === 'PUT')!;
    expect((await put.json()).entries).toEqual([{ fee_item_id: 'f1', class_level_id: 'l2', amount_kobo: 17_500_050, is_optional: false }]);
  });

  it('points to where extra fees for classes and pupils are added', async () => {
    mockApi({ ...base, 'GET /api/v1/terms/t1/fee-schedule': () => json([]) });
    renderAt('/console/fees');
    const card = await screen.findByRole('region', { name: 'Extra fees after billing' });
    expect(within(card).getByRole('link', { name: 'Billing → Add a fee to classes' })).toHaveAttribute('href', '/console/billing');
    expect(within(card).getByText(/Add a charge/)).toBeInTheDocument();
    expect(within(card).queryByText(/Only the bursar or the proprietor/)).not.toBeInTheDocument();
  });

  it('offers to add fees set after billing to the bills already sent', async () => {
    let missing = { dry_run: true, fees: [{ item_name: 'WAEC', class_names: ['SSS 3'], bills: 30, total_kobo: 300_000_000 }], bills_changed: 30, total_kobo: 300_000_000 };
    const calls = mockApi({
      ...base,
      'GET /api/v1/terms/t1/fee-schedule': () => json([]),
      'POST /api/v1/terms/t1/bills/sync': async (request) => {
        const { dry_run } = await request.json();
        if (dry_run) return json(missing);
        const done = { ...missing, dry_run: false };
        missing = { dry_run: true, fees: [], bills_changed: 0, total_kobo: 0 };
        return json(done);
      },
    });
    renderAt('/console/fees');
    const notice = await screen.findByRole('region', { name: 'Fees not on the bills yet' });
    expect(within(notice).getByText(/WAEC for SSS 3: 30 pupils/)).toBeInTheDocument();
    await userEvent.click(within(notice).getByRole('button', { name: 'Add them to 30 bills' }));
    expect(await screen.findByText(/Added to 30 bills/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Fees not on the bills yet' })).not.toBeInTheDocument());
    const applied = calls.filter((c) => c.method === 'POST');
    expect(await applied[applied.length - 2]!.json()).toEqual({ dry_run: false });
  });

  it('a fee saved for a class goes onto the bills already sent, and says so', async () => {
    mockApi({
      ...base,
      'GET /api/v1/terms/t1/fee-schedule': () => json([]),
      'PUT /api/v1/terms/t1/fee-schedule': async (request) =>
        json({
          entries: (await request.json()).entries,
          bills: { dry_run: false, fees: [{ item_name: 'Tuition', class_names: ['JSS 2'], bills: 20, total_kobo: 200_000_000 }], bills_changed: 20, total_kobo: 200_000_000 },
        }),
      'POST /api/v1/terms/t1/bills/sync': () => json(noBills),
    });
    renderAt('/console/fees');
    await userEvent.type(await screen.findByLabelText('Tuition for JSS 2'), '100000');
    await userEvent.click(screen.getByRole('button', { name: 'Save 1 change' }));
    expect(await screen.findByText(/added to the bills already sent: Tuition for JSS 2 \(20 pupils\)/)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); // nothing to confirm
  });

  it('blocks saving an invalid amount', async () => {
    mockApi({ ...base, 'GET /api/v1/terms/t1/fee-schedule': () => json([]) });
    renderAt('/console/fees');
    await userEvent.type(await screen.findByLabelText('Tuition for JSS 1'), '12.345');
    expect(screen.getByText('Fix the highlighted amounts.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save/ })).toBeDisabled();
  });

  it('previews then generates the term invoices', async () => {
    const report = (dry: boolean) => ({
      dry_run: dry, invoices: 2, total_kobo: 30_000_000, discounts_kobo: 1_500_000, already_invoiced: 0, notices_queued: dry ? 0 : 2, no_fees_set: ['Nursery 1'],
      by_class: [{ class_level_id: 'l1', class_name: 'JSS 1', invoices: 2, total_kobo: 30_000_000 }], sample: [],
    });
    const bodies: unknown[] = [];
    mockApi({
      ...base,
      'GET /api/v1/terms/t1/billing-summary': () =>
        json({ invoices: 0, billed_kobo: 0, paid_kobo: 0, outstanding_kobo: 0, students_enrolled: 2, students_invoiced: 0 }),
      'GET /api/v1/invoices': () => json({ items: [], total: 0, limit: 50, offset: 0 }),
      'POST /api/v1/terms/t1/invoices/generate': async (request) => {
        const body = (await request.json()) as { dry_run: boolean };
        bodies.push(body);
        return json(report(body.dry_run));
      },
    });
    renderAt('/console/billing');
    await userEvent.click(await screen.findByRole('button', { name: 'Generate invoices' }));
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/Preview: 2 invoices totalling/)).toBeInTheDocument();
    expect(screen.getByText(/No fees set for: Nursery 1/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Generate 2 invoices' }));
    expect(await screen.findByText(/Done: 2 invoices/)).toBeInTheDocument();
    expect(screen.getByText('2 parents will get a message about their bill.')).toBeInTheDocument();
    expect(bodies).toEqual([
      { dry_run: true, due_date: null, notify_parents: true },
      { dry_run: false, due_date: null, notify_parents: true },
    ]);
  });

  it('waits while a big school’s bills are made in the background', async () => {
    const report = { dry_run: false, invoices: 2, total_kobo: 30_000_000, discounts_kobo: 0, already_invoiced: 0, notices_queued: 0, no_fees_set: [], by_class: [], sample: [] };
    let checks = 0;
    mockApi({
      ...base,
      'GET /api/v1/terms/t1/billing-summary': () =>
        json({ invoices: 0, billed_kobo: 0, paid_kobo: 0, outstanding_kobo: 0, students_enrolled: 2, students_invoiced: 0 }),
      'GET /api/v1/invoices': () => json({ items: [], total: 0, limit: 50, offset: 0 }),
      'POST /api/v1/terms/t1/invoices/generate': async (request) => {
        const body = (await request.json()) as { dry_run: boolean };
        return body.dry_run
          ? json({ ...report, dry_run: true, run_id: null, pending: false })
          : json({ ...report, invoices: 0, total_kobo: 0, run_id: 'r1', pending: true }, 202);
      },
      'GET /api/v1/billing-runs/r1': () => {
        checks += 1;
        return checks < 2 ? json({ ...report, invoices: 0, run_id: 'r1', pending: true }, 202) : json({ ...report, run_id: 'r1', pending: false });
      },
    });
    renderAt('/console/billing');
    await userEvent.click(await screen.findByRole('button', { name: 'Generate invoices' }));
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Generate 2 invoices' }));
    expect(await screen.findByRole('button', { name: 'Making the bills…' })).toBeDisabled();
    expect(await screen.findByText(/Done: 2 invoices/, {}, { timeout: 6000 })).toBeInTheDocument();
    expect(checks).toBe(2);
  });
});
