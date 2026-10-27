"""
Who pays the payment provider's charge on an online checkout (owner, 2026-10-27, "option 3"): each school
chooses under Payment settings.

- **The school** (the default): the parent pays exactly the amount for the fees; the provider keeps its charge
  from it, so a little less reaches the school's bank (shown under Paid into the bank). Like a POS terminal.
- **The parent**: the checkout adds the charge on top, worked out so that after the provider's charge the
  school still receives the fees in full. The bill is credited with the fees amount only; the charge is never
  part of the school's books.

Only the provider's own checkout page is affected (card, transfer or USSD there all cost the same: Paystack
charges every local checkout channel alike). A transfer into a pupil's own account number never goes through
checkout, costs the school far less (capped at ₦300) and can't carry an extra charge, so it stays free for
parents. That's why schools are advised to ask parents to pay into the account numbers.

The rates are Paystack's published Nigerian local pricing: 1.5%, plus ₦100 from ₦2,500, never more than
₦2,000 (https://support.paystack.com/hc/en-us/articles/360009881920). A provider whose rates aren't known here
gets no added charge (the school bears it), so a parent is never overcharged by a guess.
"""

from dataclasses import dataclass

from app.modules.schools.models import ChargePayer

from .gateways import GatewayCode

__all__ = ["PAYSTACK_LOCAL", "ChargePayer", "ChargeRule", "parent_charge", "rule_for"]


@dataclass(frozen=True)
class ChargeRule:
    percent_bp: int  # basis points of the amount charged (150 = 1.5%)
    flat_kobo: int  # added from `flat_from_kobo` upwards
    flat_from_kobo: int
    cap_kobo: int

    def fee(self, gross_kobo: int) -> int:
        """The provider's charge on a payment of `gross_kobo` (rounded up to the kobo, never short)."""
        fee = -(-gross_kobo * self.percent_bp // 10_000)
        if gross_kobo >= self.flat_from_kobo:
            fee += self.flat_kobo
        return min(fee, self.cap_kobo)

    def gross_up(self, net_kobo: int) -> int:
        """The smallest amount to charge so that `net_kobo` is left after the provider's charge."""
        keep = 10_000 - self.percent_bp
        candidates = [
            -(-net_kobo * 10_000 // keep),  # under the flat-fee threshold
            -(-(net_kobo + self.flat_kobo) * 10_000 // keep),  # with the flat fee
            net_kobo + self.cap_kobo,  # where the cap applies
        ]
        best = min(g for g in candidates if g - self.fee(g) >= net_kobo)
        while best > net_kobo and (best - 1) - self.fee(best - 1) >= net_kobo:  # rounding: never a kobo too much
            best -= 1
        return best


PAYSTACK_LOCAL = ChargeRule(percent_bp=150, flat_kobo=100_00, flat_from_kobo=2_500_00, cap_kobo=2_000_00)
# The fake gateway behaves like Paystack, so demos and tests show real figures
RULES: dict[GatewayCode, ChargeRule] = {GatewayCode.PAYSTACK: PAYSTACK_LOCAL, GatewayCode.FAKE: PAYSTACK_LOCAL}


def rule_for(gateway: GatewayCode, payer: ChargePayer) -> ChargeRule | None:
    """The rule parents are charged by, or None when the school bears the charge (or the rates aren't known)."""
    return RULES.get(gateway) if payer == ChargePayer.PARENT else None


def parent_charge(gateway: GatewayCode, payer: ChargePayer, amount_kobo: int) -> int:
    """What the parent pays on top of `amount_kobo` (0 when the school bears it)."""
    rule = rule_for(gateway, payer)
    return rule.gross_up(amount_kobo) - amount_kobo if rule else 0
