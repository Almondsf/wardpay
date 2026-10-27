import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { useParams } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../api/client';
import { BRAND } from '../lib/brand';
import { formatKobo, koboToInput, parseNairaToKobo } from '../lib/format';
import { FeeList, FeePicker, leftOn } from '../lib/FeeBreakdown';
import { ErrorAlert, Field, Loading } from '../lib/ui';
import { PlanChoices, type PlanOffer } from './PlanChoices';
import { ChargeNote, chargeFor, type ChargeRule } from '../lib/checkoutCharge';

type Child = Schemas['FamilyChildOut'];
type Checkout = (body: Schemas['FamilyCheckoutIn']) => Promise<Schemas['FamilyCheckoutOut']>;
type Preview = (totalKobo: number) => Promise<Schemas['FamilySplitOut']>;
type ChoosePlan = (offer: PlanOffer) => Promise<unknown>;
type Mode = 'all' | 'amount' | 'each';

/** How the school shares one amount, in a parent's words. */
const RULE_WORDS: Record<Schemas['FamilySplitRule'], string> = {
  clear_most: 'older balances first, then so that as many children as possible are fully paid',
  proportional: 'in proportion to what each child owes',
  eldest_first: 'eldest child first',
  youngest_first: 'youngest child first',
};

/** A parent's family payment page, opened from the link in their bills and reminders (no sign-in). */
export function FamilyPay() {
  const { token = '' } = useParams();
  const page = useQuery({
    queryKey: ['family-pay', token],
    staleTime: 0,
    retry: false,
    queryFn: async () => unwrap(await api.GET('/api/v1/public/pay/{token}', { params: { path: { token } } })),
  });
  const checkout: Checkout = async (body) =>
    unwrap(await api.POST('/api/v1/public/pay/{token}/checkout', { params: { path: { token } }, body }));
  const preview: Preview = async (total) =>
    unwrap(await api.POST('/api/v1/public/pay/{token}/split', { params: { path: { token } }, body: { total_kobo: total } }));
  const queryClient = useQueryClient();
  const choosePlan: ChoosePlan = async (offer) => {
    const updated = unwrap(
      await api.POST('/api/v1/public/pay/{token}/plan', {
        params: { path: { token } },
        body: { invoice_id: offer.invoice_id, template_id: offer.template_id },
      }),
    );
    queryClient.setQueryData(['family-pay', token], updated);
  };

  return (
    <div className="parent-shell">
      <header className="parent-bar">
        <strong>{page.data?.school_name ?? BRAND}</strong>
      </header>
      <main className="parent-page">
        {page.isPending ? (
          <Loading />
        ) : page.isError ? (
          <ErrorAlert error={page.error} />
        ) : (
          <FamilyPayment key={planKey(page.data)} data={page.data} checkout={checkout} preview={preview} choosePlan={choosePlan} />
        )}
      </main>
    </div>
  );
}

/**
 * Paying for all of a parent's children at once (the family link, and "Pay for all your
 * children" in the portal). It opens on one button that pays everything due now on the
 * provider's page (transfer, card or USSD). A parent can instead pay one smaller amount, shared
 * by the school's rule, or choose per child. Each child's own account number is shown too.
 */
/** Changes when a plan is chosen, so the page starts afresh with the new amounts due. */
export const planKey = (data: Schemas['FamilyPageOut']) => data.children.map((c) => `${c.student_id}:${c.on_plan}`).join(',');

export function FamilyPayment({
  data,
  checkout,
  preview,
  choosePlan,
  onPlanChosen = () => undefined,
}: {
  data: Schemas['FamilyPageOut'];
  checkout: Checkout;
  preview: Preview;
  choosePlan?: ChoosePlan;
  onPlanChosen?: () => void;
}) {
  const owing = data.children.filter((c) => c.owed_kobo > 0);
  const [mode, setMode] = useState<Mode>('all');
  // What the parent will pay per child, as typed (prefilled with what's due now)
  const [amounts, setAmounts] = useState<Record<string, string>>(() =>
    Object.fromEntries(owing.map((c) => [c.student_id, koboToInput(c.suggested_kobo)])),
  );
  // Fees the parent ticked per child (e.g. only WAEC); none: the amount goes to the bill in its usual order
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const pick = (child: Child, names: string[]) => {
    setPicked({ ...picked, [child.student_id]: names });
    setAmounts({ ...amounts, [child.student_id]: koboToInput(names.length ? leftOn(child.fees, names) : child.suggested_kobo) });
  };
  const parsed = owing.map((c) => {
    const text = amounts[c.student_id] ?? '';
    const kobo = text.trim() === '' ? 0 : parseNairaToKobo(text);
    const problem =
      kobo === null
        ? 'Not a valid amount'
        : kobo > c.owed_kobo
          ? `Up to ${formatKobo(c.owed_kobo)}`
          : kobo > 0 && kobo < Math.min(data.min_part_kobo, c.owed_kobo)
            ? `At least ${formatKobo(data.min_part_kobo)}`
            : undefined;
    return { child: c, kobo: kobo ?? 0, problem };
  });
  const total = parsed.reduce((sum, p) => sum + p.kobo, 0);
  const valid = total > 0 && parsed.every((p) => !p.problem);
  const paying = parsed.filter((p) => p.kobo > 0).length;
  const dueNow = owing.reduce((sum, c) => sum + c.suggested_kobo, 0);
  const owedAll = owing.reduce((sum, c) => sum + c.owed_kobo, 0);
  const pay = useMutation({
    mutationFn: (body: Schemas['FamilyCheckoutIn']) => checkout(body),
    onSuccess: (started) => window.location.assign(started.authorization_url),
  });
  const payEach = () =>
    pay.mutate({
      items: parsed
        .filter((p) => p.kobo > 0)
        .map((p) => ({ student_id: p.child.student_id, amount_kobo: p.kobo, fees: picked[p.child.student_id] ?? [] })),
    });

  if (data.children.length === 0) {
    return (
      <section className="card">
        <p>There are no current pupils here. If that's wrong, please contact the school.</p>
      </section>
    );
  }

  const onProviderPage = (
    <p className="hint">
      On the next page, pay by <strong>bank transfer</strong>, card or USSD. Each child gets their own receipt.
    </p>
  );
  const switches = (
    <div className="row wrap small">
      {mode !== 'all' && (
        <button className="link" onClick={() => setMode('all')}>
          Pay everything due
        </button>
      )}
      {mode !== 'amount' && owing.length > 1 && (
        <button className="link" onClick={() => setMode('amount')}>
          Pay a different amount
        </button>
      )}
      {mode !== 'each' && (
        <button className="link" onClick={() => setMode('each')}>
          {owing.length > 1 ? 'Choose the amount for each child' : 'Pay part, or for particular fees'}
        </button>
      )}
    </div>
  );
  const payOnline = data.card_payments && owing.length > 0;

  return (
    <>
      <h1>School fees</h1>
      {owing.length === 0 && (
        <section className="card">
          <p className="success-text">
            <strong>Everything is paid. Thank you!</strong>
          </p>
        </section>
      )}
      {payOnline && mode === 'all' && (
        <section className="card pay-all" aria-label="Pay for all your children">
          <span className="muted">Due now for {owing.length === 1 ? owing[0]!.first_name : `all ${owing.length} children`}</span>
          <strong className="pay-all-amount">{formatKobo(dueNow)}</strong>
          <ErrorAlert error={pay.error} />
          <button className="block" onClick={() => pay.mutate({ total_kobo: dueNow })} disabled={dueNow <= 0 || pay.isPending}>
            {pay.isPending ? 'Opening…' : `Pay ${formatKobo(dueNow + chargeFor(data.checkout_charge, dueNow))}`}
          </button>
          <ChargeNote rule={data.checkout_charge} amount={dueNow} />
          {onProviderPage}
          {switches}
        </section>
      )}
      {payOnline && mode === 'amount' && (
        <OneAmount
          owedKobo={owedAll}
          minKobo={Math.min(data.min_part_kobo, owedAll)}
          owing={owing}
          rule={RULE_WORDS[data.split_rule]}
          charge={data.checkout_charge}
          preview={preview}
          pending={pay.isPending}
          error={pay.error}
          onPay={(kobo) => pay.mutate({ total_kobo: kobo })}
        >
          {onProviderPage}
          {switches}
        </OneAmount>
      )}
      {data.children.map((c) => (
        <ChildCard
          key={c.student_id}
          child={c}
          payOnline={payOnline && mode === 'each'}
          amount={amounts[c.student_id] ?? ''}
          problem={parsed.find((p) => p.child.student_id === c.student_id)?.problem}
          onAmount={(value) => setAmounts({ ...amounts, [c.student_id]: value })}
          picked={picked[c.student_id] ?? []}
          onPick={(names) => pick(c, names)}
          plans={
            choosePlan && !c.on_plan ? (
              <PlanChoices firstName={c.first_name} offers={c.plan_offers ?? []} choose={choosePlan} onChosen={onPlanChosen} />
            ) : null
          }
        />
      ))}
      {payOnline && mode === 'each' && (
        <section className="card" aria-label="Pay the amounts chosen">
          <div className="row between">
            <span>
              Total for {paying} {paying === 1 ? 'child' : 'children'}
            </span>
            <strong>{formatKobo(total)}</strong>
          </div>
          {owing.length > 1 && <p className="hint">Change any amount above, or clear it to leave that child for later.</p>}
          <ErrorAlert error={pay.error} />
          <button className="block" onClick={payEach} disabled={!valid || pay.isPending}>
            {pay.isPending ? 'Opening…' : `Pay ${formatKobo(total + chargeFor(data.checkout_charge, total))} now`}
          </button>
          <ChargeNote rule={data.checkout_charge} amount={total} />
          {onProviderPage}
          {switches}
        </section>
      )}
    </>
  );
}

/** One amount for the whole family; before paying, the page shows how the school's rule will share it. */
function OneAmount({
  owedKobo,
  minKobo,
  owing,
  rule,
  charge,
  preview,
  pending,
  error,
  onPay,
  children,
}: {
  owedKobo: number;
  minKobo: number;
  owing: Child[];
  rule: string;
  charge: ChargeRule | null | undefined;
  preview: Preview;
  pending: boolean;
  error: unknown;
  onPay: (kobo: number) => void;
  children: ReactNode;
}) {
  const [text, setText] = useState('');
  const kobo = text.trim() === '' ? null : parseNairaToKobo(text);
  const problem =
    text.trim() === ''
      ? undefined
      : kobo === null
        ? 'Not a valid amount'
        : kobo > owedKobo
          ? `Up to ${formatKobo(owedKobo)}`
          : kobo < minKobo
            ? `At least ${formatKobo(minKobo)}`
            : undefined;
  const ready = kobo !== null && !problem;
  // Ask the school how it would share this amount, a moment after the parent stops typing
  const [asked, setAsked] = useState<number | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => setAsked(ready ? kobo : null), 400);
    return () => clearTimeout(timer);
  }, [kobo, ready]);
  const shares = useQuery({
    queryKey: ['family-split', asked],
    enabled: asked !== null,
    queryFn: () => preview(asked ?? 0),
  });
  const names = Object.fromEntries(owing.map((c) => [c.student_id, c.first_name]));
  return (
    <section className="card" aria-label="Pay a different amount">
      <Field label="Amount for all the children (₦)" error={problem} hint={`The school shares it ${rule}.`}>
        <input id="family-amount" inputMode="decimal" value={text} onChange={(e) => setText(e.target.value)} placeholder="0" />
      </Field>
      {shares.data && ready && asked === kobo && (
        <ul className="plain split-preview" aria-label="How it will be shared">
          {shares.data.shares
            .filter((s) => s.amount_kobo > 0)
            .map((s) => (
              <li key={s.student_id} className="row between">
                <span>{names[s.student_id] ?? 'Child'}</span>
                <span>
                  {formatKobo(s.amount_kobo)}
                  {s.clears && <span className="badge ok">Fully paid</span>}
                </span>
              </li>
            ))}
        </ul>
      )}
      <ErrorAlert error={error ?? shares.error} />
      <button className="block" onClick={() => kobo !== null && onPay(kobo)} disabled={!ready || pending}>
        {pending ? 'Opening…' : ready ? `Pay ${formatKobo(kobo + chargeFor(charge, kobo))}` : 'Enter an amount'}
      </button>
      {ready && <ChargeNote rule={charge} amount={kobo} />}
      {children}
    </section>
  );
}

function ChildCard({
  child,
  payOnline,
  amount,
  problem,
  onAmount,
  picked,
  onPick,
  plans,
}: {
  plans?: ReactNode;
  child: Child;
  payOnline: boolean;
  amount: string;
  problem?: string;
  onAmount: (value: string) => void;
  picked: string[];
  onPick: (names: string[]) => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <section className="card child-card" aria-label={child.first_name}>
      <div>
        <h2>{child.first_name}</h2>
        <div className="muted small">{child.class_name ?? 'No class this term'}</div>
      </div>
      <div className={`owed ${child.owed_kobo > 0 ? 'due' : 'clear'}`}>
        {child.owed_kobo > 0 ? (
          <>
            <span className="muted small">Owed</span>
            <strong>{formatKobo(child.owed_kobo)}</strong>
          </>
        ) : (
          <strong>All paid</strong>
        )}
        {child.on_plan && child.suggested_kobo < child.owed_kobo && (
          <span className="small">Paying in instalments: {formatKobo(child.suggested_kobo)} is due next</span>
        )}
      </div>
      <FeeList fees={child.fees} earlierKobo={child.earlier_left_kobo} />
      {plans}
      {payOnline && child.owed_kobo > 0 && child.fees.some((f) => f.left_kobo > 0) && (
        <FeePicker fees={child.fees} picked={picked} onChange={onPick} label="Pay for particular fees? (optional)" />
      )}
      {payOnline && child.owed_kobo > 0 && (
        <Field
          label={`Amount for ${child.first_name} now (₦)`}
          hint={picked.length ? `For ${picked.join(', ')}` : undefined}
          error={problem}
        >
          <input inputMode="decimal" value={amount} onChange={(e) => onAmount(e.target.value)} placeholder="0" />
        </Field>
      )}
      {child.account_number && child.owed_kobo > 0 && (
        <div className="transfer">
          <div className="muted small">Or transfer any time to {child.first_name}'s own account</div>
          <div className="row between">
            <span className="account-number">{child.account_number}</span>
            <button
              className="secondary small"
              onClick={() =>
                void navigator.clipboard?.writeText(child.account_number ?? '').then(() => {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 2000);
                })
              }
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <div className="hint">
            {child.bank_name}. This number is for {child.first_name} only and never changes. Any amount, any time, with a receipt.
          </div>
        </div>
      )}
    </section>
  );
}
