import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../../api/client';
import { AuthProvider } from '../../auth/AuthContext';
import { json, me, mockApi, signedIn } from '../../test/fetchMock';
import { renderApp } from '../../test/render';
import { ConsoleApp } from '../ConsoleApp';

function renderSettings(capabilities: string[]) {
  const calls = mockApi({
    'POST /api/v1/auth/refresh': () => json(signedIn(me({ capabilities }))),
    'GET /api/v1/payment-settings': () => json({ gateways: ['paystack'], active_provider: 'paystack', account: null, accounts: [] }),
    'GET /api/v1/payment-settings/family-split-rule': () => json({ rule: 'clear_most' }),
    'PUT /api/v1/payment-settings/family-split-rule': async (request) => json(await request.json()),
    'GET /api/v1/payment-settings/checkout-charges': () => json({ payer: 'school' }),
    'PUT /api/v1/payment-settings/checkout-charges': async (request) => json(await request.json()),
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
    '/console/payment-settings',
  );
  return calls;
}

describe('family payment rule', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it('lets the proprietor choose how one amount is shared between children', async () => {
    const calls = renderSettings(['payments.view', 'payments.setup']);
    const card = within(await screen.findByRole('region', { name: 'Family payments' }));
    expect(await card.findByRole('radio', { name: /clear as many children as possible/ })).toBeChecked();
    await userEvent.click(card.getByRole('radio', { name: /In proportion/ }));
    expect(await card.findByText(/Parents see the new rule straight away/)).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(await put.json()).toEqual({ rule: 'proportional' });
  });

  it('lets the proprietor have parents pay the online payment charge, advising transfers', async () => {
    const calls = renderSettings(['payments.view', 'payments.setup']);
    const card = within(await screen.findByRole('region', { name: 'Online payment charges' }));
    expect(await card.findByRole('radio', { name: /The school/ })).toBeChecked();
    expect(card.getByText(/ask parents to pay by transfer into their child’s account number/)).toBeInTheDocument();
    await userEvent.click(card.getByRole('radio', { name: /The parent, added on top/ }));
    expect(await card.findByText(/applies to payments started from now on/)).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT' && c.url.endsWith('/checkout-charges'))!;
    expect(await put.json()).toEqual({ payer: 'parent' });
  });

  it('shows the rule to other staff without letting them change it', async () => {
    renderSettings(['payments.view']);
    const card = within(await screen.findByRole('region', { name: 'Family payments' }));
    expect(await card.findByRole('radio', { name: /Eldest child first/ })).toBeDisabled();
    expect(card.getByText('Only the proprietor can change this.')).toBeInTheDocument();
  });
});
