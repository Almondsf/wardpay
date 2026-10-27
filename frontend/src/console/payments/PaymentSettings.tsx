import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { BRAND } from '../../lib/brand';
import { ErrorAlert, Field, Loading, Notice, PageHeader } from '../../lib/ui';

type Gateway = Schemas['GatewayCode'];
export const GATEWAY_NAMES: Record<Gateway, string> = {
  paystack: 'Paystack',
  bachs: 'Bachs',
  flutterwave: 'Flutterwave',
  fake: 'Test gateway (no real money)',
};

export function PaymentSettings() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ['payment-settings'], queryFn: async () => unwrap(await api.GET('/api/v1/payment-settings')) });
  const provision = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/virtual-accounts/provision', { body: {} })),
  });
  const [editing, setEditing] = useState(false);
  const bachs = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/payment-settings/bachs/onboarding')),
    onSuccess: (link) => window.location.assign(link.url),
  });

  if (settings.isPending) return <Loading />;
  if (settings.isError) return <ErrorAlert error={settings.error} />;
  const account = settings.data.account;
  const onBachs = settings.data.accounts.some((a) => a.gateway === 'bachs');
  const bankGateways = settings.data.gateways.filter((g) => g !== 'bachs');

  return (
    <>
      <PageHeader
        title="Payment settings"
        subtitle={`Parents' payments settle straight into the school's own bank account. ${BRAND} never holds the money.`}
      />
      <Notice>
        Payments currently go through <strong>{GATEWAY_NAMES[settings.data.active_provider]}</strong>
        {account && account.gateway !== settings.data.active_provider
          ? `. This school isn't set up on it yet, so its payments still go through ${GATEWAY_NAMES[account.gateway]}.`
          : '.'}
      </Notice>
      <div className="grid-2">
        <div className="card">
          <h2>School bank account</h2>
          {account && !editing ? (
            <>
              <dl className="details">
                <dt>Bank</dt>
                <dd>{account.bank_name}</dd>
                <dt>Account</dt>
                <dd>
                  {account.account_name} (****{account.account_number_last4})
                </dd>
                <dt>Provider</dt>
                <dd>{GATEWAY_NAMES[account.gateway]}</dd>
              </dl>
              {settings.data.accounts.length > 1 && (
                <p className="small muted">
                  Also set up on: {settings.data.accounts.filter((a) => a.gateway !== account.gateway).map((a) => GATEWAY_NAMES[a.gateway]).join(', ')}
                </p>
              )}
              {can('payments.setup') && (
                <div className="row wrap">
                  {bankGateways.length > 0 && (
                    <button className="secondary" onClick={() => setEditing(true)}>
                      Connect a bank account
                    </button>
                  )}
                  {settings.data.gateways.includes('bachs') && (
                    <button className="secondary" onClick={() => bachs.mutate()} disabled={bachs.isPending}>
                      {onBachs ? 'Finish setting up with Bachs' : 'Set up with Bachs'}
                    </button>
                  )}
                </div>
              )}
              <ErrorAlert error={bachs.error} />
            </>
          ) : can('payments.setup') ? (
            <>
              {settings.data.gateways.includes('bachs') && (
                <p>
                  <button onClick={() => bachs.mutate()} disabled={bachs.isPending}>
                    Set up with Bachs
                  </button>{' '}
                  <span className="small muted">Bachs takes the school's details and bank account on its own pages.</span>
                </p>
              )}
              <ErrorAlert error={bachs.error} />
              {bankGateways.length > 0 && <ConnectAccount gateways={bankGateways} onDone={() => {
              setEditing(false);
              void queryClient.invalidateQueries({ queryKey: ['payment-settings'] });
            }} />}
            </>
          ) : (
            <p className="muted">Not set up yet. The proprietor connects the school's bank account here.</p>
          )}
        </div>
        <div className="card">
          <h2>Students' account numbers</h2>
          <p>
            Each student gets their own bank account number. Any transfer into it is matched to that student automatically, so parents can pay
            from any bank app.
          </p>
          <ErrorAlert error={provision.error} />
          {provision.data && <Notice>Requested account numbers for {provision.data.requested} students. They appear on each student's page shortly.</Notice>}
          {can('payments.approve') && (
            <button onClick={() => provision.mutate()} disabled={!account || provision.isPending}>
              Issue account numbers to students without one
            </button>
          )}
        </div>
      </div>
      <CheckoutChargesCard />
      <FamilySplitRuleCard />
    </>
  );
}

/**
 * Who pays the payment provider's charge when a parent pays on its checkout page (card, transfer or USSD there all
 * cost the same). The proprietor chooses; parents see the charge before paying.
 */
function CheckoutChargesCard() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const current = useQuery({
    queryKey: ['checkout-charges'],
    queryFn: async () => unwrap(await api.GET('/api/v1/payment-settings/checkout-charges')),
  });
  const save = useMutation({
    mutationFn: async (payer: Schemas['ChargePayer']) =>
      unwrap(await api.PUT('/api/v1/payment-settings/checkout-charges', { body: { payer } })),
    onSuccess: (data) => queryClient.setQueryData(['checkout-charges'], data),
  });
  const editable = can('payments.setup');
  return (
    <section className="card section page--narrow" aria-label="Online payment charges">
      <h2>Charges on online payments</h2>
      <p className="small">
        When a parent pays on the payment page (card, bank transfer or USSD there), Paystack charges 1.5% plus ₦100 (no ₦100 under
        ₦2,500), never more than ₦2,000. A transfer into a pupil’s own account number costs much less, at most ₦300, and that charge
        always stays with the school.
      </p>
      <p className="small">
        <strong>Our advice:</strong> ask parents to pay by transfer into their child’s account number. It’s the cheapest for everyone and the
        money is matched to the child automatically.
      </p>
      {current.isPending ? (
        <Loading />
      ) : (
        <fieldset disabled={!editable || save.isPending}>
          <legend>Who pays the charge on the payment page</legend>
          <label className="checkbox">
            <input
              type="radio"
              name="checkout-charge-payer"
              checked={current.data?.payer === 'school'}
              onChange={() => save.mutate('school')}
            />
            <span>
              The school
              <span className="hint">Parents pay exactly the fees; the charge comes off what reaches the school’s bank (like POS).</span>
            </span>
          </label>
          <label className="checkbox">
            <input
              type="radio"
              name="checkout-charge-payer"
              checked={current.data?.payer === 'parent'}
              onChange={() => save.mutate('parent')}
            />
            <span>
              The parent, added on top
              <span className="hint">
                For ₦100,000 of fees the parent pays ₦101,624.37 and the school receives ₦100,000. Parents see the charge before they pay.
              </span>
            </span>
          </label>
        </fieldset>
      )}
      <ErrorAlert error={current.error ?? save.error} />
      {save.isSuccess && <Notice>Saved. It applies to payments started from now on.</Notice>}
      {!editable && <p className="hint">Only the proprietor can change this.</p>}
    </section>
  );
}

const SPLIT_RULES: { rule: Schemas['FamilySplitRule']; label: string; hint: string }[] = [
  {
    rule: 'clear_most',
    label: 'Old balances first, then clear as many children as possible',
    hint: 'Earlier terms first; then the child with the smallest balance this term, so fewer children are sent home.',
  },
  { rule: 'proportional', label: 'In proportion to what each child owes', hint: 'Every child gets a fair share; none may be fully paid.' },
  { rule: 'eldest_first', label: 'Eldest child first', hint: 'By date of birth, or the highest class where no date is recorded.' },
  { rule: 'youngest_first', label: 'Youngest child first', hint: 'By date of birth, or the lowest class where no date is recorded.' },
];

/** How one amount a parent pays for several children is shared. The proprietor chooses; the parent sees it before paying. */
function FamilySplitRuleCard() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const current = useQuery({
    queryKey: ['family-split-rule'],
    queryFn: async () => unwrap(await api.GET('/api/v1/payment-settings/family-split-rule')),
  });
  const save = useMutation({
    mutationFn: async (rule: Schemas['FamilySplitRule']) =>
      unwrap(await api.PUT('/api/v1/payment-settings/family-split-rule', { body: { rule } })),
    onSuccess: (data) => queryClient.setQueryData(['family-split-rule'], data),
  });
  const editable = can('payments.setup');
  return (
    <section className="card section page--narrow" aria-label="Family payments">
      <h2>When a parent pays one amount for several children</h2>
      <p className="small">
        Parents can pay for all their children with one amount from their payment link. This is how it's shared between the children. Each
        child still gets their own receipt.
      </p>
      {current.isPending ? (
        <Loading />
      ) : (
        <fieldset disabled={!editable || save.isPending}>
          <legend>Share it</legend>
          {SPLIT_RULES.map((r) => (
            <label key={r.rule} className="checkbox">
              <input
                type="radio"
                name="family-split-rule"
                value={r.rule}
                checked={current.data?.rule === r.rule}
                onChange={() => save.mutate(r.rule)}
              />
              <span>
                {r.label}
                <span className="hint">{r.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      <ErrorAlert error={current.error ?? save.error} />
      {save.isSuccess && <Notice>Saved. Parents see the new rule straight away.</Notice>}
      {!editable && <p className="hint">Only the proprietor can change this.</p>}
    </section>
  );
}

function ConnectAccount({ gateways, onDone }: { gateways: Gateway[]; onDone: () => void }) {
  const [gateway, setGateway] = useState<Gateway>(gateways[0] ?? 'paystack');
  const [bankCode, setBankCode] = useState('');
  const [accountNumber, setAccountNumber] = useState('');
  const banks = useQuery({
    queryKey: ['banks', gateway],
    queryFn: async () => unwrap(await api.GET('/api/v1/payment-settings/banks', { params: { query: { gateway } } })),
  });
  const save = useMutation({
    mutationFn: async () => unwrap(await api.PUT('/api/v1/payment-settings', { body: { gateway, bank_code: bankCode, account_number: accountNumber } })),
    onSuccess: onDone,
  });
  return (
    <form
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <ErrorAlert error={save.error} />
      <Field label="Payment provider">
        <select value={gateway} onChange={(e) => setGateway(e.target.value as Gateway)}>
          {gateways.map((g) => (
            <option key={g} value={g}>
              {GATEWAY_NAMES[g]}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Bank">
        <select value={bankCode} onChange={(e) => setBankCode(e.target.value)} required>
          <option value="">{banks.isPending ? 'Loading banks…' : 'Choose…'}</option>
          {(banks.data ?? []).map((b) => (
            <option key={b.code} value={b.code}>
              {b.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Account number" hint="The school's own account. The provider confirms the account name.">
        <input value={accountNumber} onChange={(e) => setAccountNumber(e.target.value.replace(/\D/g, ''))} inputMode="numeric" pattern="\d{10}" maxLength={10} required />
      </Field>
      <button type="submit" disabled={save.isPending}>
        {save.isPending ? 'Checking with the bank…' : 'Connect account'}
      </button>
    </form>
  );
}
