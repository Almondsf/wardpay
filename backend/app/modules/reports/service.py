"""
Reports: where a school's money stands.

Every figure comes from the ledger (invoice lines and payment allocations,
through `fees.invoicing.totals_subquery` and `fees.ledger.paid_subquery`), so
reports always agree with invoices, student accounts and receipts. Each report
is a fixed number of aggregate queries, however many students or payments
the school has. The session must be scoped to one school.

Dates are Lagos dates: a payment at 00:30 on 1 October in Lagos counts on
1 October.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, Select, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CTE, SelectBase

from app.core.clock import lagos_today
from app.core.errors import Conflict, DomainError
from app.core.pagination import PageParams, paginate_rows
from app.core.repo import get_or_404
from app.modules.fees.invoicing import totals_subquery
from app.modules.fees.ledger import paid_subquery
from app.modules.fees.models import Invoice, InvoiceKind
from app.modules.fees.plans import progress_cte
from app.modules.notifications.models import MessageKind, MessageLog, MessageStatus
from app.modules.payments.models import (
    Allocation,
    ManualPayment,
    ManualPaymentStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
)
from app.modules.receipts.models import Receipt
from app.modules.schools.models import AcademicSession, Arm, ClassLevel, Term
from app.modules.students.models import Enrollment, Guardian, GuardianStudent, Student, StudentStatus

MAX_RANGE_DAYS = 366
EXPORT_ROW_LIMIT = 5000


class AgingBucket(StrEnum):
    NOT_DUE = "not_due"
    DAYS_1_30 = "1_30"
    DAYS_31_60 = "31_60"
    DAYS_61_90 = "61_90"
    OVER_90 = "over_90"


def _lagos_date(column: Any) -> Any:
    return cast(func.timezone("Africa/Lagos", column), Date)


def _once(name: str, stmt: SelectBase) -> CTE:
    """
    A CTE Postgres computes exactly once. Right after a bulk insert (a term's bills) the
    planner's row estimates are stale until autovacuum re-analyzes, and with an inlined
    subquery it can pick nested loops that redo an aggregate for every outer row: seconds
    instead of milliseconds. Materializing keeps every report linear whatever the plan.
    """
    return stmt.cte(name).prefix_with("MATERIALIZED")


def _balances(today: date) -> CTE:
    """
    One row per live invoice: what it's for, when it's due, and its total, paid and balance.
    A bill on a payment plan is due when its next unpaid instalment is.
    """
    # The ledger's own definitions (one place for "total" and "paid"), each computed once
    totals = _once("invoice_totals", totals_subquery().element)
    paid = _once("invoice_paid", paid_subquery().element)
    plan = progress_cte(today)
    total = func.coalesce(totals.c.total_kobo, 0)
    paid_col = func.coalesce(paid.c.paid_kobo, 0)
    return (
        select(
            Invoice.id.label("invoice_id"),
            Invoice.student_id.label("student_id"),
            Invoice.term_id.label("term_id"),
            Invoice.class_level_id.label("class_level_id"),
            Invoice.kind.label("kind"),
            # Without a due date, a bill is due when it's issued
            func.coalesce(plan.c.next_due, Invoice.due_date, _lagos_date(Invoice.issued_at)).label("due_on"),
            plan.c.invoice_id.is_not(None).label("on_plan"),
            total.label("total"),
            paid_col.label("paid"),
            (total - paid_col).label("balance"),
        )
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .outerjoin(plan, plan.c.invoice_id == Invoice.id)
        .where(Invoice.voided_at.is_(None))
        .cte("balances")
        .prefix_with("MATERIALIZED")
    )


def _owed(balance: Any) -> Any:
    """The unpaid part of a bill (an overpaid bill owes nothing; the extra is credit)."""
    return case((balance > 0, balance), else_=0)


def _bucket(due_on: Any, today: date) -> Any:
    return case(
        (due_on >= today, literal(AgingBucket.NOT_DUE.value)),
        (due_on >= today - timedelta(days=30), literal(AgingBucket.DAYS_1_30.value)),
        (due_on >= today - timedelta(days=60), literal(AgingBucket.DAYS_31_60.value)),
        (due_on >= today - timedelta(days=90), literal(AgingBucket.DAYS_61_90.value)),
        else_=literal(AgingBucket.OVER_90.value),
    )


# --- Overview (the dashboard) ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClassRow:
    class_level_id: uuid.UUID | None
    class_name: str
    invoices: int
    billed_kobo: int
    collected_kobo: int
    outstanding_kobo: int
    students_owing: int


@dataclass(frozen=True)
class DayTotal:
    day: date
    amount_kobo: int
    payments: int


@dataclass(frozen=True)
class MethodTotal:
    method: PaymentMethod
    amount_kobo: int
    payments: int


@dataclass(frozen=True)
class AgingRow:
    bucket: AgingBucket
    amount_kobo: int
    invoices: int


@dataclass
class Overview:
    term_id: uuid.UUID
    term_label: str
    term_start: date
    term_end: date
    # This term's bills
    invoices: int = 0
    billed_kobo: int = 0
    collected_kobo: int = 0
    outstanding_kobo: int = 0
    # Everything owed, including older debts (earlier terms, balances brought forward, one-off charges)
    total_outstanding_kobo: int = 0
    older_outstanding_kobo: int = 0
    students_owing: int = 0
    credit_kobo: int = 0  # paid in, not yet owed on any bill
    received_today_kobo: int = 0
    received_7_days_kobo: int = 0
    received_this_term_kobo: int = 0
    pending_approval: int = 0
    pending_approval_kobo: int = 0
    by_class: list[ClassRow] = field(default_factory=list)
    by_day: list[DayTotal] = field(default_factory=list)
    by_method: list[MethodTotal] = field(default_factory=list)
    aging: list[AgingRow] = field(default_factory=list)


def current_term_id(session: Session) -> uuid.UUID:
    term_id = session.scalar(select(Term.id).where(Term.is_current.is_(True)))
    if term_id is None:
        raise Conflict("Set the current term first (Calendar).")
    return term_id


def overview(session: Session, term_id: uuid.UUID | None, today: date | None = None) -> Overview:
    """The proprietor's view of a term: billed, collected, outstanding, and how money is coming in."""
    today = today or lagos_today()
    term = get_or_404(session, Term, term_id or current_term_id(session), label="Term")
    session_name = session.scalar(select(AcademicSession.name).where(AcademicSession.id == term.session_id)) or ""
    result = Overview(
        term_id=term.id,
        term_label=f"{session_name} {('First', 'Second', 'Third')[term.number - 1]} term",
        term_start=term.start_date,
        term_end=term.end_date,
    )
    b = _balances(today)

    # This term's bills, per class
    rows = session.execute(
        select(
            b.c.class_level_id,
            ClassLevel.name,
            func.count(),
            func.sum(b.c.total),
            func.sum(b.c.paid),
            func.sum(_owed(b.c.balance)),
            func.count(func.distinct(case((b.c.balance > 0, b.c.student_id)))),
        )
        .select_from(b)
        .outerjoin(ClassLevel, ClassLevel.id == b.c.class_level_id)
        .where(b.c.term_id == term.id, b.c.kind == InvoiceKind.TERM)
        .group_by(b.c.class_level_id, ClassLevel.name, ClassLevel.rank)
        .order_by(ClassLevel.rank)
    ).all()
    result.by_class = [
        ClassRow(level_id, name or "No class", int(n), int(billed), int(paid), int(owed), int(owing))
        for level_id, name, n, billed, paid, owed, owing in rows
    ]
    result.invoices = sum(r.invoices for r in result.by_class)
    result.billed_kobo = sum(r.billed_kobo for r in result.by_class)
    result.collected_kobo = sum(r.collected_kobo for r in result.by_class)
    result.outstanding_kobo = sum(r.outstanding_kobo for r in result.by_class)

    # Everything owed at the school, and how many students owe
    total, older, owing_students = session.execute(
        select(
            func.coalesce(func.sum(_owed(b.c.balance)), 0),
            func.coalesce(
                func.sum(case((or_(b.c.term_id.is_(None), b.c.term_id != term.id), _owed(b.c.balance)), else_=0)), 0
            ),
            func.count(func.distinct(case((b.c.balance > 0, b.c.student_id)))),
        ).select_from(b)
    ).one()
    result.total_outstanding_kobo, result.older_outstanding_kobo = int(total), int(older)
    result.students_owing = int(owing_students)

    # How old the debts are
    bucket = _bucket(b.c.due_on, today)
    aged = {
        key: (int(amount), int(n))
        for key, amount, n in session.execute(
            select(bucket, func.sum(b.c.balance), func.count()).select_from(b).where(b.c.balance > 0).group_by(bucket)
        ).all()
    }
    result.aging = [AgingRow(k, *aged.get(k.value, (0, 0))) for k in AgingBucket]

    # Credit: money paid in that no bill has claimed yet
    paid_in = select(func.coalesce(func.sum(Payment.amount_kobo), 0)).scalar_subquery()
    applied = select(func.coalesce(func.sum(Allocation.amount_kobo), 0)).scalar_subquery()
    result.credit_kobo = int(session.scalar(select(paid_in - applied)) or 0)

    # Money received (net of reversals), by day and by method, over the term so far
    day = _lagos_date(Payment.received_at)
    window_end = min(today, term.end_date)
    in_term = and_(day >= term.start_date, day <= window_end)
    result.by_day = [
        DayTotal(d, int(amount), int(n))
        for d, amount, n in session.execute(
            select(day, func.sum(Payment.amount_kobo), func.count(case((Payment.kind == PaymentKind.RECEIPT, 1))))
            .where(in_term)
            .group_by(day)
            .order_by(day)
        ).all()
    ]
    result.by_method = [
        MethodTotal(method, int(amount), int(n))
        for method, amount, n in session.execute(
            select(
                Payment.method,
                func.sum(Payment.amount_kobo),
                func.count(case((Payment.kind == PaymentKind.RECEIPT, 1))),
            )
            .where(in_term)
            .group_by(Payment.method)
            .order_by(func.sum(Payment.amount_kobo).desc())
        ).all()
    ]
    result.received_this_term_kobo = sum(d.amount_kobo for d in result.by_day)
    today_sum, week_sum = session.execute(
        select(
            func.coalesce(func.sum(case((day == today, Payment.amount_kobo), else_=0)), 0),
            func.coalesce(func.sum(Payment.amount_kobo), 0),
        ).where(day > today - timedelta(days=7), day <= today)
    ).one()
    result.received_today_kobo, result.received_7_days_kobo = int(today_sum), int(week_sum)

    pending, pending_sum = session.execute(
        select(func.count(), func.coalesce(func.sum(ManualPayment.amount_kobo), 0)).where(
            ManualPayment.status == ManualPaymentStatus.PENDING
        )
    ).one()
    result.pending_approval, result.pending_approval_kobo = int(pending), int(pending_sum)
    return result


# --- Debtors ----------------------------------------------------------------------------------------


class DebtorSort(StrEnum):
    OWED = "owed"  # most owed first
    OLDEST = "oldest"  # longest overdue first
    NAME = "name"
    CLASS = "class"


@dataclass(frozen=True)
class DebtorFilters:
    class_level_id: uuid.UUID | None = None
    min_owed_kobo: int = 1
    overdue_only: bool = False
    search: str = ""
    sort: DebtorSort = DebtorSort.OWED


@dataclass(frozen=True)
class Debtor:
    student_id: uuid.UUID
    admission_number: str
    full_name: str
    status: StudentStatus
    class_name: str
    owed_kobo: int
    oldest_due: date | None
    days_overdue: int
    guardian_name: str
    guardian_phone: str
    reminders_off: bool
    reminders_sent: int
    last_paid_at: datetime | None
    on_plan: bool  # paying in instalments; "overdue" counts from the next unpaid instalment


@dataclass(frozen=True)
class DebtorsPage:
    items: list[Debtor]
    total: int
    total_owed_kobo: int


def _debtors_query(filters: DebtorFilters, today: date) -> Select[Any]:
    b = _balances(today)
    owing = _once(
        "owing",
        select(
            b.c.student_id,
            func.sum(_owed(b.c.balance)).label("owed"),
            func.min(case((b.c.balance > 0, b.c.due_on))).label("oldest_due"),
            func.bool_or(and_(b.c.on_plan, b.c.balance > 0)).label("on_plan"),
        )
        .group_by(b.c.student_id)
        .having(func.sum(_owed(b.c.balance)) >= max(filters.min_owed_kobo, 1)),
    )
    placement = (
        select(Enrollment.student_id, Enrollment.class_level_id, Enrollment.arm_id)
        .join(Term, Term.id == Enrollment.term_id)
        .where(Term.is_current.is_(True))
        .subquery("placement")
    )
    primary = (
        select(GuardianStudent.student_id, GuardianStudent.guardian_id)
        .where(GuardianStudent.is_primary.is_(True))
        .subquery("primary_guardian")
    )
    reversed_ids = select(Payment.reverses_payment_id).where(Payment.reverses_payment_id.is_not(None))
    last_paid = _once(
        "last_paid",
        select(Payment.student_id, func.max(Payment.received_at).label("at"))
        .where(Payment.kind == PaymentKind.RECEIPT, Payment.id.not_in(reversed_ids))  # a bounced teller isn't a payment
        .group_by(Payment.student_id),
    )
    reminded = _once(
        "reminded",
        select(MessageLog.student_id, func.count().label("sent"))
        .where(MessageLog.kind == MessageKind.REMINDER, MessageLog.status == MessageStatus.SENT)
        .group_by(MessageLog.student_id),
    )
    stmt = (
        select(
            Student.id,
            Student.admission_number,
            Student.first_name,
            Student.last_name,
            Student.status,
            ClassLevel.name,
            Arm.name,
            owing.c.owed,
            owing.c.oldest_due,
            Guardian.full_name,
            Guardian.phone,
            Guardian.reminders_opt_out_at,
            func.coalesce(reminded.c.sent, 0),
            last_paid.c.at,
            owing.c.on_plan,
        )
        .join(owing, owing.c.student_id == Student.id)
        .outerjoin(placement, placement.c.student_id == Student.id)
        .outerjoin(ClassLevel, ClassLevel.id == placement.c.class_level_id)
        .outerjoin(Arm, Arm.id == placement.c.arm_id)
        .outerjoin(primary, primary.c.student_id == Student.id)
        .outerjoin(Guardian, Guardian.id == primary.c.guardian_id)
        .outerjoin(last_paid, last_paid.c.student_id == Student.id)
        .outerjoin(reminded, reminded.c.student_id == Student.id)
    )
    if filters.class_level_id:
        stmt = stmt.where(placement.c.class_level_id == filters.class_level_id)
    if filters.overdue_only:
        stmt = stmt.where(owing.c.oldest_due < today)
    if term := filters.search.strip():
        pattern = f"%{term.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(
                Student.first_name.ilike(pattern),
                Student.last_name.ilike(pattern),
                Student.admission_number.ilike(pattern),
                (Student.first_name + " " + Student.last_name).ilike(pattern),
            )
        )
    order = {
        DebtorSort.OWED: (owing.c.owed.desc(),),
        DebtorSort.OLDEST: (owing.c.oldest_due.asc().nulls_last(), owing.c.owed.desc()),
        DebtorSort.NAME: (Student.last_name, Student.first_name),
        DebtorSort.CLASS: (ClassLevel.rank.asc().nulls_last(), Arm.name, Student.last_name, Student.first_name),
    }[filters.sort]
    return stmt.order_by(*order, Student.id)


def _debtor(row: Any, today: date) -> Debtor:
    (sid, admission, first, last, status, level, arm, owed, oldest, g_name, g_phone, opt_out, sent, last_paid, plan) = (
        row
    )
    return Debtor(
        student_id=sid,
        admission_number=admission,
        full_name=f"{first} {last}",
        status=status,
        class_name=f"{level} {arm}" if level else "",
        owed_kobo=int(owed),
        oldest_due=oldest,
        days_overdue=max((today - oldest).days, 0) if oldest else 0,
        guardian_name=g_name or "",
        guardian_phone=g_phone or "",
        reminders_off=opt_out is not None,
        reminders_sent=int(sent),
        last_paid_at=last_paid,
        on_plan=bool(plan),
    )


def debtors(session: Session, filters: DebtorFilters, params: PageParams, today: date | None = None) -> DebtorsPage:
    """Students who owe, with who to call and how overdue they are. Four queries for any page."""
    today = today or lagos_today()
    stmt = _debtors_query(filters, today)
    rows, total = paginate_rows(session, stmt, params)
    filtered = stmt.order_by(None).subquery()
    total_owed = session.scalar(select(func.coalesce(func.sum(filtered.c.owed), 0)))
    return DebtorsPage([_debtor(r, today) for r in rows], total, int(total_owed or 0))


def all_debtors(
    session: Session, filters: DebtorFilters, today: date | None = None, *, limit: int | None = EXPORT_ROW_LIMIT
) -> list[Debtor]:
    """
    For exports: every matching debtor, up to `limit` (downloads made while someone waits). Files built in
    the background pass None: the end-of-session pack must list everyone (load test: 10,000 pupils).
    """
    today = today or lagos_today()
    stmt = _debtors_query(filters, today)
    rows = session.execute(stmt if limit is None else stmt.limit(limit + 1)).all()
    if limit is not None and len(rows) > limit:
        raise DomainError(f"More than {limit} students match. Narrow it down (e.g. by class).")
    return [_debtor(r, today) for r in rows]


# --- Collections over a date range ------------------------------------------------------------------


@dataclass(frozen=True)
class CollectedPayment:
    received_at: datetime
    receipt_number: int | None
    student_name: str
    admission_number: str
    class_name: str
    method: PaymentMethod
    reference: str
    amount_kobo: int  # negative for a reversal
    is_reversal: bool


@dataclass(frozen=True)
class Collections:
    start: date
    end: date
    received_kobo: int  # receipts
    reversed_kobo: int  # reversals (negative)
    net_kobo: int
    payments: int
    by_day: list[DayTotal]
    by_method: list[MethodTotal]


def check_range(start: date, end: date) -> None:
    if end < start:
        raise DomainError("The end date is before the start date.", field="end")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise DomainError("Choose at most a year at a time.", field="end")


def collections(session: Session, start: date, end: date) -> Collections:
    """Money received between two Lagos dates (inclusive), net of reversals. Three queries."""
    check_range(start, end)
    day = _lagos_date(Payment.received_at)
    in_range = and_(day >= start, day <= end)
    receipt = Payment.kind == PaymentKind.RECEIPT
    received, reversed_, count = session.execute(
        select(
            func.coalesce(func.sum(case((receipt, Payment.amount_kobo), else_=0)), 0),
            func.coalesce(func.sum(case((receipt, 0), else_=Payment.amount_kobo)), 0),
            func.count(case((receipt, 1))),
        ).where(in_range)
    ).one()
    by_day = [
        DayTotal(d, int(amount), int(n))
        for d, amount, n in session.execute(
            select(day, func.sum(Payment.amount_kobo), func.count(case((receipt, 1))))
            .where(in_range)
            .group_by(day)
            .order_by(day)
        ).all()
    ]
    by_method = [
        MethodTotal(method, int(amount), int(n))
        for method, amount, n in session.execute(
            select(Payment.method, func.sum(Payment.amount_kobo), func.count(case((receipt, 1))))
            .where(in_range)
            .group_by(Payment.method)
            .order_by(func.sum(Payment.amount_kobo).desc())
        ).all()
    ]
    return Collections(
        start, end, int(received), int(reversed_), int(received) + int(reversed_), int(count), by_day, by_method
    )


def collected_payments(session: Session, start: date, end: date) -> list[CollectedPayment]:
    """Every payment and reversal in the range, oldest first, for exports. One query."""
    check_range(start, end)
    day = _lagos_date(Payment.received_at)
    placement = (
        select(Enrollment.student_id, Enrollment.class_level_id, Enrollment.arm_id)
        .join(Term, Term.id == Enrollment.term_id)
        .where(Term.is_current.is_(True))
        .subquery("placement")
    )
    rows = session.execute(
        select(
            Payment.received_at,
            Receipt.number,
            Student.first_name,
            Student.last_name,
            Student.admission_number,
            ClassLevel.name,
            Arm.name,
            Payment.method,
            Payment.reference,
            Payment.gateway_reference,
            Payment.amount_kobo,
            Payment.kind,
        )
        .join(Student, Student.id == Payment.student_id)
        .outerjoin(Receipt, Receipt.payment_id == Payment.id)
        .outerjoin(placement, placement.c.student_id == Student.id)
        .outerjoin(ClassLevel, ClassLevel.id == placement.c.class_level_id)
        .outerjoin(Arm, Arm.id == placement.c.arm_id)
        .where(day >= start, day <= end)
        .order_by(Payment.received_at, Receipt.number.nulls_last(), Payment.created_at, Payment.id)
        .limit(EXPORT_ROW_LIMIT + 1)
    ).all()
    if len(rows) > EXPORT_ROW_LIMIT:
        raise DomainError(f"More than {EXPORT_ROW_LIMIT} payments in that period. Choose a shorter one.")
    return [
        CollectedPayment(
            received_at=at,
            receipt_number=number,
            student_name=f"{first} {last}",
            admission_number=admission,
            class_name=f"{level} {arm}" if level else "",
            method=method,
            reference=reference or gateway_ref or "",
            amount_kobo=int(amount),
            is_reversal=kind == PaymentKind.REVERSAL,
        )
        for at, number, first, last, admission, level, arm, method, reference, gateway_ref, amount, kind in rows
    ]
