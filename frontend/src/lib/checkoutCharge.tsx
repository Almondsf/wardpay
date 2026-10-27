import type { Schemas } from '../api/client';
import { formatKobo } from './format';

export type ChargeRule = Schemas['CheckoutChargeOut'];

/** The provider's charge on a payment of `gross` kobo (as the server works it out: rounded up, capped). */
function fee(rule: ChargeRule, gross: number): number {
  const percent = Math.ceil((gross * rule.percent_bp) / 10_000);
  return Math.min(percent + (gross >= rule.flat_from_kobo ? rule.flat_kobo : 0), rule.cap_kobo);
}

/**
 * What the parent pays on top of `amount` so the school still receives it in full (the server's
 * `ChargeRule.gross_up`; the server's figure is the one charged, this is only to show it beforehand).
 */
export function chargeFor(rule: ChargeRule | null | undefined, amount: number): number {
  if (!rule || amount <= 0) return 0;
  const keep = 10_000 - rule.percent_bp;
  const candidates = [
    Math.ceil((amount * 10_000) / keep),
    Math.ceil(((amount + rule.flat_kobo) * 10_000) / keep),
    amount + rule.cap_kobo,
  ].filter((g) => g - fee(rule, g) >= amount);
  let best = Math.min(...candidates);
  while (best > amount && best - 1 - fee(rule, best - 1) >= amount) best -= 1;
  return best - amount;
}

/** Under an online-payment button: the charge the school asks parents to pay, and how to avoid it. */
export function ChargeNote({
  rule,
  amount,
  transferFree = true,
}: {
  rule: ChargeRule | null | undefined;
  amount: number;
  transferFree?: boolean;
}) {
  const charge = chargeFor(rule, amount);
  if (charge <= 0) return null;
  return (
    <p className="hint" aria-label="Payment charge">
      Includes a {formatKobo(charge)} payment charge (the school’s choice) for {formatKobo(amount)} of fees.
      {transferFree && ' No charge if you transfer to your child’s own account number instead.'}
    </p>
  );
}
