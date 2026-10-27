import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FamilyLinkDialog } from '../console/students/FamilyLink';
import { error, json, mockApi } from '../test/fetchMock';
import { renderApp } from '../test/render';
import { FamilyPay } from './FamilyPay';

const TOKEN = 'abcdefghijklmnopqrstuvwxyz012345';

const page = {
  school_name: 'Alpha College',
  card_payments: true,
  min_part_kobo: 100_000,
  split_rule: 'clear_most',
  children: [
    {
      student_id: 's1',
      first_name: 'Ada',
      class_name: 'JSS 1 A',
      owed_kobo: 15_500_000,
      suggested_kobo: 15_500_000,
      on_plan: false,
      account_number: '9900000001',
      bank_name: 'Wema Bank',
      fees: [
        { name: 'Tuition', billed_kobo: 15_000_000, paid_kobo: 0, left_kobo: 15_000_000, status: 'Unpaid' },
        { name: 'WAEC', billed_kobo: 400_000, paid_kobo: 0, left_kobo: 400_000, status: 'Unpaid' },
        { name: 'PTA', billed_kobo: 300_000, paid_kobo: 200_000, left_kobo: 100_000, status: 'Part paid' },
      ],
      earlier_left_kobo: 0,
      plan_offers: [],
    },
    {
      student_id: 's2',
      first_name: 'Bayo',
      class_name: 'JSS 2 A',
      owed_kobo: 18_500_000,
      suggested_kobo: 5_000_000,
      on_plan: true,
      account_number: null,
      bank_name: null,
      fees: [{ name: 'Tuition', billed_kobo: 18_500_000, paid_kobo: 0, left_kobo: 18_500_000, status: 'Unpaid' }],
      earlier_left_kobo: 2_000_000,
      plan_offers: [],
    },
    {
      student_id: 's3',
      first_name: 'Chidi',
      class_name: 'SSS 1 Arts',
      owed_kobo: 0,
      suggested_kobo: 0,
      on_plan: false,
      account_number: '9900000003',
      bank_name: 'Wema Bank',
      fees: [{ name: 'Tuition', billed_kobo: 20_000_000, paid_kobo: 20_000_000, left_kobo: 0, status: 'Paid' }],
      earlier_left_kobo: 0,
      plan_offers: [],
    },
  ],
};

function renderPage() {
  return renderApp(
    <Routes>
      <Route path="/pay/f/:token" element={<FamilyPay />} />
    </Routes>,
    `/pay/f/${TOKEN}`,
  );
}

describe('family payment page', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('opens on one button that pays everything due now for all the children', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const calls = mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () => json(page),
      [`POST /api/v1/public/pay/${TOKEN}/checkout`]: () =>
        json({ reference: 'SP1', amount_kobo: 20_500_000, authorization_url: 'https://gateway.example/pay/SP1' }, 201),
    });
    renderPage();
    const top = within(await screen.findByRole('region', { name: 'Pay for all your children' }));
    expect(top.getByText('₦205,000.00')).toBeInTheDocument(); // Ada's 155k + Bayo's next instalment (50k)
    expect(screen.queryByLabelText(/Amount for Ada/)).not.toBeInTheDocument(); // no typing needed
    await userEvent.click(top.getByRole('button', { name: 'Pay ₦205,000.00' }));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(await post.json()).toEqual({ total_kobo: 20_500_000 });
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith('https://gateway.example/pay/SP1'));
  });

  it('shows the payment charge on top when the school asks parents to pay it', async () => {
    mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () =>
        json({ ...page, checkout_charge: { percent_bp: 150, flat_kobo: 10_000, flat_from_kobo: 250_000, cap_kobo: 200_000 } }),
    });
    renderPage();
    const top = within(await screen.findByRole('region', { name: 'Pay for all your children' }));
    expect(top.getByRole('button', { name: 'Pay ₦207,000.00' })).toBeInTheDocument(); // ₦205,000 + the ₦2,000 cap
    expect(top.getByLabelText('Payment charge')).toHaveTextContent('Includes a ₦2,000.00 payment charge');
    expect(top.getByLabelText('Payment charge')).toHaveTextContent('No charge if you transfer to your child’s own account number');
  });

  it('shows how a different amount will be shared before paying it', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const calls = mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () => json(page),
      [`POST /api/v1/public/pay/${TOKEN}/split`]: () =>
        json({
          split_rule: 'clear_most',
          shares: [
            { student_id: 's1', amount_kobo: 15_500_000, clears: true },
            { student_id: 's2', amount_kobo: 4_500_000, clears: false },
          ],
        }),
      [`POST /api/v1/public/pay/${TOKEN}/checkout`]: () =>
        json({ reference: 'SP1', amount_kobo: 20_000_000, authorization_url: 'https://gateway.example/pay/SP1' }, 201),
    });
    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'Pay a different amount' }));
    const box = within(screen.getByRole('region', { name: 'Pay a different amount' }));
    expect(box.getByText(/as many children as possible are fully paid/)).toBeInTheDocument();
    await userEvent.type(box.getByLabelText(/Amount for all the children/), '200000');
    const shared = within(await box.findByRole('list', { name: 'How it will be shared' }));
    expect(shared.getByText('Ada')).toBeInTheDocument();
    expect(shared.getByText('Fully paid')).toBeInTheDocument();
    await userEvent.click(box.getByRole('button', { name: 'Pay ₦200,000.00' }));
    const post = calls.find((c) => c.method === 'POST' && c.url.endsWith('/checkout'))!;
    expect(await post.json()).toEqual({ total_kobo: 20_000_000 });
  });

  it('can still choose the amount per child', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const calls = mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () => json(page),
      [`POST /api/v1/public/pay/${TOKEN}/checkout`]: () =>
        json({ reference: 'SP1', amount_kobo: 0, authorization_url: 'https://gateway.example/pay/SP1' }, 201),
    });
    renderPage();
    const ada = await screen.findByRole('region', { name: 'Ada' });
    expect(within(ada).getByText('9900000001')).toBeInTheDocument();
    const adaFees = within(ada).getByRole('list', { name: 'Fees' });
    expect(within(adaFees).getByText('WAEC')).toBeInTheDocument();
    expect(within(adaFees).getByText(/₦1,000.00 left/)).toBeInTheDocument(); // PTA part paid
    expect(within(screen.getByRole('region', { name: 'Bayo' })).getByText('From earlier terms')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Bayo' })).getByText(/₦50,000.00 is due next/)).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Chidi' })).getByText('All paid')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Choose the amount for each child' }));
    expect(screen.queryByLabelText(/Amount for Chidi/)).not.toBeInTheDocument();

    // Starts at Ada's 155k + Bayo's next instalment (50k)
    const top = within(screen.getByRole('region', { name: 'Pay the amounts chosen' }));
    expect(top.getByRole('button', { name: 'Pay ₦205,000.00 now' })).toBeEnabled();
    expect(top.getByText(/bank transfer/)).toBeInTheDocument();
    const bayo = screen.getByLabelText(/Amount for Bayo/);
    await userEvent.clear(bayo);
    await userEvent.type(bayo, '200000');
    expect(screen.getByText('Up to ₦185,000.00')).toBeInTheDocument();
    expect(top.getByRole('button', { name: /now$/ })).toBeDisabled();
    await userEvent.clear(bayo); // Bayo another time
    await userEvent.click(top.getByRole('button', { name: 'Pay ₦155,000.00 now' }));

    const post = calls.find((c) => c.method === 'POST')!;
    expect(await post.json()).toEqual({ items: [{ student_id: 's1', amount_kobo: 15_500_000, fees: [] }] });
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith('https://gateway.example/pay/SP1'));
  });

  it('pays for particular fees the parent ticks', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const calls = mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () => json(page),
      [`POST /api/v1/public/pay/${TOKEN}/checkout`]: () =>
        json({ reference: 'SP1', amount_kobo: 400_000, authorization_url: 'https://gateway.example/pay/SP1' }, 201),
    });
    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'Choose the amount for each child' }));
    const ada = screen.getByRole('region', { name: 'Ada' });
    await userEvent.click(within(ada).getByRole('checkbox', { name: /WAEC/ }));
    expect(within(ada).getByLabelText(/Amount for Ada/)).toHaveValue('4000');
    expect(within(ada).getByText('For WAEC')).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText(/Amount for Bayo/)); // Bayo another time
    await userEvent.click(screen.getByRole('button', { name: 'Pay ₦4,000.00 now' }));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(await post.json()).toEqual({ items: [{ student_id: 's1', amount_kobo: 400_000, fees: ['WAEC'] }] });
  });

  it('says plainly when a link no longer works', async () => {
    mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () =>
        error(404, 'not_found', 'This payment link has expired or been replaced. Ask the school to send you a new one.'),
    });
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('expired or been replaced');
  });

  it('lets staff copy or replace a parent link', async () => {
    const first = { url: 'https://app.example/pay/f/old', expires_at: '2027-02-01T00:00:00Z' };
    const second = { url: 'https://app.example/pay/f/new', expires_at: '2027-02-20T00:00:00Z' };
    mockApi({
      'GET /api/v1/guardians/g1/pay-link': () => json(first),
      'POST /api/v1/guardians/g1/pay-link/replace': () => json(second),
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderApp(<FamilyLinkDialog guardianId="g1" name="Mrs Adeyemi" onClose={() => {}} />);
    expect(await screen.findByLabelText('Payment link')).toHaveValue(first.url);
    await userEvent.click(screen.getByRole('button', { name: 'Replace link' }));
    await vi.waitFor(() => expect(screen.getByLabelText('Payment link')).toHaveValue(second.url));
  });

  it('lets a parent spread a child’s fees with one of the school’s plans', async () => {
    const offer = {
      invoice_id: 'inv1',
      template_id: 'pt1',
      name: 'Pay in 3',
      charge_kobo: 150_000,
      total_kobo: 15_650_000,
      instalments: [
        { due_date: '2030-01-10', amount_kobo: 7_825_000 },
        { due_date: '2030-02-10', amount_kobo: 3_912_500 },
        { due_date: '2030-03-10', amount_kobo: 3_912_500 },
      ],
    };
    const withOffer = { ...page, children: [{ ...page.children[0]!, plan_offers: [offer] }, ...page.children.slice(1)] };
    const onPlan = {
      ...page,
      children: [{ ...page.children[0]!, on_plan: true, owed_kobo: 15_650_000, suggested_kobo: 7_825_000 }, ...page.children.slice(1)],
    };
    const calls = mockApi({
      [`GET /api/v1/public/pay/${TOKEN}`]: () => json(withOffer),
      [`POST /api/v1/public/pay/${TOKEN}/plan`]: () => json(onPlan),
    });
    renderPage();
    const ada = within(await screen.findByRole('group', { name: "Spread Ada's fees" }));
    expect(ada.getByText('Pay in 3')).toBeInTheDocument();
    expect(ada.getByText(/Plan charge ₦1,500.00/)).toBeInTheDocument();
    await userEvent.click(ada.getByRole('button', { name: 'Choose' }));
    await userEvent.click(ada.getByRole('button', { name: 'Yes, choose Pay in 3' }));
    const post = await vi.waitFor(() => calls.find((c) => c.url.endsWith('/plan'))!);
    expect(await post.json()).toEqual({ invoice_id: 'inv1', template_id: 'pt1' });
    expect(await screen.findByText(/Paying in instalments: ₦78,250.00 is due next/)).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: "Spread Ada's fees" })).not.toBeInTheDocument();
  });
});
