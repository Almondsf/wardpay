"""
What schools pay the platform (the model of ParentPay in the UK, PLAN.md §3.4).

Once a term, a platform admin generates the subscription bills: one per active
school for its current term. Each bill has:

- the plan's set-up fee, on the school's first bill only;
- a third of the yearly per-pupil fee for each pupil enrolled that term.

Generating again is safe: a school already billed for its term is skipped (and a
partial unique index makes a second live bill impossible). Bills are numbered
platform-wide with no gaps. Schools pay by bank transfer to the platform's own
account (`PLATFORM_ACCOUNT_*`); a platform admin records each payment.
Everything is a fixed number of queries however many schools there are.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, and_, case, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.core import locks, outbox
from app.core.clock import LAGOS
from app.core.config import get_settings
from app.core.errors import Conflict, DomainError, NotFound
from app.core.money import format_naira
from app.core.sequences import reserve_platform
from app.modules.accounts.deps import Principal
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.fees.models import PaymentPlan
from app.modules.schools.models import AcademicSession, School, SchoolStatus, Term
from app.modules.students.models import Enrollment, Student, StudentStatus

from .models import Plan, SubscriptionInvoice, SubscriptionLine, SubscriptionLineKind, SubscriptionPayment

SEQUENCE = "subscription_invoice"
TERM_NAMES = ("First", "Second", "Third")
EMAIL_TASK = "platform.send_subscription_invoice"


def term_label(session_name: str, term_number: int) -> str:
    return f"{session_name} {TERM_NAMES[term_number - 1]} term"


def termly(per_pupil_year_kobo: int) -> int:
    """A third of the yearly per-pupil fee, rounded to the nearest kobo (halves up)."""
    return (per_pupil_year_kobo * 2 + 3) // 6


def default_plan(session: Session) -> Plan:
    plan = session.scalar(select(Plan).where(Plan.is_default.is_(True)))
    if plan is None:  # the migration creates one, and the default can't be removed
        raise Conflict("No default plan. Create one under Plans.")
    return plan


# --- Drafting --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftLine:
    kind: SubscriptionLineKind
    description: str
    quantity: int
    unit_kobo: int

    @property
    def amount_kobo(self) -> int:
        return self.quantity * self.unit_kobo


@dataclass
class DraftBill:
    school_id: uuid.UUID
    school_name: str
    plan: Plan
    session_name: str
    term_number: int
    pupils: int
    lines: list[DraftLine]

    @property
    def total_kobo(self) -> int:
        return sum(ln.amount_kobo for ln in self.lines)


@dataclass
class BillingRun:
    dry_run: bool
    bills: list[DraftBill] = field(default_factory=list)
    already_billed: int = 0
    no_current_term: list[str] = field(default_factory=list)  # school names
    cutoff: datetime = field(default_factory=lambda: datetime.now(UTC))  # instalment plans made up to here

    @property
    def total_kobo(self) -> int:
        return sum(b.total_kobo for b in self.bills)


# The per-plan fee started here: plans made before it are never charged
PLAN_FEE_FROM = datetime(2026, 10, 25, tzinfo=UTC)


def _unbilled_plans(cutoff: datetime) -> Any:
    """Instalment plans made up to `cutoff`, not yet on a WardPay bill and not cancelled."""
    return (
        PaymentPlan.subscription_invoice_id.is_(None),
        PaymentPlan.cancelled_at.is_(None),
        PaymentPlan.created_at >= PLAN_FEE_FROM,
        PaymentPlan.created_at <= cutoff,
    )


def draft_bills(session: Session, cutoff: datetime | None = None) -> BillingRun:
    """This term's bills for every active school, without saving anything. Six queries."""
    run = BillingRun(dry_run=True)
    plans = {p.id: p for p in session.scalars(select(Plan))}
    fallback = next((p for p in plans.values() if p.is_default), None)
    if fallback is None:
        raise Conflict("No default plan. Create one under Plans.")
    schools = session.execute(
        select(School.id, School.name, School.plan_id).where(School.status == SchoolStatus.ACTIVE).order_by(School.name)
    ).all()
    current = {
        school_id: (term_id, session_name, number)
        for school_id, term_id, session_name, number in session.execute(
            select(Term.school_id, Term.id, AcademicSession.name, Term.number)
            .join(AcademicSession, AcademicSession.id == Term.session_id)
            .where(Term.is_current.is_(True))
        ).all()
    }
    pupils = dict(
        session.execute(
            select(Enrollment.school_id, func.count())
            .join(Student, Student.id == Enrollment.student_id)
            .where(
                Enrollment.term_id.in_([t for t, _, _ in current.values()]),
                Student.status == StudentStatus.ACTIVE,
            )
            .group_by(Enrollment.school_id)
        ).all()
    )
    # What each school has been billed before: which terms, and whether the set-up fee
    history = session.execute(
        select(
            SubscriptionInvoice.school_id,
            SubscriptionInvoice.session_name,
            SubscriptionInvoice.term_number,
            func.bool_or(SubscriptionLine.kind == SubscriptionLineKind.SETUP),
        )
        .outerjoin(SubscriptionLine, SubscriptionLine.invoice_id == SubscriptionInvoice.id)
        .where(SubscriptionInvoice.voided_at.is_(None))
        .group_by(SubscriptionInvoice.school_id, SubscriptionInvoice.session_name, SubscriptionInvoice.term_number)
    ).all()
    billed_terms = {(s, name, n) for s, name, n, _ in history}
    # Instalment plans made since each school's last bill: the per-plan fee (owner, 2026-10-25)
    run.cutoff = cutoff or datetime.now(UTC)
    plans_made = dict(
        session.execute(
            select(PaymentPlan.school_id, func.count())
            .where(*_unbilled_plans(run.cutoff))
            .group_by(PaymentPlan.school_id)
        ).all()
    )
    setup_paid = {s for s, _, _, setup in history if setup}

    for school_id, name, plan_id in schools:
        if school_id not in current:
            run.no_current_term.append(name)
            continue
        _, session_name, number = current[school_id]
        if (school_id, session_name, number) in billed_terms:
            run.already_billed += 1
            continue
        plan = plans.get(plan_id) if plan_id else fallback
        plan = plan or fallback
        count = int(pupils.get(school_id, 0))
        lines: list[DraftLine] = []
        if school_id not in setup_paid and plan.setup_fee_kobo:
            lines.append(DraftLine(SubscriptionLineKind.SETUP, "Set-up fee (once)", 1, plan.setup_fee_kobo))
        if count and plan.per_pupil_year_kobo:
            lines.append(
                DraftLine(
                    SubscriptionLineKind.PUPILS,
                    f"{term_label(session_name, number)}: pupils enrolled",
                    count,
                    termly(plan.per_pupil_year_kobo),
                )
            )
        made = int(plans_made.get(school_id, 0))
        if made and plan.instalment_plan_fee_kobo:
            lines.append(
                DraftLine(
                    SubscriptionLineKind.INSTALMENT_PLANS,
                    "Instalment plans made since the last bill",
                    made,
                    plan.instalment_plan_fee_kobo,
                )
            )
        if lines:
            run.bills.append(DraftBill(school_id, name, plan, session_name, number, count, lines))
    return run


def generate(session: Session, admin: Principal, *, dry_run: bool, today: date | None = None) -> BillingRun:
    """Bill every active school for its current term (dry_run: just show what would be billed)."""
    if not dry_run:  # two admins billing at once: the second waits, then finds every school billed
        locks.hold(session, "subscription-billing")
    run = draft_bills(session)
    run.dry_run = dry_run
    if dry_run or not run.bills:
        session.rollback()
        return run
    today = today or datetime.now(LAGOS).date()
    due = today + timedelta(days=get_settings().subscription_due_days)
    numbers = reserve_platform(session, SEQUENCE, len(run.bills))
    invoices: list[SubscriptionInvoice] = []
    lines: list[SubscriptionLine] = []
    for bill, number in zip(run.bills, numbers, strict=True):
        invoice = SubscriptionInvoice(
            id=uuid.uuid4(),
            school_id=bill.school_id,
            number=number,
            plan_id=bill.plan.id,
            session_name=bill.session_name,
            term_number=bill.term_number,
            pupils=bill.pupils,
            due_date=due,
            created_by=admin.user.id,
        )
        invoices.append(invoice)
        lines.extend(
            SubscriptionLine(
                school_id=bill.school_id,
                invoice_id=invoice.id,
                kind=ln.kind,
                description=ln.description,
                quantity=ln.quantity,
                unit_kobo=ln.unit_kobo,
                amount_kobo=ln.amount_kobo,
            )
            for ln in bill.lines
        )
    session.add_all(invoices)
    session.flush()
    session.add_all(lines)
    session.flush()
    # The plans just charged are marked with the bill that charged them, so they're never charged twice
    charged = [
        i.id
        for i, b in zip(invoices, run.bills, strict=True)
        if any(ln.kind == SubscriptionLineKind.INSTALMENT_PLANS for ln in b.lines)
    ]
    if charged:
        session.execute(
            update(PaymentPlan)
            .where(
                PaymentPlan.school_id == SubscriptionInvoice.school_id,
                SubscriptionInvoice.id.in_(charged),
                *_unbilled_plans(run.cutoff),
            )
            .values(subscription_invoice_id=SubscriptionInvoice.id)
        )
    for invoice in invoices:  # each school's proprietor is emailed their bill once this commits
        outbox.enqueue(session, EMAIL_TASK, {"invoice_id": str(invoice.id)})
    record(
        session,
        AuditAction.SUBSCRIPTIONS_GENERATED,
        school_id=None,
        actor=admin.actor,
        meta=admin.meta,
        changes={
            "invoices": len(invoices),
            "total_kobo": run.total_kobo,
            "first_number": numbers.start,
            "last_number": numbers.stop - 1,
        },
    )
    session.commit()
    return run


# --- Reading ---------------------------------------------------------------------------------------


def _amounts() -> tuple[Subquery, Subquery]:
    totals = (
        select(SubscriptionLine.invoice_id, func.sum(SubscriptionLine.amount_kobo).label("total"))
        .group_by(SubscriptionLine.invoice_id)
        .subquery("sub_totals")
    )
    paid = (
        select(SubscriptionPayment.invoice_id, func.sum(SubscriptionPayment.amount_kobo).label("paid"))
        .group_by(SubscriptionPayment.invoice_id)
        .subquery("sub_paid")
    )
    return totals, paid


@dataclass(frozen=True)
class InvoiceRow:
    id: uuid.UUID
    number: int
    school_id: uuid.UUID
    school_name: str
    plan_name: str
    period: str
    pupils: int
    issued_at: datetime
    due_date: date
    total_kobo: int
    paid_kobo: int
    voided: bool

    @property
    def balance_kobo(self) -> int:
        return 0 if self.voided else max(self.total_kobo - self.paid_kobo, 0)

    @property
    def status(self) -> Literal["unpaid", "overdue", "paid", "void"]:
        if self.voided:
            return "void"
        if self.balance_kobo == 0:
            return "paid"
        return "overdue" if self.due_date < datetime.now(LAGOS).date() else "unpaid"


def invoices_query(*, school_id: uuid.UUID | None = None, status: str | None = None) -> Select[*tuple[Any, ...]]:
    totals, paid = _amounts()
    total = func.coalesce(totals.c.total, 0)
    paid_col = func.coalesce(paid.c.paid, 0)
    stmt = (
        select(
            SubscriptionInvoice.id,
            SubscriptionInvoice.number,
            SubscriptionInvoice.school_id,
            School.name,
            Plan.name,
            SubscriptionInvoice.session_name,
            SubscriptionInvoice.term_number,
            SubscriptionInvoice.pupils,
            SubscriptionInvoice.issued_at,
            SubscriptionInvoice.due_date,
            total,
            paid_col,
            SubscriptionInvoice.voided_at.is_not(None),
        )
        .join(School, School.id == SubscriptionInvoice.school_id)
        .join(Plan, Plan.id == SubscriptionInvoice.plan_id)
        .outerjoin(totals, totals.c.invoice_id == SubscriptionInvoice.id)
        .outerjoin(paid, paid.c.invoice_id == SubscriptionInvoice.id)
        .order_by(SubscriptionInvoice.number.desc())
    )
    if school_id:
        stmt = stmt.where(SubscriptionInvoice.school_id == school_id)
    live = SubscriptionInvoice.voided_at.is_(None)
    today = datetime.now(LAGOS).date()
    if status == "void":
        stmt = stmt.where(SubscriptionInvoice.voided_at.is_not(None))
    elif status == "paid":
        stmt = stmt.where(live, paid_col >= total)
    elif status == "unpaid":
        stmt = stmt.where(live, paid_col < total)
    elif status == "overdue":
        stmt = stmt.where(live, paid_col < total, SubscriptionInvoice.due_date < today)
    return stmt


def to_row(r: Any) -> InvoiceRow:
    (iid, number, school_id, school, plan, session_name, term, pupils, issued, due, total, paid, voided) = r
    return InvoiceRow(
        id=iid,
        number=int(number),
        school_id=school_id,
        school_name=school,
        plan_name=plan,
        period=term_label(session_name, int(term)),
        pupils=int(pupils),
        issued_at=issued,
        due_date=due,
        total_kobo=int(total),
        paid_kobo=int(paid),
        voided=bool(voided),
    )


def invoice_detail(
    session: Session, invoice_id: uuid.UUID
) -> tuple[InvoiceRow, list[SubscriptionLine], list[SubscriptionPayment]]:
    row = session.execute(invoices_query().where(SubscriptionInvoice.id == invoice_id)).first()
    if row is None:
        raise NotFound("Invoice not found.")
    lines = list(session.scalars(select(SubscriptionLine).where(SubscriptionLine.invoice_id == invoice_id)))
    payments = list(
        session.scalars(
            select(SubscriptionPayment)
            .where(SubscriptionPayment.invoice_id == invoice_id)
            .order_by(SubscriptionPayment.received_on)
        )
    )
    return to_row(row), lines, payments


def outstanding_by_school(session: Session, school_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """What each school still owes the platform, in one query."""
    if not school_ids:
        return {}
    totals, paid = _amounts()
    balance = func.coalesce(totals.c.total, 0) - func.coalesce(paid.c.paid, 0)
    rows = session.execute(
        select(SubscriptionInvoice.school_id, func.sum(case((balance > 0, balance), else_=0)))
        .outerjoin(totals, totals.c.invoice_id == SubscriptionInvoice.id)
        .outerjoin(paid, paid.c.invoice_id == SubscriptionInvoice.id)
        .where(SubscriptionInvoice.school_id.in_(school_ids), SubscriptionInvoice.voided_at.is_(None))
        .group_by(SubscriptionInvoice.school_id)
    ).all()
    return {sid: int(amount) for sid, amount in rows}


# --- Payments and voiding --------------------------------------------------------------------------


def record_payment(
    session: Session, admin: Principal, invoice_id: uuid.UUID, *, amount_kobo: int, received_on: date, reference: str
) -> InvoiceRow:
    """A school's bank transfer to the platform, recorded by a platform admin. Can't exceed what's owed."""
    invoice = session.get(SubscriptionInvoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise NotFound("Invoice not found.")
    if invoice.voided_at is not None:
        raise Conflict("This invoice was voided.")
    row, _, _ = invoice_detail(session, invoice_id)
    if amount_kobo <= 0:
        raise DomainError("Enter the amount received.", field="amount_kobo")
    if amount_kobo > row.balance_kobo:
        raise DomainError(f"Only {format_naira(row.balance_kobo)} is owed on this invoice.", field="amount_kobo")
    if received_on > datetime.now(LAGOS).date():
        raise DomainError("That date is in the future.", field="received_on")
    session.add(
        SubscriptionPayment(
            school_id=invoice.school_id,
            invoice_id=invoice.id,
            amount_kobo=amount_kobo,
            received_on=received_on,
            reference=reference.strip()[:100],
            recorded_by=admin.user.id,
        )
    )
    for school in (None, invoice.school_id):
        record(
            session,
            AuditAction.SUBSCRIPTION_PAYMENT_RECORDED,
            school_id=school,
            actor=admin.actor,
            meta=admin.meta,
            target=invoice,
            changes={"number": invoice.number, "amount_kobo": amount_kobo, "reference": reference.strip()[:100]},
        )
    session.commit()
    return invoice_detail(session, invoice_id)[0]


def void(session: Session, admin: Principal, invoice_id: uuid.UUID, reason: str) -> InvoiceRow:
    """Cancel a bill made in error (wrong plan or pupil count). Only while nothing has been paid on it."""
    invoice = session.get(SubscriptionInvoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise NotFound("Invoice not found.")
    if invoice.voided_at is not None:
        raise Conflict("This invoice is already void.")
    paid = session.scalar(select(func.count()).where(SubscriptionPayment.invoice_id == invoice_id))
    if paid:
        raise Conflict("Payments have been recorded on this invoice, so it can't be voided.")
    invoice.voided_at, invoice.void_reason = datetime.now(UTC), reason.strip()[:300]
    # Plans it charged for go back to waiting for the next bill
    session.execute(
        update(PaymentPlan)
        .where(PaymentPlan.subscription_invoice_id == invoice_id)
        .values(subscription_invoice_id=None)
    )
    for school in (None, invoice.school_id):
        record(
            session,
            AuditAction.SUBSCRIPTION_INVOICE_VOIDED,
            school_id=school,
            actor=admin.actor,
            meta=admin.meta,
            target=invoice,
            changes={"number": invoice.number, "reason": reason.strip()[:300]},
        )
    session.commit()
    return invoice_detail(session, invoice_id)[0]


# --- Adjusting a bill ------------------------------------------------------------------------------


def _live_invoice(session: Session, invoice_id: uuid.UUID) -> SubscriptionInvoice:
    invoice = session.get(SubscriptionInvoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise NotFound("Invoice not found.")
    if invoice.voided_at is not None:
        raise Conflict("This invoice was voided.")
    return invoice


def add_discount(
    session: Session, admin: Principal, invoice_id: uuid.UUID, *, amount_kobo: int, reason: str
) -> InvoiceRow:
    """
    Take money off a bill (e.g. the set-up fee waived for an early school): a new negative
    "discount" line, since lines are never edited. At most what's still owed, so a bill
    never ends up owing the school money.
    """
    invoice = _live_invoice(session, invoice_id)
    row, _, _ = invoice_detail(session, invoice_id)
    if amount_kobo <= 0:
        raise DomainError("Enter the amount to take off.", field="amount_kobo")
    if amount_kobo > row.balance_kobo:
        raise DomainError(f"Only {format_naira(row.balance_kobo)} is still owed on this invoice.", field="amount_kobo")
    reason = reason.strip()[:150]
    session.add(
        SubscriptionLine(
            school_id=invoice.school_id,
            invoice_id=invoice.id,
            kind=SubscriptionLineKind.DISCOUNT,
            description=f"Discount: {reason}",
            quantity=1,
            unit_kobo=-amount_kobo,
            amount_kobo=-amount_kobo,
        )
    )
    for school in (None, invoice.school_id):
        record(
            session,
            AuditAction.SUBSCRIPTION_DISCOUNTED,
            school_id=school,
            actor=admin.actor,
            meta=admin.meta,
            target=invoice,
            changes={"number": invoice.number, "amount_kobo": amount_kobo, "reason": reason},
        )
    session.commit()
    return invoice_detail(session, invoice_id)[0]


def change_due_date(
    session: Session, admin: Principal, invoice_id: uuid.UUID, *, due_date: date, reason: str
) -> InvoiceRow:
    """Give a school more (or less) time to pay. Not before the bill was issued."""
    invoice = _live_invoice(session, invoice_id)
    if due_date < invoice.issued_at.astimezone(LAGOS).date():
        raise DomainError("The due date can't be before the bill was issued.", field="due_date")
    before = invoice.due_date
    if due_date == before:
        raise DomainError("That's already the due date.", field="due_date")
    invoice.due_date = due_date
    for school in (None, invoice.school_id):
        record(
            session,
            AuditAction.SUBSCRIPTION_DUE_DATE_CHANGED,
            school_id=school,
            actor=admin.actor,
            meta=admin.meta,
            target=invoice,
            changes={"number": invoice.number, "before": before, "after": due_date, "reason": reason.strip()[:300]},
        )
    session.commit()
    return invoice_detail(session, invoice_id)[0]


# --- The school's own view -------------------------------------------------------------------------


@dataclass(frozen=True)
class SchoolPlanView:
    plan: Plan
    pupils_this_term: int
    next_term_estimate_kobo: int  # the pupil charge for a term at today's numbers


def school_plan(session: Session, school_id: uuid.UUID) -> SchoolPlanView:
    """The school's plan and what a term costs at its current pupil numbers. Session scoped to the school."""
    plan_id = session.scalar(select(School.plan_id).where(School.id == school_id))
    plan = (session.get(Plan, plan_id) if plan_id else None) or default_plan(session)
    pupils = (
        session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .join(Term, and_(Term.id == Enrollment.term_id, Term.is_current.is_(True)))
            .join(Student, Student.id == Enrollment.student_id)
            .where(Student.status == StudentStatus.ACTIVE)
        )
        or 0
    )
    return SchoolPlanView(plan, int(pupils), int(pupils) * termly(plan.per_pupil_year_kobo))
