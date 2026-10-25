"""
Payment plans: paying what's left on a bill in instalments.

A plan covers what was still owed when it was agreed (`covered_kobo`), split
into 2 to 12 dated instalments that add up to exactly that. Money is applied
to bills as always (oldest debt first); a plan only changes:

- **when the bill counts as late**: by its next unpaid instalment, not the
  bill's due date (reports, debtors, aging);
- **what reminders ask for**: the next instalment, around its date.

Progress is worked out from the ledger: what's been paid on the bill since the
plan started (`paid now - paid_at_start`) covers the instalments in order. A
plan is never edited; it's cancelled (with a reason) and a new one made.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Literal

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CTE

from app.core.clock import lagos_today
from app.core.errors import Conflict, DomainError
from app.core.http import RequestMeta
from app.core.money import format_naira
from app.core.repo import get_or_404
from app.modules.accounts.deps import StaffContext
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record

from .ledger import paid_subquery
from .models import Invoice, PaymentPlan, PlanInstalment

MIN_INSTALMENTS, MAX_INSTALMENTS = 2, 12
InstalmentStatus = Literal["paid", "part_paid", "overdue", "due_today", "upcoming"]


# --- Views -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalmentView:
    number: int
    due_date: date
    amount_kobo: int
    paid_kobo: int  # how much of this instalment is covered
    status: InstalmentStatus


@dataclass(frozen=True)
class PlanView:
    id: uuid.UUID
    invoice_id: uuid.UUID
    note: str
    created_at: datetime
    covered_kobo: int
    paid_on_plan_kobo: int
    behind_kobo: int  # instalments due by today but not yet paid
    next_due: date | None
    next_amount_kobo: int  # what's still needed for the next instalment
    next_number: int | None
    instalments: list[InstalmentView]

    @property
    def complete(self) -> bool:
        return self.next_due is None


def _view(plan: PaymentPlan, instalments: list[PlanInstalment], paid_now: int, today: date) -> PlanView:
    paid_on_plan = max(paid_now - plan.paid_at_start_kobo, 0)
    left = paid_on_plan
    views: list[InstalmentView] = []
    behind = 0
    next_due, next_amount, next_number = None, 0, None
    for inst in sorted(instalments, key=lambda i: i.number):
        covered = min(left, inst.amount_kobo)
        left -= covered
        if covered == inst.amount_kobo:
            status: InstalmentStatus = "paid"
        elif inst.due_date < today:
            status = "overdue"
        elif inst.due_date == today:
            status = "due_today"
        else:
            status = "part_paid" if covered else "upcoming"
        if covered < inst.amount_kobo:
            if inst.due_date <= today:
                behind += inst.amount_kobo - covered
            if next_due is None:
                next_due, next_amount, next_number = inst.due_date, inst.amount_kobo - covered, inst.number
        views.append(InstalmentView(inst.number, inst.due_date, inst.amount_kobo, covered, status))
    return PlanView(
        id=plan.id,
        invoice_id=plan.invoice_id,
        note=plan.note,
        created_at=plan.created_at,
        covered_kobo=plan.covered_kobo,
        paid_on_plan_kobo=paid_on_plan,
        behind_kobo=behind,
        next_due=next_due,
        next_amount_kobo=next_amount,
        next_number=next_number,
        instalments=views,
    )


def plan_views(session: Session, invoice_ids: list[uuid.UUID], today: date | None = None) -> dict[uuid.UUID, PlanView]:
    """The live plan (if any) for each bill, with progress. Three queries for any number of bills."""
    if not invoice_ids:
        return {}
    today = today or lagos_today()
    plans = list(
        session.scalars(
            select(PaymentPlan).where(PaymentPlan.invoice_id.in_(invoice_ids), PaymentPlan.cancelled_at.is_(None))
        )
    )
    if not plans:
        return {}
    by_plan: dict[uuid.UUID, list[PlanInstalment]] = {p.id: [] for p in plans}
    for inst in session.scalars(select(PlanInstalment).where(PlanInstalment.plan_id.in_(list(by_plan)))):
        by_plan[inst.plan_id].append(inst)
    paid = paid_subquery()
    paid_now = dict(
        session.execute(
            select(paid.c.invoice_id, paid.c.paid_kobo).where(paid.c.invoice_id.in_([p.invoice_id for p in plans]))
        ).all()
    )
    return {p.invoice_id: _view(p, by_plan[p.id], int(paid_now.get(p.invoice_id, 0)), today) for p in plans}


def progress_cte(today: date) -> CTE:
    """
    invoice_id -> next_due, next_amount_kobo, behind_kobo for every live plan: the same
    rules as `_view`, in SQL, for reports and reminders. Computed once (materialized).
    """
    paid = paid_subquery()
    paid_on_plan = func.greatest(func.coalesce(paid.c.paid_kobo, 0) - PaymentPlan.paid_at_start_kobo, 0)
    cumulative = func.sum(PlanInstalment.amount_kobo).over(
        partition_by=PlanInstalment.plan_id, order_by=PlanInstalment.number
    )
    steps = (
        select(
            PaymentPlan.invoice_id.label("invoice_id"),
            PlanInstalment.due_date.label("due_date"),
            PlanInstalment.amount_kobo.label("amount"),
            cumulative.label("through"),
            paid_on_plan.label("paid_on_plan"),
        )
        .join(PlanInstalment, PlanInstalment.plan_id == PaymentPlan.id)
        .outerjoin(paid, paid.c.invoice_id == PaymentPlan.invoice_id)
        .where(PaymentPlan.cancelled_at.is_(None))
        .subquery("plan_steps")
    )
    uncovered = steps.c.through > steps.c.paid_on_plan
    return (
        select(
            steps.c.invoice_id,
            func.min(case((uncovered, steps.c.due_date))).label("next_due"),
            func.min(case((uncovered, steps.c.through - steps.c.paid_on_plan))).label("next_amount_kobo"),
            func.greatest(
                func.sum(case((steps.c.due_date <= today, steps.c.amount), else_=0)) - func.max(steps.c.paid_on_plan), 0
            ).label("behind_kobo"),
        )
        .group_by(steps.c.invoice_id)
        .cte("plan_progress")
        .prefix_with("MATERIALIZED")
    )


# --- Creating and cancelling --------------------------------------------------------------------------


def create_plan(
    session: Session,
    ctx: StaffContext,
    invoice_id: uuid.UUID,
    instalments: list[tuple[date, int]],
    note: str = "",
) -> PlanView:
    """A plan agreed by staff (any dates and amounts that add up to what's owed)."""
    invoice = get_or_404(session, Invoice, invoice_id, label="Invoice", lock=True)
    add_plan(
        session,
        invoice,
        instalments,
        note=note,
        created_by=ctx.user.id,
        actor=ctx.actor,
        meta=ctx.meta,
        school_id=ctx.school_id,
    )
    session.commit()
    return plan_views(session, [invoice.id])[invoice.id]


def add_plan(
    session: Session,
    invoice: Invoice,
    instalments: list[tuple[date, int]],
    *,
    note: str,
    created_by: uuid.UUID | None,
    actor: Actor,
    meta: RequestMeta | None,
    school_id: uuid.UUID,
    template_id: uuid.UUID | None = None,
) -> PaymentPlan:
    """Check and save a plan on a locked bill, staff's or a parent's choice. The caller commits."""
    if invoice.voided_at is not None:
        raise Conflict("This bill was voided.")
    if session.scalar(
        select(PaymentPlan.id).where(PaymentPlan.invoice_id == invoice.id, PaymentPlan.cancelled_at.is_(None))
    ):
        raise Conflict("This bill already has a payment plan. Cancel it first to make a new one.")
    from .invoicing import amounts_for  # invoicing imports this module's sibling helpers

    amounts = amounts_for(session, [invoice.id])[invoice.id]
    owed = amounts.balance_kobo
    if owed <= 0:
        raise Conflict("Nothing is owed on this bill.")
    if not MIN_INSTALMENTS <= len(instalments) <= MAX_INSTALMENTS:
        raise DomainError(f"Use {MIN_INSTALMENTS} to {MAX_INSTALMENTS} instalments.", field="instalments")
    dates = [d for d, _ in instalments]
    if any(b <= a for a, b in pairwise(dates)):
        raise DomainError("Each instalment must fall after the one before.", field="instalments")
    if dates[0] < lagos_today():
        raise DomainError("The first instalment can't be in the past.", field="instalments")
    if any(amount <= 0 for _, amount in instalments):
        raise DomainError("Every instalment must be more than ₦0.", field="instalments")
    total = sum(amount for _, amount in instalments)
    if total != owed:
        raise DomainError(
            f"The instalments add up to {format_naira(total)}, but {format_naira(owed)} is owed.", field="instalments"
        )
    plan = PaymentPlan(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        student_id=invoice.student_id,
        paid_at_start_kobo=amounts.paid_kobo,
        covered_kobo=owed,
        note=note.strip()[:300],
        created_by=created_by,
        template_id=template_id,
    )
    session.add(plan)
    session.flush()
    session.add_all(
        PlanInstalment(plan_id=plan.id, number=n, due_date=d, amount_kobo=amount)
        for n, (d, amount) in enumerate(instalments, start=1)
    )
    record(
        session,
        AuditAction.PAYMENT_PLAN_CREATED,
        school_id=school_id,
        actor=actor,
        meta=meta,
        target=invoice,
        changes={
            "number": invoice.number,
            "covered_kobo": owed,
            "instalments": [{"due": d.isoformat(), "amount_kobo": a} for d, a in instalments],
            **({"school_plan": str(template_id)} if template_id else {}),
        },
    )
    return plan


def cancel_plan(session: Session, ctx: StaffContext, invoice_id: uuid.UUID, reason: str) -> None:
    invoice = get_or_404(session, Invoice, invoice_id, label="Invoice")
    changed = session.execute(
        update(PaymentPlan)
        .where(PaymentPlan.invoice_id == invoice.id, PaymentPlan.cancelled_at.is_(None))
        .values(cancelled_at=datetime.now(UTC), cancel_reason=reason.strip()[:300])
        .returning(PaymentPlan.id)
    ).all()
    if not changed:
        raise Conflict("This bill has no payment plan.")
    record(
        session,
        AuditAction.PAYMENT_PLAN_CANCELLED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=invoice,
        changes={"number": invoice.number, "reason": reason.strip()[:300]},
    )
    session.commit()


def cancel_for_voided(session: Session, invoice_id: uuid.UUID, reason: str) -> None:
    """A voided bill's plan ends with it (in the caller's transaction)."""
    session.execute(
        update(PaymentPlan)
        .where(PaymentPlan.invoice_id == invoice_id, PaymentPlan.cancelled_at.is_(None))
        .values(cancelled_at=datetime.now(UTC), cancel_reason=f"Bill voided: {reason}"[:300])
    )


def plan_phrase(view: PlanView) -> str:
    """For reminders: ' (instalment 2 of 3, due 15 Nov 2026)'."""
    if view.next_due is None or view.next_number is None:
        return ""
    late = "was due" if view.next_due < lagos_today() else "due"
    when = f"{view.next_due.day} {view.next_due:%b %Y}"
    return f" (instalment {view.next_number} of {len(view.instalments)}, {late} {when})"
