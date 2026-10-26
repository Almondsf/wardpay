"""
Invoices: generating a term's invoices in one go, adjusting and voiding them,
opening balances, and reading balances from the ledger.

How much is owed is always computed: an invoice's total is the sum of its
lines, and what's been paid comes from payment allocations (`paid_subquery`,
filled in by the payments module). Nothing stores a balance that could drift.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import Subquery

from app.core import locks
from app.core.clock import LAGOS
from app.core.errors import Conflict, DomainError, NotFound
from app.core.repo import get_or_404
from app.core.sequences import reserve
from app.modules.accounting import periods
from app.modules.accounts.deps import StaffContext
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.schools.models import ClassLevel, Term
from app.modules.students.models import Enrollment, GuardianStudent, Student, StudentStatus

from . import ledger, plans
from .models import (
    Discount,
    DiscountKind,
    DiscountRule,
    FeeItem,
    FeeSchedule,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    LineKind,
    StudentDiscount,
    StudentFeeChoice,
    StudentFeeExemption,
)
from .setup_service import MAX_AMOUNT_KOBO

INVOICE_SEQUENCE = "invoice"


# --- Ledger reads ----------------------------------------------------------------------------------


def totals_subquery(within: Any = None) -> Subquery:
    """
    invoice_id -> total_kobo (sum of its lines). `within` (invoice ids, or a select of them) limits it to
    those bills: Postgres can't push an outer filter into the grouping, so without it every bill in the
    school is totalled. Per-pupil paths (a payment arriving) must pass it (load test, 2026-10-25).
    """
    stmt = select(InvoiceLine.invoice_id, func.sum(InvoiceLine.amount_kobo).label("total_kobo"))
    if within is not None:
        stmt = stmt.where(InvoiceLine.invoice_id.in_(within))
    return stmt.group_by(InvoiceLine.invoice_id).subquery("invoice_totals")


def paid_subquery(within: Any = None) -> Subquery:
    """invoice_id -> paid_kobo (see ledger.py); `within` as for totals_subquery."""
    return ledger.paid_subquery(within)


@dataclass(frozen=True)
class InvoiceAmounts:
    total_kobo: int
    paid_kobo: int

    @property
    def balance_kobo(self) -> int:
        return self.total_kobo - self.paid_kobo


def status_of(invoice: Invoice, amounts: InvoiceAmounts) -> str:
    if invoice.voided_at is not None:
        return "void"
    if amounts.balance_kobo <= 0:
        return "paid"
    return "part_paid" if amounts.paid_kobo > 0 else "unpaid"


def status_condition(status: str, total: ColumnElement[int], paid: ColumnElement[int]) -> ColumnElement[bool]:
    live = Invoice.voided_at.is_(None)
    return {
        "void": Invoice.voided_at.is_not(None),
        "paid": and_(live, total - paid <= 0),
        "part_paid": and_(live, paid > 0, total - paid > 0),
        "unpaid": and_(live, paid <= 0, total > 0),
        "outstanding": and_(live, total - paid > 0),
    }[status]


def invoices_query(
    *,
    term_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    class_level_id: uuid.UUID | None = None,
    status: str | None = None,
    search: str | None = None,
) -> Select[Invoice, Student, int, int]:
    """Invoices with their student, total and paid amounts, in one query."""
    totals, paid = totals_subquery(), paid_subquery()
    total_col = func.coalesce(totals.c.total_kobo, 0)
    paid_col = func.coalesce(paid.c.paid_kobo, 0)
    stmt = (
        select(Invoice, Student, total_col.label("total_kobo"), paid_col.label("paid_kobo"))
        .join(Student, Student.id == Invoice.student_id)
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .order_by(Invoice.number.desc())
    )
    if term_id:
        stmt = stmt.where(Invoice.term_id == term_id)
    if student_id:
        stmt = stmt.where(Invoice.student_id == student_id)
    if class_level_id:
        stmt = stmt.where(Invoice.class_level_id == class_level_id)
    if status:
        stmt = stmt.where(status_condition(status, total_col, paid_col))
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        number_match = Invoice.number == int(search) if search.strip().isdigit() else literal(False)
        stmt = stmt.where(
            or_(
                Student.first_name.ilike(pattern),
                Student.last_name.ilike(pattern),
                Student.admission_number.ilike(pattern),
                number_match,
            )
        )
    return stmt


def amounts_for(session: Session, invoice_ids: list[uuid.UUID]) -> dict[uuid.UUID, InvoiceAmounts]:
    if not invoice_ids:
        return {}
    totals, paid = totals_subquery(invoice_ids), paid_subquery(invoice_ids)
    rows = session.execute(
        select(Invoice.id, func.coalesce(totals.c.total_kobo, 0), func.coalesce(paid.c.paid_kobo, 0))
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(Invoice.id.in_(invoice_ids))
    ).all()
    return {invoice_id: InvoiceAmounts(int(total), int(paid_amount)) for invoice_id, total, paid_amount in rows}


def invoice_with_lines(session: Session, invoice_id: uuid.UUID) -> tuple[Invoice, Student, InvoiceAmounts]:
    invoice = session.scalars(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
        .execution_options(populate_existing=True)
    ).one_or_none()
    if invoice is None:
        raise NotFound("Invoice not found.")
    student = get_or_404(session, Student, invoice.student_id, label="Student")
    return invoice, student, amounts_for(session, [invoice.id])[invoice.id]


@dataclass(frozen=True)
class AccountSummary:
    """A student's position across all their live invoices."""

    billed_kobo: int
    paid_kobo: int
    outstanding_kobo: int  # sum of unpaid balances (overpayments sit in credit, not here)


def account_summaries(session: Session, student_ids: list[uuid.UUID]) -> dict[uuid.UUID, AccountSummary]:
    """Billed, paid and outstanding per student, for any number of students, in one query."""
    if not student_ids:
        return {}
    theirs = select(Invoice.id).where(Invoice.student_id.in_(student_ids))
    totals, paid = totals_subquery(theirs), paid_subquery(theirs)
    total_col, paid_col = func.coalesce(totals.c.total_kobo, 0), func.coalesce(paid.c.paid_kobo, 0)
    balance = total_col - paid_col
    rows = session.execute(
        select(
            Invoice.student_id, func.sum(total_col), func.sum(paid_col), func.sum(case((balance > 0, balance), else_=0))
        )
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(Invoice.student_id.in_(student_ids), Invoice.voided_at.is_(None))
        .group_by(Invoice.student_id)
    ).all()
    found = {sid: AccountSummary(int(b), int(p), int(o)) for sid, b, p, o in rows}
    return {sid: found.get(sid, AccountSummary(0, 0, 0)) for sid in student_ids}


# --- Generating a term's invoices -----------------------------------------------------------------


@dataclass
class DraftLine:
    kind: LineKind
    description: str
    amount_kobo: int
    fee_item_id: uuid.UUID | None = None
    discount_id: uuid.UUID | None = None


@dataclass
class DraftInvoice:
    student_id: uuid.UUID
    admission_number: str
    student_name: str
    class_level_id: uuid.UUID
    class_name: str
    lines: list[DraftLine]

    @property
    def total_kobo(self) -> int:
        return sum(line.amount_kobo for line in self.lines)


@dataclass
class ClassTotal:
    class_level_id: uuid.UUID
    class_name: str
    invoices: int = 0
    total_kobo: int = 0


@dataclass
class GenerationReport:
    dry_run: bool
    invoices: int = 0
    total_kobo: int = 0
    discounts_kobo: int = 0
    already_invoiced: int = 0
    no_fees_set: list[str] = field(default_factory=list)  # class names with students but no schedule
    by_class: list[ClassTotal] = field(default_factory=list)
    sample: list[DraftInvoice] = field(default_factory=list)
    notices_queued: int = 0


def _discount_amount(discount: Discount, lines: list[DraftLine]) -> int:
    fee_lines = [ln for ln in lines if ln.kind == LineKind.FEE]
    base = sum(
        ln.amount_kobo for ln in fee_lines if discount.fee_item_id is None or ln.fee_item_id == discount.fee_item_id
    )
    if base <= 0:
        return 0
    if discount.kind == DiscountKind.PERCENT:
        return (base * discount.value + 5_000) // 10_000  # round half up to the kobo
    return min(discount.value, base)


def _families(student_ids: set[uuid.UUID], links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> dict[uuid.UUID, uuid.UUID]:
    """Group students who share any guardian (union-find). Returns student -> family representative."""
    parent = {sid: sid for sid in student_ids}

    def find(x: uuid.UUID) -> uuid.UUID:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_guardian: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for guardian_id, student_id in links:
        if student_id in parent:
            by_guardian[guardian_id].append(student_id)
    for siblings in by_guardian.values():
        root = find(siblings[0])
        for other in siblings[1:]:
            parent[find(other)] = root
    return {sid: find(sid) for sid in student_ids}


def draft_term_invoices(
    session: Session, term: Term, class_level_ids: set[uuid.UUID] | None = None
) -> tuple[list[DraftInvoice], GenerationReport]:
    """Work out every invoice the term needs. A fixed number of queries, however many students."""
    report = GenerationReport(dry_run=True)
    enrolled = session.execute(
        select(
            Student.id,
            Student.admission_number,
            Student.first_name,
            Student.last_name,
            ClassLevel.id,
            ClassLevel.name,
            ClassLevel.rank,
        )
        .join(Enrollment, Enrollment.student_id == Student.id)
        .join(ClassLevel, ClassLevel.id == Enrollment.class_level_id)
        .where(Enrollment.term_id == term.id, Student.status == StudentStatus.ACTIVE)
    ).all()
    if class_level_ids:
        enrolled = [row for row in enrolled if row[4] in class_level_ids]
    invoiced = set(
        session.scalars(
            select(Invoice.student_id).where(
                Invoice.term_id == term.id, Invoice.kind == InvoiceKind.TERM, Invoice.voided_at.is_(None)
            )
        )
    )
    schedules = session.execute(
        select(FeeSchedule, FeeItem.name)
        .join(FeeItem, FeeItem.id == FeeSchedule.fee_item_id)
        .where(FeeSchedule.term_id == term.id, FeeItem.is_active.is_(True))
        .order_by(FeeItem.name)
    ).all()
    by_level: dict[uuid.UUID, list[tuple[FeeSchedule, str]]] = defaultdict(list)
    for schedule, item_name in schedules:
        by_level[schedule.class_level_id].append((schedule, item_name))
    chosen = set(
        session.execute(
            select(StudentFeeChoice.student_id, StudentFeeChoice.fee_item_id).where(StudentFeeChoice.term_id == term.id)
        )
    )
    exempt = set(
        session.execute(
            select(StudentFeeExemption.student_id, StudentFeeExemption.fee_item_id).where(
                StudentFeeExemption.term_id == term.id
            )
        )
    )
    discounts = list(session.scalars(select(Discount).where(Discount.is_active.is_(True)).order_by(Discount.name)))
    manual: dict[uuid.UUID, list[Discount]] = defaultdict(list)
    discount_by_id = {d.id: d for d in discounts}
    for student_id, discount_id in session.execute(select(StudentDiscount.student_id, StudentDiscount.discount_id)):
        if discount_id in discount_by_id:
            manual[student_id].append(discount_by_id[discount_id])
    sibling_discounts = [d for d in discounts if d.rule == DiscountRule.SIBLING]

    # Siblings: among everyone enrolled this term (invoiced or not), the eldest (highest class, then
    # lowest admission number) pays in full and the others get the sibling discount
    younger: set[uuid.UUID] = set()
    if sibling_discounts:
        everyone = {
            row[0]: row
            for row in session.execute(
                select(Student.id, Student.admission_number, ClassLevel.rank)
                .join(Enrollment, Enrollment.student_id == Student.id)
                .join(ClassLevel, ClassLevel.id == Enrollment.class_level_id)
                .where(Enrollment.term_id == term.id, Student.status == StudentStatus.ACTIVE)
            ).all()
        }
        links = list(
            session.execute(
                select(GuardianStudent.guardian_id, GuardianStudent.student_id).where(
                    GuardianStudent.student_id.in_(everyone)
                )
            )
        )
        families: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for student_id, root in _families(set(everyone), links).items():
            families[root].append(student_id)
        for members in families.values():
            members.sort(key=lambda sid: (-everyone[sid][2], everyone[sid][1]))
            younger.update(members[1:])

    drafts: list[DraftInvoice] = []
    totals_by_class: dict[uuid.UUID, ClassTotal] = {}
    missing_fees: set[str] = set()
    for student_id, admission, first, last, level_id, level_name, _rank in sorted(enrolled, key=lambda r: (r[6], r[3])):
        if student_id in invoiced:
            report.already_invoiced += 1
            continue
        lines = [
            DraftLine(LineKind.FEE, item_name, schedule.amount_kobo, fee_item_id=schedule.fee_item_id)
            for schedule, item_name in by_level.get(level_id, [])
            if (not schedule.is_optional or (student_id, schedule.fee_item_id) in chosen)
            and (student_id, schedule.fee_item_id) not in exempt
        ]
        if not lines:
            missing_fees.add(level_name)
            continue
        applicable = manual.get(student_id, []) + (sibling_discounts if student_id in younger else [])
        remaining = sum(ln.amount_kobo for ln in lines)
        for discount in applicable:
            amount = min(_discount_amount(discount, lines), remaining)
            if amount > 0:
                lines.append(DraftLine(LineKind.DISCOUNT, discount.name, -amount, discount_id=discount.id))
                remaining -= amount
                report.discounts_kobo += amount
        draft = DraftInvoice(student_id, admission, f"{first} {last}", level_id, level_name, lines)
        drafts.append(draft)
        bucket = totals_by_class.setdefault(level_id, ClassTotal(level_id, level_name))
        bucket.invoices += 1
        bucket.total_kobo += draft.total_kobo

    report.invoices = len(drafts)
    report.total_kobo = sum(d.total_kobo for d in drafts)
    report.no_fees_set = sorted(missing_fees)
    report.by_class = sorted(totals_by_class.values(), key=lambda c: c.class_name)
    report.sample = drafts[:10]
    return drafts, report


def generate_term_invoices(
    session: Session,
    ctx: StaffContext,
    *,
    term_id: uuid.UUID,
    due_date: date | None,
    class_level_ids: set[uuid.UUID] | None,
    dry_run: bool,
    notify_parents: bool = False,
    before_commit: Callable[[GenerationReport], None] | None = None,
) -> GenerationReport:
    """`before_commit`: stamp something in the same transaction as the bills (a background billing run)."""
    if not dry_run:  # a second "Bill this term" at the same moment waits, then sees these bills
        locks.hold(session, "billing", ctx.school_id)
    term = get_or_404(session, Term, term_id, label="Term")
    if periods.term_is_closed(session, ctx.school_id, term):
        raise Conflict("That term is closed. Ask the proprietor to re-open it to bill it again.", code="books_closed")
    if class_level_ids:
        found = set(session.scalars(select(ClassLevel.id).where(ClassLevel.id.in_(class_level_ids))))
        if found != class_level_ids:
            raise DomainError("Unknown class.", field="class_level_ids")
    drafts, report = draft_term_invoices(session, term, class_level_ids)
    report.dry_run = dry_run
    if dry_run or not drafts:
        session.rollback()
        return report
    numbers = reserve(session, ctx.school_id, INVOICE_SEQUENCE, len(drafts))
    invoices: list[Invoice] = []
    lines: list[InvoiceLine] = []
    for draft, number in zip(drafts, numbers, strict=True):
        invoice = Invoice(
            id=uuid.uuid4(),
            school_id=ctx.school_id,
            number=number,
            student_id=draft.student_id,
            kind=InvoiceKind.TERM,
            term_id=term.id,
            class_level_id=draft.class_level_id,
            due_date=due_date,
            created_by=ctx.user.id,
        )
        invoices.append(invoice)
        lines.extend(
            InvoiceLine(
                school_id=ctx.school_id,
                invoice_id=invoice.id,
                position=position,
                kind=line.kind,
                description=line.description,
                amount_kobo=line.amount_kobo,
                fee_item_id=line.fee_item_id,
                discount_id=line.discount_id,
                created_by=ctx.user.id,
            )
            for position, line in enumerate(draft.lines, start=1)
        )
    session.add_all(invoices)
    session.flush()
    session.add_all(lines)
    _apply_credit(session, [invoice.student_id for invoice in invoices])
    if notify_parents:
        from app.modules.notifications.service import queue_fee_notices  # notifications builds on fees

        report.notices_queued = queue_fee_notices(session, ctx.school_id, invoices)
    record(
        session,
        AuditAction.INVOICES_GENERATED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=term,
        changes={
            "invoices": len(invoices),
            "total_kobo": report.total_kobo,
            "first_number": numbers.start,
            "last_number": numbers.stop - 1,
            "classes": sorted(str(c) for c in class_level_ids) if class_level_ids else "all",
            "fee_notices": report.notices_queued,
        },
    )
    if before_commit is not None:
        before_commit(report)
    session.commit()
    return report


def _apply_credit(session: Session, student_ids: list[uuid.UUID]) -> None:
    """Money students already paid in advance (credit) pays their new invoices straight away."""
    from app.modules.payments.allocation import allocate  # payments builds on fees; this is the one call back

    session.flush()
    allocate(session, student_ids)


# --- Changing invoices -----------------------------------------------------------------------------


def _live_invoice(session: Session, invoice_id: uuid.UUID) -> Invoice:
    invoice = get_or_404(session, Invoice, invoice_id, label="Invoice", lock=True)
    if invoice.voided_at is not None:
        raise Conflict("This invoice has been voided.")
    return invoice


def add_line(
    session: Session,
    ctx: StaffContext,
    invoice_id: uuid.UUID,
    *,
    kind: LineKind,
    description: str,
    amount_kobo: int,
    reason: str,
    fee_item_id: uuid.UUID | None = None,
) -> Invoice:
    """
    Add a charge (kind=fee) or a correction (kind=adjustment, either sign) to a live invoice.
    Existing lines are never edited; the deferred check stops an invoice going below zero.
    """
    if kind == LineKind.DISCOUNT:
        raise DomainError("Use an adjustment to reduce an invoice.", field="kind")
    if kind == LineKind.WRITE_OFF:
        raise DomainError("Ask for a write-off instead: the proprietor approves those.", field="kind")
    if kind == LineKind.FEE and amount_kobo <= 0:
        raise DomainError("A charge must be more than ₦0.", field="amount_kobo")
    if amount_kobo == 0 or abs(amount_kobo) > MAX_AMOUNT_KOBO:
        raise DomainError("Enter an amount.", field="amount_kobo")
    invoice = _live_invoice(session, invoice_id)
    if fee_item_id is not None:
        get_or_404(session, FeeItem, fee_item_id, label="Fee", field="fee_item_id")
    amounts = amounts_for(session, [invoice.id])[invoice.id]
    if amounts.total_kobo + amount_kobo < 0:
        raise DomainError("That would take the invoice below ₦0.", field="amount_kobo")
    if amounts.total_kobo + amount_kobo < amounts.paid_kobo:
        raise DomainError("The invoice can't be reduced below what's already been paid on it.", field="amount_kobo")
    position = (
        session.scalar(select(func.max(InvoiceLine.position)).where(InvoiceLine.invoice_id == invoice.id)) or 0
    ) + 1
    session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            position=position,
            kind=kind,
            description=description.strip(),
            amount_kobo=amount_kobo,
            fee_item_id=fee_item_id,
            created_by=ctx.user.id,
            reason=reason.strip(),
        )
    )
    record(
        session,
        AuditAction.INVOICE_ADJUSTED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=invoice,
        changes={
            "number": invoice.number,
            "kind": kind,
            "description": description,
            "amount_kobo": amount_kobo,
            "reason": reason,
        },
    )
    session.commit()
    return invoice


def void_invoice(session: Session, ctx: StaffContext, invoice_id: uuid.UUID, reason: str) -> Invoice:
    """Cancel an invoice issued by mistake. Only possible while nothing has been paid on it."""
    invoice = _live_invoice(session, invoice_id)
    issued_on = invoice.issued_at.astimezone(LAGOS).date()
    term = session.get(Term, invoice.term_id) if invoice.term_id else None
    if (term and periods.term_is_closed(session, ctx.school_id, term)) or (
        term is None and periods.closed_on(session, ctx.school_id, issued_on)
    ):
        raise Conflict(
            "This bill belongs to a closed term. Adjust it instead (the correction is dated today).",
            code="books_closed",
        )
    amounts = amounts_for(session, [invoice.id])[invoice.id]
    if amounts.paid_kobo > 0:
        raise Conflict("Payments have been applied to this invoice. Reverse them first, or adjust the invoice instead.")
    invoice.voided_at = func.now()
    invoice.void_reason = reason.strip()
    plans.cancel_for_voided(session, invoice.id, reason.strip())
    record(
        session,
        AuditAction.INVOICE_VOIDED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=invoice,
        changes={"number": invoice.number, "total_kobo": amounts.total_kobo, "reason": reason},
    )
    session.commit()
    session.refresh(invoice)
    return invoice


def create_invoice(
    session: Session,
    ctx: StaffContext,
    *,
    student_id: uuid.UUID,
    kind: InvoiceKind,
    lines: list[DraftLine],
    due_date: date | None,
) -> Invoice:
    """An opening balance (debt from before WardPay) or a one-off charge for one student."""
    if kind == InvoiceKind.TERM:
        raise DomainError("Term invoices are generated for the whole term.", field="kind")
    if not lines or any(ln.kind != LineKind.FEE or not 0 < ln.amount_kobo <= MAX_AMOUNT_KOBO for ln in lines):
        raise DomainError("Add at least one charge of more than ₦0.", field="lines")
    student = get_or_404(session, Student, student_id, label="Student", field="student_id")
    if kind == InvoiceKind.OPENING_BALANCE and session.scalar(
        select(Invoice.id).where(
            Invoice.student_id == student.id, Invoice.kind == InvoiceKind.OPENING_BALANCE, Invoice.voided_at.is_(None)
        )
    ):
        raise Conflict("This student already has an opening balance. Adjust or void it instead.")
    [number] = reserve(session, ctx.school_id, INVOICE_SEQUENCE, 1)
    current_level = session.scalar(
        select(Enrollment.class_level_id)
        .join(Term, Term.id == Enrollment.term_id)
        .where(Enrollment.student_id == student.id, Term.is_current.is_(True))
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        school_id=ctx.school_id,
        number=number,
        student_id=student.id,
        kind=kind,
        class_level_id=current_level,
        due_date=due_date,
        created_by=ctx.user.id,
    )
    session.add(invoice)
    session.flush()
    session.add_all(
        InvoiceLine(
            invoice_id=invoice.id,
            position=i,
            kind=LineKind.FEE,
            description=ln.description,
            amount_kobo=ln.amount_kobo,
            fee_item_id=ln.fee_item_id,
            created_by=ctx.user.id,
        )
        for i, ln in enumerate(lines, start=1)
    )
    _apply_credit(session, [student.id])
    record(
        session,
        AuditAction.INVOICE_CREATED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        target=invoice,
        changes={
            "number": number,
            "kind": kind,
            "student": student.id,
            "total_kobo": sum(ln.amount_kobo for ln in lines),
        },
    )
    session.commit()
    return invoice


# --- Term summary ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TermSummary:
    invoices: int
    billed_kobo: int
    paid_kobo: int
    outstanding_kobo: int
    students_enrolled: int
    students_invoiced: int


def term_summary(session: Session, term_id: uuid.UUID) -> TermSummary:
    get_or_404(session, Term, term_id, label="Term")
    totals, paid = totals_subquery(), paid_subquery()
    total_col, paid_col = func.coalesce(totals.c.total_kobo, 0), func.coalesce(paid.c.paid_kobo, 0)
    count, billed, paid_sum, outstanding = session.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(total_col), 0),
            func.coalesce(func.sum(paid_col), 0),
            func.coalesce(func.sum(case((total_col > paid_col, total_col - paid_col), else_=0)), 0),
        )
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(Invoice.term_id == term_id, Invoice.voided_at.is_(None))
    ).one()
    enrolled = (
        session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .join(Student)
            .where(Enrollment.term_id == term_id, Student.status == StudentStatus.ACTIVE)
        )
        or 0
    )
    return TermSummary(int(count), int(billed), int(paid_sum), int(outstanding), int(enrolled), int(count))
