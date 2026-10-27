"""
Head-office figures: every branch of a school group side by side, and the group's totals (owner, 2026-10-27,
school groups step 2).

The session here is platform-wide (see `accounts.deps.require_head_office`), so every query names the
branches explicitly (`school_id IN branch_ids`); nothing is read for any other school. Figures only: no
pupil, parent or payment details. Each figure is one aggregate query grouped by branch, so the number of
queries is fixed however many branches or pupils there are.

"This term" is each branch's own current term (branches may run different calendars). Money received is
net of reversals; dates are Lagos dates.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session

from app.core.clock import lagos_today
from app.modules.fees.invoicing import totals_subquery
from app.modules.fees.ledger import paid_subquery
from app.modules.fees.models import Invoice, InvoiceKind
from app.modules.payments.models import Allocation, ManualPayment, ManualPaymentStatus, Payment
from app.modules.schools.models import AcademicSession, School, SchoolGroup, Term
from app.modules.students.models import Student, StudentStatus

DAYS = 30


@dataclass
class BranchFigures:
    school_id: uuid.UUID | None  # None on the totals row
    name: str
    term_label: str = ""  # the branch's current term; empty if it has none set
    pupils: int = 0
    billed_kobo: int = 0  # this term's bills
    collected_kobo: int = 0
    outstanding_kobo: int = 0
    older_outstanding_kobo: int = 0  # owed on earlier terms' and one-off bills
    students_owing: int = 0
    credit_kobo: int = 0
    received_today_kobo: int = 0
    received_7_days_kobo: int = 0
    received_30_days_kobo: int = 0
    pending_approval: int = 0
    pending_approval_kobo: int = 0

    @property
    def total_outstanding_kobo(self) -> int:
        return self.outstanding_kobo + self.older_outstanding_kobo


@dataclass(frozen=True)
class GroupDay:
    day: date
    amount_kobo: int


@dataclass
class GroupOverview:
    group_name: str
    today: date
    branches: list[BranchFigures]
    totals: BranchFigures
    by_day: list[GroupDay] = field(default_factory=list)  # the whole group, last 30 days, every day


_SUMMED = (
    "pupils",
    "billed_kobo",
    "collected_kobo",
    "outstanding_kobo",
    "older_outstanding_kobo",
    "students_owing",
    "credit_kobo",
    "received_today_kobo",
    "received_7_days_kobo",
    "received_30_days_kobo",
    "pending_approval",
    "pending_approval_kobo",
)


def _lagos_date(column: Any) -> Any:
    return cast(func.timezone("Africa/Lagos", column), Date)


def overview(
    session: Session, group_id: uuid.UUID, branch_ids: tuple[uuid.UUID, ...], today: date | None = None
) -> GroupOverview:
    today = today or lagos_today()
    ids = list(branch_ids)
    group_name = session.scalar(select(SchoolGroup.name).where(SchoolGroup.id == group_id)) or ""

    # The branches and their current terms
    rows = session.execute(
        select(School.id, School.name, Term.id, Term.number, AcademicSession.name)
        .outerjoin(Term, (Term.school_id == School.id) & Term.is_current.is_(True))
        .outerjoin(AcademicSession, AcademicSession.id == Term.session_id)
        .where(School.id.in_(ids))
        .order_by(School.name)
    ).all()
    branches = {
        sid: BranchFigures(
            sid, name, f"{session_name} {('First', 'Second', 'Third')[number - 1]} term" if term_id else ""
        )
        for sid, name, term_id, number, session_name in rows
    }
    current_terms = [term_id for _, _, term_id, _, _ in rows if term_id is not None]

    if ids:
        _pupils(session, ids, branches)
        _bills(session, ids, current_terms, branches)
        _credit(session, ids, branches)
        by_day = _received(session, ids, today, branches)
        _pending(session, ids, branches)
    else:
        by_day = {}

    totals = BranchFigures(None, "All branches")
    for b in branches.values():
        for name in _SUMMED:
            setattr(totals, name, getattr(totals, name) + getattr(b, name))
    start = today - timedelta(days=DAYS - 1)
    days = [GroupDay(start + timedelta(days=i), by_day.get(start + timedelta(days=i), 0)) for i in range(DAYS)]
    return GroupOverview(group_name, today, list(branches.values()), totals, days)


def _pupils(session: Session, ids: list[uuid.UUID], branches: dict[uuid.UUID, BranchFigures]) -> None:
    for sid, n in session.execute(
        select(Student.school_id, func.count())
        .where(Student.school_id.in_(ids), Student.status == StudentStatus.ACTIVE)
        .group_by(Student.school_id)
    ):
        branches[sid].pupils = int(n)


def _bills(
    session: Session, ids: list[uuid.UUID], current_terms: list[uuid.UUID], branches: dict[uuid.UUID, BranchFigures]
) -> None:
    live = select(Invoice.id).where(Invoice.school_id.in_(ids), Invoice.voided_at.is_(None))
    totals = totals_subquery(live).element.cte("group_invoice_totals").prefix_with("MATERIALIZED")
    paid = paid_subquery(live).element.cte("group_invoice_paid").prefix_with("MATERIALIZED")
    total = func.coalesce(totals.c.total_kobo, 0)
    paid_col = func.coalesce(paid.c.paid_kobo, 0)
    balance = total - paid_col
    owed = case((balance > 0, balance), else_=0)
    this_term = Invoice.term_id.in_(current_terms) & (Invoice.kind == InvoiceKind.TERM)
    for sid, billed, collected, outstanding, older, owing in session.execute(
        select(
            Invoice.school_id,
            func.coalesce(func.sum(case((this_term, total), else_=0)), 0),
            func.coalesce(func.sum(case((this_term, paid_col), else_=0)), 0),
            func.coalesce(func.sum(case((this_term, owed), else_=0)), 0),
            func.coalesce(func.sum(case((this_term, 0), else_=owed)), 0),
            func.count(func.distinct(case((balance > 0, Invoice.student_id)))),
        )
        .outerjoin(totals, totals.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(Invoice.school_id.in_(ids), Invoice.voided_at.is_(None))
        .group_by(Invoice.school_id)
    ):
        b = branches[sid]
        b.billed_kobo, b.collected_kobo, b.outstanding_kobo = int(billed), int(collected), int(outstanding)
        b.older_outstanding_kobo, b.students_owing = int(older), int(owing)


def _credit(session: Session, ids: list[uuid.UUID], branches: dict[uuid.UUID, BranchFigures]) -> None:
    """Money paid in that no bill has claimed yet."""
    paid_in = (
        select(Payment.school_id, func.sum(Payment.amount_kobo).label("kobo"))
        .where(Payment.school_id.in_(ids))
        .group_by(Payment.school_id)
        .subquery()
    )
    applied = (
        select(Allocation.school_id, func.sum(Allocation.amount_kobo).label("kobo"))
        .where(Allocation.school_id.in_(ids))
        .group_by(Allocation.school_id)
        .subquery()
    )
    for sid, credit in session.execute(
        select(paid_in.c.school_id, paid_in.c.kobo - func.coalesce(applied.c.kobo, 0)).outerjoin(
            applied, applied.c.school_id == paid_in.c.school_id
        )
    ):
        branches[sid].credit_kobo = int(credit)


def _received(
    session: Session, ids: list[uuid.UUID], today: date, branches: dict[uuid.UUID, BranchFigures]
) -> dict[date, int]:
    day = _lagos_date(Payment.received_at)
    by_day: dict[date, int] = {}
    for sid, d, amount in session.execute(
        select(Payment.school_id, day, func.sum(Payment.amount_kobo))
        .where(Payment.school_id.in_(ids), day > today - timedelta(days=DAYS), day <= today)
        .group_by(Payment.school_id, day)
    ):
        b, amount = branches[sid], int(amount)
        by_day[d] = by_day.get(d, 0) + amount
        b.received_30_days_kobo += amount
        if d > today - timedelta(days=7):
            b.received_7_days_kobo += amount
        if d == today:
            b.received_today_kobo += amount
    return by_day


def _pending(session: Session, ids: list[uuid.UUID], branches: dict[uuid.UUID, BranchFigures]) -> None:
    for sid, n, kobo in session.execute(
        select(ManualPayment.school_id, func.count(), func.sum(ManualPayment.amount_kobo))
        .where(ManualPayment.school_id.in_(ids), ManualPayment.status == ManualPaymentStatus.PENDING)
        .group_by(ManualPayment.school_id)
    ):
        branches[sid].pending_approval, branches[sid].pending_approval_kobo = int(n), int(kobo)
