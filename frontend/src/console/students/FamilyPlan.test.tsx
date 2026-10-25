import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { json, mockApi } from '../../test/fetchMock';
import { renderApp } from '../../test/render';
import { FamilyPlanDialog } from './FamilyPlan';

const offer = (studentInvoice: string, total: number) => ({
  invoice_id: studentInvoice,
  template_id: 'pt9',
  name: 'By arrangement',
  charge_kobo: 0,
  total_kobo: total,
  instalments: [
    { due_date: '2030-01-10', amount_kobo: total / 2 },
    { due_date: '2030-02-10', amount_kobo: total / 2 },
  ],
});

describe('putting a family on a plan', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('puts every child on the plan and tells the parent', async () => {
    const calls = mockApi({
      'GET /api/v1/guardians/g1/plan-offers': () =>
        json([
          { student_id: 's1', first_name: 'Ada', offers: [offer('i1', 15_500_000)] },
          { student_id: 's2', first_name: 'Bayo', offers: [offer('i2', 18_500_000)] },
          { student_id: 's3', first_name: 'Chidi', offers: [] },
        ]),
      'POST /api/v1/guardians/g1/school-plan': () => json({ applied: ['s1', 's2'], messages: 2, told: true }, 201),
    });
    renderApp(<FamilyPlanDialog guardianId="g1" name="Mrs Adeyemi" remindersOn onClose={() => undefined} />);
    expect(await screen.findByText(/₦77,500.00 by 10 Jan 2030/)).toBeInTheDocument(); // Ada
    expect(screen.getByText(/₦92,500.00 by 10 Jan 2030/)).toBeInTheDocument(); // Bayo
    expect(screen.getByText(/not on this plan/)).toBeInTheDocument(); // Chidi owes nothing
    expect(screen.getByLabelText('Tell Mrs Adeyemi now by WhatsApp or text')).toBeChecked();
    await userEvent.click(screen.getByRole('button', { name: 'Put on the plan' }));
    expect(await screen.findByText(/2 children are now on the plan/)).toBeInTheDocument();
    expect(screen.getByText(/has been sent the plan by WhatsApp or text/)).toBeInTheDocument();
    const post = calls.find((c) => c.method === 'POST')!;
    expect(await post.json()).toEqual({ template_id: 'pt9', tell_parent: true });
  });

  it('says plainly when a parent who turned reminders off won’t be messaged', async () => {
    mockApi({
      'GET /api/v1/guardians/g1/plan-offers': () => json([{ student_id: 's1', first_name: 'Ada', offers: [offer('i1', 15_500_000)] }]),
    });
    renderApp(<FamilyPlanDialog guardianId="g1" name="Mr Okafor" remindersOn={false} onClose={() => undefined} />);
    expect(await screen.findByLabelText('Mr Okafor has turned off reminders, so no message will be sent')).toBeDisabled();
  });
});
