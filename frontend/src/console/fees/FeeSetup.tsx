import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { api, unwrap, type Schemas } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { formatKobo, humanize, koboToInput, parseNairaToKobo } from '../../lib/format';
import { Empty, ErrorAlert, Field, Loading, Modal, Notice, PageHeader } from '../../lib/ui';
import { useClassLevels, useCurrentTerm, useSessions } from '../hooks';
import { PlanTemplates } from './PlanTemplates';

type FeeItem = Schemas['FeeItemOut'];
type Discount = Schemas['DiscountOut'];
const CATEGORIES: Schemas['FeeCategory'][] = ['tuition', 'levy', 'service', 'materials', 'other'];

export function FeeSetup() {
  const { can } = useAuth();
  const [tab, setTab] = useState<'schedule' | 'items' | 'discounts' | 'plans'>('schedule');
  return (
    <>
      <PageHeader title="Fees" subtitle="What the school charges, per class and term. Changes apply to invoices generated afterwards." />
      <section className="card section" aria-label="Extra fees after billing">
        <h2>Extra fees after the bills have gone out</h2>
        <ul className="small">
          <li>
            <strong>For whole classes</strong> (WAEC, NECO, JAMB, an end-of-year party):{' '}
            {can('invoices.issue') ? <Link to="/console/billing">Billing → Add a fee to classes</Link> : 'Billing → Add a fee to classes'}. Untick
            pupils not taking part.
          </li>
          <li>
            <strong>For one pupil</strong> (uniform, textbooks): open the pupil from{' '}
            <Link to="/console/students">Students</Link> → Fees → Add a charge. Set prices under Fee items so they fill in.
          </li>
          <li>
            <strong>Leaving a pupil out</strong> of a class fee: on the pupil’s page, Exempt from a fee.
          </li>
        </ul>
        {!can('invoices.issue') && <p className="hint">Only the bursar or the proprietor can add fees to bills.</p>}
      </section>
      <div className="tabs" role="tablist">
        {(['schedule', 'items', 'discounts', 'plans'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'tab active' : 'tab'} onClick={() => setTab(t)}>
            {{ schedule: 'Fees per class', items: 'Fee items', discounts: 'Discounts', plans: 'Instalment plans' }[t]}
          </button>
        ))}
      </div>
      {tab === 'schedule' && <Schedule />}
      {tab === 'items' && <FeeItems />}
      {tab === 'discounts' && <Discounts />}
      {tab === 'plans' && <PlanTemplates />}
    </>
  );
}

const useFeeItems = () => useQuery({ queryKey: ['fee-items'], queryFn: async () => unwrap(await api.GET('/api/v1/fee-items')) });

// --- Schedule grid -----------------------------------------------------------------------------------

interface Cell {
  amount: string;
  optional: boolean;
}

function Schedule() {
  const { can } = useAuth();
  const manage = can('fees.manage');
  const queryClient = useQueryClient();
  const sessions = useSessions();
  const current = useCurrentTerm();
  const levels = useClassLevels();
  const items = useFeeItems();
  const [termId, setTermId] = useState('');
  const term = termId || current.data?.id || '';
  const schedule = useQuery({
    queryKey: ['fee-schedule', term],
    enabled: !!term,
    queryFn: async () => unwrap(await api.GET('/api/v1/terms/{term_id}/fee-schedule', { params: { path: { term_id: term } } })),
  });
  const [cells, setCells] = useState<Record<string, Cell>>({});
  const [copyFrom, setCopyFrom] = useState('');
  const key = (itemId: string, levelId: string) => `${itemId}|${levelId}`;

  const saved = useMemo(() => {
    const map: Record<string, Cell> = {};
    for (const e of schedule.data ?? []) map[key(e.fee_item_id, e.class_level_id)] = { amount: koboToInput(e.amount_kobo), optional: e.is_optional };
    return map;
  }, [schedule.data]);
  useEffect(() => setCells(saved), [saved]);

  const activeItems = (items.data ?? []).filter((i) => i.is_active);
  const activeLevels = (levels.data ?? []).filter((l) => l.is_active);
  const changed = Object.keys({ ...cells, ...saved }).filter((k) => {
    const a = cells[k];
    const b = saved[k];
    return (a?.amount ?? '') !== (b?.amount ?? '') || (a?.optional ?? false) !== (b?.optional ?? false);
  });
  const invalid = changed.filter((k) => cells[k]?.amount && parseNairaToKobo(cells[k]!.amount) === null);

  const save = useMutation({
    mutationFn: async () => {
      const entries = changed.map((k) => {
        const [fee_item_id, class_level_id] = k.split('|') as [string, string];
        const cell = cells[k];
        const amount = cell?.amount ? parseNairaToKobo(cell.amount) : null;
        return { fee_item_id, class_level_id, amount_kobo: amount || null, is_optional: cell?.optional ?? false };
      });
      return unwrap(await api.PUT('/api/v1/terms/{term_id}/fee-schedule', { params: { path: { term_id: term } }, body: { entries } }));
    },
    onSuccess: (data) => {
      queryClient.setQueryData(['fee-schedule', term], data.entries);
      void queryClient.invalidateQueries({ queryKey: ['bill-sync', term] });
      if (data.bills.bills_changed > 0) {
        // A fee added to a class goes onto that class's bills already sent (owner, 2026-10-21)
        void queryClient.invalidateQueries({ queryKey: ['account'] });
        void queryClient.invalidateQueries({ queryKey: ['invoices'] });
      }
    },
  });
  // Fees on the list that the term's bills (already sent) don't have yet
  const missing = useQuery({
    queryKey: ['bill-sync', term],
    enabled: !!term && can('invoices.issue'),
    staleTime: 0,
    queryFn: async () =>
      unwrap(await api.POST('/api/v1/terms/{term_id}/bills/sync', { params: { path: { term_id: term } }, body: { dry_run: true } })),
  });
  const addToBills = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/terms/{term_id}/bills/sync', { params: { path: { term_id: term } }, body: { dry_run: false } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['bill-sync', term] });
      void queryClient.invalidateQueries({ queryKey: ['account'] });
      void queryClient.invalidateQueries({ queryKey: ['invoices'] });
    },
  });
  const copy = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/terms/{term_id}/fee-schedule/copy', { params: { path: { term_id: term } }, body: { from_term_id: copyFrom } })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['fee-schedule', term] }),
  });

  if (current.isPending || items.isPending || levels.isPending) return <Loading />;
  if (!term) return <Empty>Set up the school calendar first.</Empty>;
  if (activeItems.length === 0 || activeLevels.length === 0) return <Empty>Add fee items and classes first.</Empty>;

  const terms = (sessions.data ?? []).flatMap((s) => s.terms.map((t) => ({ id: t.id, label: `${s.name} · ${t.label}` })));
  return (
    <div className="card section">
      <div className="filters">
        <select aria-label="Term" value={term} onChange={(e) => setTermId(e.target.value)}>
          {terms.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </select>
        {manage && (
          <>
            <select aria-label="Copy from" value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)}>
              <option value="">Copy amounts from…</option>
              {terms.filter((t) => t.id !== term).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
            <button className="secondary" disabled={!copyFrom || copy.isPending} onClick={() => copy.mutate()}>
              Copy
            </button>
          </>
        )}
      </div>
      {copy.data && <Notice>{copy.data.copied} amounts copied (amounts already set were kept).</Notice>}
      {missing.data && missing.data.bills_changed > 0 && (
        <section className="alert info" aria-label="Fees not on the bills yet">
          <p>
            <strong>This term’s bills have already gone out</strong>, and these fees aren’t on them yet:
          </p>
          <ul>
            {missing.data.fees.map((f) => (
              <li key={f.item_name}>
                {f.item_name} for {f.class_names.join(', ')}: {f.bills} {f.bills === 1 ? 'pupil' : 'pupils'}, {formatKobo(f.total_kobo)}
              </li>
            ))}
          </ul>
          <button onClick={() => addToBills.mutate()} disabled={addToBills.isPending}>
            {addToBills.isPending ? 'Adding…' : `Add them to ${missing.data.bills_changed} ${missing.data.bills_changed === 1 ? 'bill' : 'bills'}`}
          </button>{' '}
          <span className="hint">Pupils exempted from a fee, and optional fees a pupil hasn’t taken, are left out.</span>
        </section>
      )}
      {save.data && save.data.bills.bills_changed > 0 && (
        <Notice>
          Saved, and added to the bills already sent:{' '}
          {save.data.bills.fees.map((f) => `${f.item_name} for ${f.class_names.join(', ')} (${f.bills} ${f.bills === 1 ? 'pupil' : 'pupils'})`).join('; ')}
          . Parents see the new amount on their bill.
        </Notice>
      )}
      {addToBills.data && (
        <Notice>
          Added to {addToBills.data.bills_changed} bills ({formatKobo(addToBills.data.total_kobo)}).
        </Notice>
      )}
      <ErrorAlert error={save.error ?? copy.error ?? addToBills.error} />
      <p className="hint">Amounts in naira. Leave a box empty if the class doesn't pay that fee. Tick "optional" for fees only some students pay (e.g. bus).</p>
      <div className="table-scroll">
        <table className="grid">
          <thead>
            <tr>
              <th>Fee</th>
              {activeLevels.map((l) => (
                <th key={l.id}>{l.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {activeItems.map((item) => (
              <tr key={item.id}>
                <th scope="row">{item.name}</th>
                {activeLevels.map((level) => {
                  const k = key(item.id, level.id);
                  const cell = cells[k] ?? { amount: '', optional: false };
                  const bad = invalid.includes(k);
                  return (
                    <td key={level.id}>
                      <input
                        aria-label={`${item.name} for ${level.name}`}
                        className={bad ? 'invalid' : ''}
                        value={cell.amount}
                        inputMode="decimal"
                        disabled={!manage}
                        onChange={(e) => setCells({ ...cells, [k]: { ...cell, amount: e.target.value } })}
                      />
                      {cell.amount && (
                        <label className="tiny">
                          <input
                            type="checkbox"
                            checked={cell.optional}
                            disabled={!manage}
                            onChange={(e) => setCells({ ...cells, [k]: { ...cell, optional: e.target.checked } })}
                          />{' '}
                          optional
                        </label>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {manage && (
        <div className="row end">
          {invalid.length > 0 && <span className="error-text">Fix the highlighted amounts.</span>}
          <button onClick={() => save.mutate()} disabled={changed.length === 0 || invalid.length > 0 || save.isPending}>
            Save {changed.length > 0 ? `${changed.length} change${changed.length === 1 ? '' : 's'}` : ''}
          </button>
        </div>
      )}
    </div>
  );
}

// --- Fee items ---------------------------------------------------------------------------------------

function FeeItems() {
  const { can } = useAuth();
  const manage = can('fees.manage');
  const queryClient = useQueryClient();
  const items = useFeeItems();
  const [name, setName] = useState('');
  const [category, setCategory] = useState<Schemas['FeeCategory']>('tuition');
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['fee-items'] });
  const create = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/fee-items', { body: { name, category } })),
    onSuccess: () => {
      setName('');
      refresh();
    },
  });
  const update = useMutation({
    mutationFn: async ({ item, body }: { item: FeeItem; body: Schemas['FeeItemUpdateIn'] }) =>
      unwrap(await api.PATCH('/api/v1/fee-items/{item_id}', { params: { path: { item_id: item.id } }, body })),
    onSuccess: refresh,
  });

  if (items.isPending) return <Loading />;
  return (
    <div className="card section">
      <ErrorAlert error={create.error ?? update.error} />
      {manage && (
        <form
          className="filters"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <input placeholder="New fee, e.g. Tuition, School bus" value={name} onChange={(e) => setName(e.target.value)} required minLength={2} />
          <select aria-label="Category" value={category} onChange={(e) => setCategory(e.target.value as Schemas['FeeCategory'])}>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {humanize(c)}
              </option>
            ))}
          </select>
          <button type="submit" disabled={create.isPending}>
            Add fee
          </button>
        </form>
      )}
      {(items.data ?? []).length === 0 ? (
        <Empty>No fees yet.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Fee</th>
              <th>Category</th>
              <th className="num" title="For charging one pupil, e.g. a uniform or textbooks">
                Standard price
              </th>
              {manage && <th aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {(items.data ?? []).map((item) => (
              <tr key={item.id} className={item.is_active ? '' : 'retired'}>
                <td>
                  {item.name} {!item.is_active && <span className="badge">Retired</span>}
                </td>
                <td>{humanize(item.category)}</td>
                <td className="num">
                  {manage ? (
                    <PriceInput item={item} onSave={(kobo) => update.mutate({ item, body: { default_amount_kobo: kobo } })} />
                  ) : item.default_amount_kobo ? (
                    formatKobo(item.default_amount_kobo)
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                {manage && (
                  <td className="actions">
                    <button className="secondary small" onClick={() => update.mutate({ item, body: { is_active: !item.is_active } })}>
                      {item.is_active ? 'Retire' : 'Restore'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** The item's standard price, saved when the field loses focus (empty clears it). */
function PriceInput({ item, onSave }: { item: FeeItem; onSave: (kobo: number | null) => void }) {
  const saved = item.default_amount_kobo ? koboToInput(item.default_amount_kobo) : '';
  const [value, setValue] = useState(saved);
  const kobo = value.trim() ? parseNairaToKobo(value) : null;
  return (
    <input
      className="price-input"
      aria-label={`Standard price for ${item.name}`}
      inputMode="decimal"
      placeholder="None"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => {
        if (value !== saved && (kobo === null || kobo > 0)) onSave(kobo);
      }}
    />
  );
}

// --- Discounts ---------------------------------------------------------------------------------------

export function describeDiscount(d: Discount, items: FeeItem[] | undefined): string {
  const amount = d.kind === 'percent' ? `${d.value / 100}%` : formatKobo(d.value);
  const on = d.fee_item_id ? ` of ${items?.find((i) => i.id === d.fee_item_id)?.name ?? 'one fee'}` : ' of all fees';
  return `${amount}${on}${d.rule === 'sibling' ? ', for every child after the eldest' : ''}`;
}

function Discounts() {
  const { can } = useAuth();
  const manage = can('fees.manage');
  const queryClient = useQueryClient();
  const items = useFeeItems();
  const discounts = useQuery({ queryKey: ['discounts'], queryFn: async () => unwrap(await api.GET('/api/v1/discounts')) });
  const [adding, setAdding] = useState(false);
  const update = useMutation({
    mutationFn: async (d: Discount) =>
      unwrap(await api.PATCH('/api/v1/discounts/{discount_id}', { params: { path: { discount_id: d.id } }, body: { is_active: !d.is_active } })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['discounts'] }),
  });

  if (discounts.isPending) return <Loading />;
  return (
    <div className="card section">
      <ErrorAlert error={update.error} />
      {manage && (
        <div className="row end">
          <button onClick={() => setAdding(true)}>Add discount</button>
        </div>
      )}
      {(discounts.data ?? []).length === 0 ? (
        <Empty>No discounts. Add a sibling discount, staff-child discount or scholarship here, then give it to students on their page.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Discount</th>
              <th>Amount</th>
              <th>Given</th>
              {manage && <th aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {(discounts.data ?? []).map((d) => (
              <tr key={d.id} className={d.is_active ? '' : 'retired'}>
                <td>{d.name}</td>
                <td>{describeDiscount(d, items.data)}</td>
                <td>{d.rule === 'sibling' ? 'Automatically' : 'To chosen students'}</td>
                {manage && (
                  <td className="actions">
                    <button className="secondary small" onClick={() => update.mutate(d)}>
                      {d.is_active ? 'Retire' : 'Restore'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {adding && <AddDiscount items={items.data ?? []} onClose={() => setAdding(false)} />}
    </div>
  );
}

function AddDiscount({ items, onClose }: { items: FeeItem[]; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ name: '', kind: 'percent' as Schemas['DiscountKind'], value: '', rule: 'manual' as Schemas['DiscountRule'], fee_item_id: '' });
  const value =
    form.kind === 'percent'
      ? /^\d{1,3}(\.\d{1,2})?$/.test(form.value)
        ? Math.round(Number(form.value) * 100)
        : null
      : parseNairaToKobo(form.value);
  const create = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/discounts', { body: { ...form, value: value ?? 0, fee_item_id: form.fee_item_id || null } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['discounts'] });
      onClose();
    },
  });
  return (
    <Modal title="Add discount" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <ErrorAlert error={create.error} />
        <Field label="Name">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required minLength={2} placeholder="e.g. Staff child 50%" />
        </Field>
        <div className="grid-2 tight">
          <Field label="Type">
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Schemas['DiscountKind'] })}>
              <option value="percent">Percentage</option>
              <option value="fixed">Fixed amount</option>
            </select>
          </Field>
          <Field label={form.kind === 'percent' ? 'Percent' : 'Amount (₦)'} error={form.value && value === null ? 'Not a valid number' : undefined}>
            <input value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} inputMode="decimal" required />
          </Field>
        </div>
        <Field label="Applies to">
          <select value={form.fee_item_id} onChange={(e) => setForm({ ...form, fee_item_id: e.target.value })}>
            <option value="">All fees on the invoice</option>
            {items.filter((i) => i.is_active).map((i) => (
              <option key={i.id} value={i.id}>
                {i.name} only
              </option>
            ))}
          </select>
        </Field>
        <Field label="Who gets it">
          <select value={form.rule} onChange={(e) => setForm({ ...form, rule: e.target.value as Schemas['DiscountRule'] })}>
            <option value="manual">Students I choose (scholarship, staff child)</option>
            <option value="sibling">Automatically: every child after the eldest in a family</option>
          </select>
        </Field>
        <div className="row end">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={!value || create.isPending}>
            Add discount
          </button>
        </div>
      </form>
    </Modal>
  );
}
