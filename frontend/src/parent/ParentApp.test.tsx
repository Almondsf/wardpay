import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../api/client';
import { error, json, mockApi } from '../test/fetchMock';
import { renderApp } from '../test/render';
import { ParentApp } from './ParentApp';

const child = (overrides: Record<string, unknown>) => ({
  id: 'c1',
  full_name: 'Tolu Adeyemi',
  first_name: 'Tolu',
  admission_number: 'DMC/1',
  class_name: 'JSS 1 A',
  owed_kobo: 15_500_000,
  paid_kobo: 0,
  credit_kobo: 0,
  pay_in: {
    account_number: '9820000015',
    account_name: 'DMC/ADEYEMI TOLU',
    bank_name: 'Wema Bank',
  },
  fees: [
    { name: 'Tuition', billed_kobo: 15_000_000, paid_kobo: 0, left_kobo: 15_000_000, status: 'Unpaid' },
    { name: 'PTA', billed_kobo: 500_000, paid_kobo: 0, left_kobo: 500_000, status: 'Unpaid' },
  ],
  earlier_left_kobo: 0,
  ...overrides,
});
const home = {
  guardian_name: 'Mrs Adeyemi',
  school_name: 'Demo Model College',
  children: [
    child({}),
    child({
      id: 'c2',
      full_name: 'Bisi Adeyemi',
      first_name: 'Bisi',
      owed_kobo: 0,
      pay_in: null,
    }),
  ],
};
const signedIn = {
  status: 'signed_in',
  access_token: 'parent-token',
  expires_in: 900,
};

function renderAt(path: string) {
  return renderApp(
    <Routes>
      <Route path="/parent/*" element={<ParentApp />} />
    </Routes>,
    path,
  );
}

describe('parent portal', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('signs in with phone and texted code, then shows the children', async () => {
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => error(401, 'session_expired'),
      'POST /api/v1/parent/auth/request-code': () => json({ detail: 'sent' }, 202),
      'POST /api/v1/parent/auth/verify': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
    });
    renderAt('/parent');
    await userEvent.type(await screen.findByLabelText('Phone number'), '0803 111 2222');
    await userEvent.click(screen.getByRole('button', { name: 'Text me a code' }));
    await userEvent.type(await screen.findByLabelText('Code'), '123456');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Hello, Mrs Adeyemi')).toBeInTheDocument();
    // One page: the total, then each child with everything needed to pay
    expect(screen.getByRole('region', { name: 'Summary' })).toHaveTextContent('for 1 child');
    expect(screen.getByText('9820000015')).toBeInTheDocument();
    expect(screen.getByText('All paid. Thank you!')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /Bills and receipts/ })).toHaveLength(2);
    expect(screen.getByText(/^Total owed:/)).toBeInTheDocument(); // only one of the two still owes
    expect(screen.getByRole('link', { name: 'Pay now' })).toHaveAttribute('href', '/parent/pay');
    const verify = calls.find((c) => c.url.endsWith('/parent/auth/verify'))!;
    expect(await verify.json()).toEqual({
      phone: '0803 111 2222',
      code: '123456',
    });
    const homeCall = calls.find((c) => c.url.endsWith('/parent/home'))!;
    expect(homeCall.headers.get('Authorization')).toBe('Bearer parent-token');
  });

  it('shows children at other branches of the group and opens one', async () => {
    let token = 'parent-token';
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => json(signedIn),
      'GET /api/v1/parent/home': () =>
        json(
          token === 'parent-token'
            ? home
            : { guardian_name: 'Mrs Adeyemi', school_name: 'Lekki Campus', children: [child({ id: 'c9', first_name: 'Kemi', full_name: 'Kemi Adeyemi' })] },
        ),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
      'GET /api/v1/parent/branches': () =>
        json(
          token === 'parent-token'
            ? [
                { guardian_id: 'g1', school_name: 'Demo Model College', children: ['Bisi', 'Tolu'], owed_kobo: 15_500_000, current: true },
                { guardian_id: 'g2', school_name: 'Lekki Campus', children: ['Kemi'], owed_kobo: 9_000_000, current: false },
              ]
            : [],
        ),
      'POST /api/v1/parent/auth/switch-branch': () => {
        token = 'lekki-token';
        return json({ ...signedIn, access_token: 'lekki-token' });
      },
    });
    renderAt('/parent');
    const others = await screen.findByRole('region', { name: 'Your children at other branches' });
    expect(others).toHaveTextContent('Lekki Campus');
    expect(others).toHaveTextContent('Kemi');
    expect(others).toHaveTextContent('To pay: ₦90,000');
    expect(within(others).queryByText('Demo Model College')).not.toBeInTheDocument();
    await userEvent.click(within(others).getByRole('button', { name: 'Open Lekki Campus' }));
    expect(await screen.findByText('Lekki Campus')).toBeInTheDocument();
    const switched = calls.find((c) => c.url.endsWith('/switch-branch'))!;
    expect(await switched.json()).toEqual({ guardian_id: 'g2' });
    const lastHome = calls.filter((c) => c.url.endsWith('/parent/home')).pop()!;
    expect(lastHome.headers.get('Authorization')).toBe('Bearer lekki-token');
  });

  it('lets a family at two schools choose one', async () => {
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => error(401, 'session_expired'),
      'POST /api/v1/parent/auth/request-code': () => json({ detail: 'sent' }, 202),
      'POST /api/v1/parent/auth/verify': () =>
        json({
          status: 'choose_school',
          ticket: 't',
          schools: [
            { guardian_id: 'g1', school_name: 'Alpha', children: ['Tolu'] },
            { guardian_id: 'g2', school_name: 'Beta', children: ['Kemi'] },
          ],
        }),
      'POST /api/v1/parent/auth/choose-school': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
    });
    renderAt('/parent/login');
    await userEvent.type(await screen.findByLabelText('Phone number'), '08031112222');
    await userEvent.click(screen.getByRole('button', { name: 'Text me a code' }));
    await userEvent.type(await screen.findByLabelText('Code'), '654321');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await userEvent.click(await screen.findByRole('button', { name: /Beta/ }));
    expect(await screen.findByText('Hello, Mrs Adeyemi')).toBeInTheDocument();
    const choose = calls.find((c) => c.url.endsWith('/choose-school'))!;
    expect(await choose.json()).toEqual({ ticket: 't', guardian_id: 'g2' });
  });

  it('lets the parent turn off bill messages and reminders', async () => {
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
      'PUT /api/v1/parent/preferences': async (request) => json(await request.json()),
    });
    renderAt('/parent/settings');
    const toggle = await screen.findByLabelText('Send me new bills and payment reminders');
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);
    await vi.waitFor(() => expect(toggle).not.toBeChecked());
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(await put.json()).toEqual({ reminders: false });
    expect(screen.getByText("You'll always get a receipt when a payment is received.")).toBeInTheDocument();
  });

  it('lets the parent ask for phone calls in Pidgin when the school makes calls', async () => {
    let prefs = { reminders: true, prefers_calls: false, language: 'en', calls_available: true };
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json(prefs),
      'PUT /api/v1/parent/preferences': async (request) => {
        prefs = { ...prefs, ...(await request.json()) };
        return json(prefs);
      },
    });
    renderAt('/parent/settings');
    await userEvent.click(await screen.findByLabelText(/Also phone me/));
    const language = await screen.findByLabelText('Language on the call');
    await userEvent.selectOptions(language, 'pcm');
    await vi.waitFor(() => expect(screen.getByLabelText('Language on the call')).toHaveValue('pcm'));
    const puts = calls.filter((c) => c.method === 'PUT');
    expect(await puts[0]!.json()).toEqual({ reminders: true, prefers_calls: true });
    expect(await puts[1]!.json()).toEqual({ reminders: true, language: 'pcm' });
  });

  it('pays for all the children at once from the portal', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const family = {
      school_name: 'Alpha College',
      card_payments: true,
      min_part_kobo: 100_000,
      split_rule: 'clear_most',
      children: [
        { student_id: 'c1', first_name: 'Tolu', class_name: 'JSS 1 A', owed_kobo: 5_000_000, suggested_kobo: 5_000_000, on_plan: false, account_number: null, bank_name: null, fees: [], earlier_left_kobo: 0 },
      ],
    };
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
      'GET /api/v1/parent/family': () => json(family),
      'POST /api/v1/parent/family/checkout': () => json({ reference: 'SP1', amount_kobo: 5_000_000, authorization_url: 'https://pay.example/SP1' }, 201),
    });
    renderAt('/parent');
    const box = await screen.findByRole('region', { name: 'Pay for all your children' });
    await userEvent.click(within(box).getByRole('link'));
    const pay = within(await screen.findByRole('region', { name: 'Pay for all your children' })).getByRole('button', { name: 'Pay ₦50,000.00' });
    await userEvent.click(pay);
    const post = calls.find((c) => c.url.endsWith('/parent/family/checkout'))!;
    expect(await post.json()).toEqual({ total_kobo: 5_000_000 });
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith('https://pay.example/SP1'));
  });

  it('restores a session from the parent refresh cookie', async () => {
    const calls = mockApi({
      'POST /api/v1/parent/auth/refresh': () => json(signedIn),
      'GET /api/v1/parent/home': () => json(home),
      'GET /api/v1/parent/preferences': () => json({ reminders: true }),
    });
    renderAt('/parent');
    expect(await screen.findByText('Hello, Mrs Adeyemi')).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith('/api/v1/auth/refresh'))).toBe(false); // not the staff endpoint
  });
});
