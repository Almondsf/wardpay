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
from app.core.config import get_settings
from app.core.errors import Conflict, DomainError, NotFound
from app.core.http import RequestMeta
from app.core.money import format_naira
from app.modules.accounts.deps import StaffContext
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record
from app.modules.schools.models import School, Term
from app.modules.students.models import Guardian, GuardianStudent, Student, StudentStatus

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
    PlanInstalment,
    PlanTemplate,
)
from .plans import MAX_INSTALMENTS, MIN_INSTALMENTS

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
    standard = rates(session, ctx.school_id).standard(len(cleaned))
    if charge_kind == PlanChargeKind.FIXED and charge_value < standard:
        raise DomainError(
            f"The plan charge can't be less than {format_naira(standard)}, {get_settings().app_name}'s standard for "
            f"{len(cleaned)} payments. Choose the standard charge, or more.",
            field="charge_value",
        )
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


@dataclass(frozen=True)
class Rates:
    """WardPay's standard plan charge (owner, 2026-10-25): per payment (Paystack's transfer cap), plus its fee."""

    per_payment_kobo: int = 30_000
    plan_fee_kobo: int = 20_000

    def standard(self, payments: int) -> int:
        return payments * self.per_payment_kobo + self.plan_fee_kobo


def rates(session: Session, school_id: uuid.UUID) -> Rates:
    """The school's pricing plan's rates (its own plan, else the default). One query."""
    from app.modules.platform.models import Plan  # platform billing reads fees; imported here

    own = select(School.plan_id).where(School.id == school_id).scalar_subquery()
    default = select(Plan.id).where(Plan.is_default.is_(True)).scalar_subquery()
    row = session.execute(
        select(Plan.plan_charge_per_payment_kobo, Plan.instalment_plan_fee_kobo).where(
            Plan.id == func.coalesce(own, default)
        )
    ).first()
    return Rates(int(row[0]), int(row[1])) if row else Rates()


def charge_for(template: PlanTemplate, owed_kobo: int, payments: int, rates: Rates) -> int:
    """The plan charge: the school's own if it chose more, never less than WardPay's standard."""
    own = 0
    if template.charge_kind == PlanChargeKind.FIXED:
        own = template.charge_value
    elif template.charge_kind == PlanChargeKind.PERCENT:
        own = owed_kobo * template.charge_value // FULL
    return max(own, rates.standard(payments))


def remaining_parts(template: PlanTemplate, term_start: date, today: date) -> list[tuple[date, int]]:
    """(due date, share in basis points) still to come: parts already due fall due today, merged."""
    merged: list[tuple[date, int]] = []
    for part in template.parts:
        due = max(term_start + timedelta(days=part["day"]), today)
        if merged and merged[-1][0] == due:
            merged[-1] = (due, merged[-1][1] + part["percent_bp"])
        else:
            merged.append((due, part["percent_bp"]))
    return merged


def schedule(parts: list[tuple[date, int]], total_kobo: int) -> list[tuple[date, int]] | None:
    """The dated amounts for `total_kobo`; None if under two payments are left."""
    if len(parts) < MIN_INSTALMENTS:
        return None
    amounts = [total_kobo * bp // FULL for _, bp in parts]
    amounts[-1] += total_kobo - sum(amounts)  # the last takes what rounding left
    if any(a <= 0 for a in amounts):
        return None
    return [(due, amount) for (due, _), amount in zip(parts, amounts, strict=True)]


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
    session: Session,
    school_id: uuid.UUID,
    student_ids: list[uuid.UUID],
    *,
    staff: bool = False,
    today: date | None = None,
) -> dict[uuid.UUID, list[Offer]]:
    """invoice id -> the plans it can go on. Parents see their own choices; staff also see staff-only plans."""
    today = today or lagos_today()
    if not student_ids:
        return {}
    active = [t for t in templates(session) if t.is_active and (staff or t.eligibility != PlanEligibility.STAFF_ONLY)]
    if not active:
        return {}
    bills = _bills(session, student_ids, today)
    rated = rates(session, school_id)
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
            parts = remaining_parts(t, b.term_start, today)
            charge = charge_for(t, b.owed_kobo, len(parts), rated)
            dated = schedule(parts, b.owed_kobo + charge)
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
    Put one bill on one of the school's plans. `student_ids`: the pupils a parent may act for (a parent can
    only choose for their own children).
    """
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())
    if invoice is None or (student_ids is not None and invoice.student_id not in student_ids):
        raise NotFound("Bill not found.")
    [result] = choose_many(
        session,
        [invoice],
        template_id,
        school_id=school_id,
        actor=actor,
        meta=meta,
        created_by=created_by,
        staff=staff,
    )
    if result.plan is None:
        raise Conflict("That plan isn't available for this bill.", code="plan_not_offered")
    session.commit()
    return result.plan


@dataclass(frozen=True)
class Applied:
    invoice: Invoice
    plan: PaymentPlan | None  # None: the plan isn't available for this bill (paid, already on a plan…)


def choose_many(
    session: Session,
    invoices: list[Invoice],
    template_id: uuid.UUID,
    *,
    school_id: uuid.UUID,
    actor: Actor,
    meta: RequestMeta | None,
    created_by: uuid.UUID | None,
    staff: bool,
) -> list[Applied]:
    """
    Put several bills (locked by the caller) on one of the school's plans: each plan charge as a line, then
    the plans, in a fixed number of statements however many bills. Bills the plan isn't available for are
    left alone. The caller commits.
    """
    from .invoicing import amounts_for  # invoicing imports this module's siblings

    by_invoice = {
        o.invoice_id: o
        for bill_offers in offers(session, school_id, list({i.student_id for i in invoices}), staff=staff).values()
        for o in bill_offers
        if o.template_id == template_id
    }
    chosen = [(i, by_invoice[i.id]) for i in invoices if i.id in by_invoice]
    if not chosen:
        return [Applied(i, None) for i in invoices]
    charged = [(i, o) for i, o in chosen if o.charge_kobo]
    if charged:
        positions = dict(
            session.execute(
                select(InvoiceLine.invoice_id, func.max(InvoiceLine.position))
                .where(InvoiceLine.invoice_id.in_([i.id for i, _ in charged]))
                .group_by(InvoiceLine.invoice_id)
            ).all()
        )
        session.add_all(
            InvoiceLine(
                id=uuid.uuid4(),
                invoice_id=i.id,
                position=int(positions.get(i.id) or 0) + 1,
                kind=LineKind.FEE,
                description=f"Instalment plan charge ({o.name})",
                amount_kobo=o.charge_kobo,
                created_by=created_by,
                reason=f"Put on the {o.name} plan",
            )
            for i, o in charged
        )
        session.flush()
    paid = amounts_for(session, [i.id for i, _ in chosen])
    who = "chosen by the parent" if created_by is None else None
    plans = {
        i.id: PaymentPlan(
            id=uuid.uuid4(),
            school_id=school_id,
            invoice_id=i.id,
            student_id=i.student_id,
            paid_at_start_kobo=paid[i.id].paid_kobo,
            covered_kobo=o.total_kobo,
            note=f"{o.name} ({who})" if who else o.name,
            created_by=created_by,
            template_id=template_id,
        )
        for i, o in chosen
    }
    session.add_all(plans.values())
    session.flush()
    session.add_all(
        PlanInstalment(school_id=school_id, plan_id=plans[i.id].id, number=n, due_date=d, amount_kobo=amount)
        for i, o in chosen
        for n, (d, amount) in enumerate(o.instalments, start=1)
    )
    for i, o in chosen:  # audit rows, flushed together
        record(
            session,
            AuditAction.PAYMENT_PLAN_CREATED,
            school_id=school_id,
            actor=actor,
            meta=meta,
            target=i,
            changes={
                "number": i.number,
                "covered_kobo": o.total_kobo,
                "school_plan": o.name,
                "plan_charge_kobo": o.charge_kobo,
                "instalments": [{"due": d.isoformat(), "amount_kobo": a} for d, a in o.instalments],
            },
        )
    session.flush()
    return [Applied(i, plans.get(i.id)) for i in invoices]


# --- A family, put on a plan by staff ------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyChild:
    student_id: uuid.UUID
    first_name: str
    offers: list[Offer]


def family_offers(session: Session, school_id: uuid.UUID, guardian_id: uuid.UUID) -> list[FamilyChild]:
    """A parent's current children and the plans (staff-only ones included) each one's bill can go on."""
    guardian = session.scalar(select(Guardian.id).where(Guardian.id == guardian_id))
    if guardian is None:
        raise NotFound("Parent not found.")
    children = _children(session, guardian_id)
    found = offers(session, school_id, [sid for sid, _ in children], staff=True)
    per_child: dict[uuid.UUID, list[Offer]] = defaultdict(list)
    for bill_offers in found.values():
        for o in bill_offers:
            per_child[o.student_id].append(o)
    return [FamilyChild(sid, name, per_child.get(sid, [])) for sid, name in children]


def _children(session: Session, guardian_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    return [
        (sid, name)
        for sid, name in session.execute(
            select(Student.id, Student.first_name)
            .join(GuardianStudent, GuardianStudent.student_id == Student.id)
            .where(GuardianStudent.guardian_id == guardian_id, Student.status == StudentStatus.ACTIVE)
            .order_by(Student.first_name, Student.id)
        )
    ]


@dataclass(frozen=True)
class FamilyResult:
    applied: list[Applied]
    messages: int  # reminders queued to tell the parent
    told: bool  # False: the parent has turned reminders off, so nothing was sent


def apply_for_family(
    session: Session, ctx: StaffContext, guardian_id: uuid.UUID, template_id: uuid.UUID, *, tell_parent: bool
) -> FamilyResult:
    """
    Staff put every current child of one parent on a school plan (for parents who asked, and won't use the
    website), and optionally tell the parent at once: a reminder per child, worded around the plan's first
    payment. A fixed number of statements however many children.
    """
    from app.modules.notifications.models import MessageKind  # notifications reads fees; imported here
    from app.modules.notifications.service import NewMessage, queue

    guardian = session.scalar(select(Guardian).where(Guardian.id == guardian_id))
    if guardian is None:
        raise NotFound("Parent not found.")
    student_ids = [sid for sid, _ in _children(session, guardian_id)]
    bills = list(
        session.scalars(
            select(Invoice)
            .where(
                Invoice.student_id.in_(student_ids),
                Invoice.kind == InvoiceKind.TERM,
                Invoice.voided_at.is_(None),
            )
            .order_by(Invoice.id)
            .with_for_update()
        )
    )
    applied = choose_many(
        session,
        bills,
        template_id,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        created_by=ctx.user.id,
        staff=True,
    )
    made = [a for a in applied if a.plan is not None]
    if not made:
        raise Conflict("That plan isn't available for any of this parent's children.", code="plan_not_offered")
    told = tell_parent and guardian.reminders_opt_out_at is None
    messages = (
        queue(
            session,
            ctx.school_id,
            [
                NewMessage(guardian.id, a.invoice.student_id, MessageKind.REMINDER, f"plan-agreed:{a.plan.id}")
                for a in made
                if a.plan is not None
            ],
        )
        if told
        else 0
    )
    session.commit()
    return FamilyResult(made, messages, told)
