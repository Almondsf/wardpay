"""
The school's own instalment plans, which parents can choose themselves (owner, 2026-10-25).

A school sets up plans such as "Pay in 2: 60% now, 40% by mid-term" or "Pay in 3: 50% / 25% / 25%". Each
part is a share of what's owed and a day counted from the start of the term, so one plan serves every term.
Per plan the school decides who may choose it (any parent, only parents with nothing owed on the pupil's
other bills, or staff only) and an optional plan charge (fixed or a percentage, at most 20%) for the extra
payment fees a plan costs it; the charge goes on the bill as its own line and, like all school money, into
the school's account.

A plan is offered on a term bill that's still owed, has no plan yet, and whose term hasn't ended or been
closed. Parts whose day has already passed fall due today (merged into one), and a plan with fewer than two
parts left isn't offered. Choosing one adds the charge line, then the plan, in one transaction; from then on
it's an ordinary payment plan (reminders follow it; staff can cancel it). WardPay charges the school a fee
per plan made, on its next termly bill (platform/billing.py).

Offers are worked out in a fixed number of queries; the session is scoped to the school.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import lagos_today
from app.core.errors import Conflict, DomainError, NotFound
from app.core.http import RequestMeta
from app.modules.accounts.deps import StaffContext
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record
from app.modules.schools.models import School, Term

from .invoicing import totals_subquery
from .ledger import paid_subquery
from .models import (
    Invoice,
    InvoiceKind,
    InvoiceLine,
    LineKind,
    PaymentPlan,
    PlanChargeKind,
    PlanEligibility,
    PlanTemplate,
)
from .plans import MAX_INSTALMENTS, MIN_INSTALMENTS, add_plan

FULL = 10_000  # basis points: 100%
MAX_PERCENT_BP = 2_000
MAX_DAY = 200


# --- The school's plans ------------------------------------------------------------------------------


def _clean_parts(parts: list[tuple[int, int]]) -> list[dict[str, int]]:
    """(share in basis points, day from term start) pairs: checked, in order."""
    if not MIN_INSTALMENTS <= len(parts) <= MAX_INSTALMENTS:
        raise DomainError(f"A plan has {MIN_INSTALMENTS} to {MAX_INSTALMENTS} payments.", field="parts")
    if any(bp <= 0 for bp, _ in parts):
        raise DomainError("Every payment must be a share of the bill.", field="parts")
    if sum(bp for bp, _ in parts) != FULL:
        raise DomainError("The shares must add up to 100%.", field="parts")
    days = [day for _, day in parts]
    if any(not 0 <= d <= MAX_DAY for d in days):
        raise DomainError(f"Days are counted from the start of term, 0 to {MAX_DAY}.", field="parts")
    if any(b <= a for a, b in pairwise(days)):
        raise DomainError("Each payment must fall later than the one before.", field="parts")
    return [{"percent_bp": bp, "day": day} for bp, day in parts]


def templates(session: Session) -> list[PlanTemplate]:
    return list(session.scalars(select(PlanTemplate).order_by(PlanTemplate.is_active.desc(), PlanTemplate.name)))


def save_template(
    session: Session,
    ctx: StaffContext,
    template_id: uuid.UUID | None,
    *,
    name: str,
    is_active: bool,
    eligibility: PlanEligibility,
    charge_kind: PlanChargeKind,
    charge_value: int,
    parts: list[tuple[int, int]],
) -> PlanTemplate:
    name = name.strip()
    if len(name) < 2:
        raise DomainError("Give the plan a name, e.g. Pay in 3.", field="name")
    if charge_kind == PlanChargeKind.NONE:
        charge_value = 0
    elif charge_value <= 0:
        raise DomainError("Enter the plan charge, or choose no charge.", field="charge_value")
    elif charge_kind == PlanChargeKind.PERCENT and charge_value > MAX_PERCENT_BP:
        raise DomainError("A plan charge can be at most 20%.", field="charge_value")
    cleaned = _clean_parts(parts)
    clash = session.scalar(
        select(PlanTemplate.id).where(func.lower(PlanTemplate.name) == name.lower(), PlanTemplate.id != template_id)
    )
    if clash is not None:
        raise Conflict("There's already a plan with that name.", field="name")
    if template_id is None:
        template = PlanTemplate(id=uuid.uuid4(), school_id=ctx.school_id)
        session.add(template)
    else:
        found = session.scalar(select(PlanTemplate).where(PlanTemplate.id == template_id).with_for_update())
        if found is None:
            raise NotFound("Plan not found.")
        template = found
    template.name, template.is_active, template.eligibility = name, is_active, eligibility
    template.charge_kind, template.charge_value, template.parts = charge_kind, charge_value, cleaned
    session.flush()
    record(
        session,
        AuditAction.FEES_CHANGED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=template,
        changes={
            "school_plan": name,
            "active": is_active,
            "who": eligibility,
            "charge": [charge_kind, charge_value],
            "parts": cleaned,
        },
    )
    session.commit()
    return template


# --- What a bill is offered ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    invoice_id: uuid.UUID
    student_id: uuid.UUID
    template_id: uuid.UUID
    name: str
    charge_kobo: int
    instalments: list[tuple[date, int]]  # what the parent would pay, and when (the charge included)

    @property
    def total_kobo(self) -> int:
        return sum(amount for _, amount in self.instalments)


def charge_for(template: PlanTemplate, owed_kobo: int) -> int:
    if template.charge_kind == PlanChargeKind.FIXED:
        return template.charge_value
    if template.charge_kind == PlanChargeKind.PERCENT:
        return owed_kobo * template.charge_value // FULL
    return 0


def schedule(template: PlanTemplate, term_start: date, total_kobo: int, today: date) -> list[tuple[date, int]] | None:
    """The dated amounts for `total_kobo`. Parts already due fall due today, merged; None if under two are left."""
    merged: list[tuple[date, int]] = []  # (due, share in basis points)
    for part in template.parts:
        due = max(term_start + timedelta(days=part["day"]), today)
        if merged and merged[-1][0] == due:
            merged[-1] = (due, merged[-1][1] + part["percent_bp"])
        else:
            merged.append((due, part["percent_bp"]))
    if len(merged) < MIN_INSTALMENTS:
        return None
    amounts = [total_kobo * bp // FULL for _, bp in merged]
    amounts[-1] += total_kobo - sum(amounts)  # the last takes what rounding left
    if any(a <= 0 for a in amounts):
        return None
    return [(due, amount) for (due, _), amount in zip(merged, amounts, strict=True)]


@dataclass(frozen=True)
class _Bill:
    invoice_id: uuid.UUID
    student_id: uuid.UUID
    owed_kobo: int
    term_start: date | None  # None: not a live term bill
    on_plan: bool


def _bills(session: Session, student_ids: list[uuid.UUID], today: date) -> list[_Bill]:
    """Every live bill of these pupils with what's owed. One query."""
    totals, paid = totals_subquery(), paid_subquery()
    closed = select(School.books_closed_through).where(School.id == Invoice.school_id).scalar_subquery()
    live_plan = (
        select(PaymentPlan.id).where(PaymentPlan.invoice_id == Invoice.id, PaymentPlan.cancelled_at.is_(None)).exists()
    )
    rows = session.execute(
        select(
            Invoice.id,
            Invoice.student_id,
            func.coalesce(totals.c.total_kobo, 0) - func.coalesce(paid.c.paid_kobo, 0),
            Invoice.kind,
            Term.start_date,
            Term.end_date,
            closed,
            live_plan,
        )
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .outerjoin(Term, Term.id == Invoice.term_id)
        .where(Invoice.student_id.in_(student_ids), Invoice.voided_at.is_(None))
    ).all()
    bills = []
    for invoice_id, student_id, owed, kind, start, end, closed_through, on_plan in rows:
        open_term = (
            kind == InvoiceKind.TERM
            and end is not None
            and end >= today
            and (closed_through is None or end > closed_through)
        )
        bills.append(_Bill(invoice_id, student_id, int(owed), start if open_term else None, bool(on_plan)))
    return bills


def offers(
    session: Session, student_ids: list[uuid.UUID], *, staff: bool = False, today: date | None = None
) -> dict[uuid.UUID, list[Offer]]:
    """invoice id -> the plans it can go on. Parents see their own choices; staff also see staff-only plans."""
    today = today or lagos_today()
    if not student_ids:
        return {}
    active = [t for t in templates(session) if t.is_active and (staff or t.eligibility != PlanEligibility.STAFF_ONLY)]
    if not active:
        return {}
    bills = _bills(session, student_ids, today)
    owing_elsewhere: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for b in bills:
        if b.owed_kobo > 0:
            owing_elsewhere[b.student_id].add(b.invoice_id)
    found: dict[uuid.UUID, list[Offer]] = {}
    for b in bills:
        if b.term_start is None or b.on_plan or b.owed_kobo <= 0:
            continue
        old_debts = bool(owing_elsewhere[b.student_id] - {b.invoice_id})
        for t in active:
            if t.eligibility == PlanEligibility.NO_OLD_DEBTS and old_debts and not staff:
                continue
            charge = charge_for(t, b.owed_kobo)
            dated = schedule(t, b.term_start, b.owed_kobo + charge, today)
            if dated:
                found.setdefault(b.invoice_id, []).append(
                    Offer(b.invoice_id, b.student_id, t.id, t.name, charge, dated)
                )
    return found


# --- Choosing one -------------------------------------------------------------------------------------


def choose(
    session: Session,
    invoice_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    school_id: uuid.UUID,
    actor: Actor,
    meta: RequestMeta | None,
    created_by: uuid.UUID | None,
    staff: bool,
    student_ids: list[uuid.UUID] | None = None,
) -> PaymentPlan:
    """
    Put a bill on one of the school's plans: the plan charge as a line, then the plan. `student_ids`: the
    pupils a parent may act for (a parent can only choose for their own children).
    """
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())
    if invoice is None or (student_ids is not None and invoice.student_id not in student_ids):
        raise NotFound("Bill not found.")
    offer = next(
        (
            o
            for o in offers(session, [invoice.student_id], staff=staff).get(invoice.id, [])
            if o.template_id == template_id
        ),
        None,
    )
    if offer is None:
        raise Conflict("That plan isn't available for this bill.", code="plan_not_offered")
    if offer.charge_kobo:
        position = (
            session.scalar(select(func.max(InvoiceLine.position)).where(InvoiceLine.invoice_id == invoice.id)) or 0
        ) + 1
        session.add(
            InvoiceLine(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                position=position,
                kind=LineKind.FEE,
                description=f"Instalment plan charge ({offer.name})",
                amount_kobo=offer.charge_kobo,
                created_by=created_by,
                reason=f"Chose the {offer.name} plan",
            )
        )
        session.flush()
    plan = add_plan(
        session,
        invoice,
        offer.instalments,
        note=f"{offer.name} (chosen by the parent)" if created_by is None else offer.name,
        created_by=created_by,
        actor=actor,
        meta=meta,
        school_id=school_id,
        template_id=template_id,
    )
    session.commit()
    return plan
