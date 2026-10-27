import { describe, expect, it } from 'vitest';

import { chargeFor } from './checkoutCharge';

const paystack = { percent_bp: 150, flat_kobo: 10_000, flat_from_kobo: 250_000, cap_kobo: 200_000 };

describe('the payment charge parents see before paying', () => {
  it('matches the server to the kobo (the same cases as tests/test_checkout_charges.py)', () => {
    expect(chargeFor(paystack, 100_000)).toBe(1_523); // ₦1,000: 1.5%, no ₦100 under ₦2,500
    expect(chargeFor(paystack, 1_000_000)).toBe(25_381); // ₦10,000: 1.5% + ₦100 on the grossed-up amount
    expect(chargeFor(paystack, 10_000_000)).toBe(162_437); // ₦100,000
    expect(chargeFor(paystack, 12_500_000)).toBe(200_000); // the ₦2,000 cap
    expect(chargeFor(paystack, 50_000_000)).toBe(200_000);
  });

  it('adds nothing when the school pays the charge', () => {
    expect(chargeFor(null, 10_000_000)).toBe(0);
    expect(chargeFor(undefined, 10_000_000)).toBe(0);
  });
});
