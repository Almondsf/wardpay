"""
Unusual activity: things a proprietor should look at, because they're where school money usually goes missing.
Nothing here accuses anyone; each item says what happened, when and who recorded it, so the proprietor
can ask.

Over the last N days:
- **Payments reversed** (a reversal undoes a receipt); one person reversing several stands out.
- **Cash recorded after the day was closed**, and **drawer counts that came up short** (from the cash book).
- **Cash entered days after it was taken** (received three or more days before it was recorded).
- **Bills reduced**: corrections that lower a bill (large ones first) and bills voided.
- **Debt written off**, the proprietor's own included.
- **Banking that didn't match the bank** (rejected by the second person).
- **Spending that looks entered twice** (same payee and amount within a week).

A fixed number of queries; the session is scoped to the school.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Date, and_, cast, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.core.clock import lagos_today
from app.modules.accounts.models import User
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.expenses.models import Expense, ExpenseStatus
from app.modules.fees.models import Invoice, InvoiceLine, LineKind, WriteOff, WriteOffStatus
from app.modules.payments.cashbook import cash_book
from app.modules.payments.models import (
    CashDeposit,
    CashDepositStatus,
    ManualPayment,
    ManualPaymentStatus,
    Payment,
    PaymentKind,
)
from app.modules.schools.models import School
from app.modules.students.models import Student

MAX_DAYS = 180
LATE_ENTRY_DAYS = 3
BIG_REDUCTION_KOBO = 50_000 * 100
REVERSALS_BY_ONE = 3  # one person reversing this many stands out
DUPLICATE_WINDOW_DAYS = 7
MAX_ITEMS = 50


@dataclass(frozen=True)
class Item:
    day: date
    what: str
    amount_kobo: int | None = None
    who: str = ""
    link: str = ""  # a console path, when there's somewhere to look


@dataclass
class Signal:
    key: str
    title: str
    why: str  # why it's worth a look
    serious: bool = False
    items: list[Item] = field(default_factory=list)


@dataclass
class Signals:
    start: date
    end: date
    signals: list[Signal]

    @property
    def count(self) -> int:
        return sum(len(s.items) for s in self.signals)


def _day(column: Any) -> Any:
    return cast(func.timezone("Africa/Lagos", column), Date)


def signals(session: Session, school_id: uuid.UUID, days: int = 30, today: date | None = None) -> Signals:
    today = today or lagos_today()
    days = max(1, min(days, MAX_DAYS))
    start, end = today - timedelta(days=days - 1), today
    since = _day  # readability below
    who_ids: set[uuid.UUID] = set()

    reversals = session.execute(
        select(since(Payment.received_at), Payment.amount_kobo, Payment.note, Payment.recorded_by, Student.id)
        .join(Student, Student.id == Payment.student_id)
        .where(Payment.kind == PaymentKind.REVERSAL, since(Payment.received_at).between(start, end))
        .order_by(Payment.received_at.desc())
        .limit(MAX_ITEMS)
    ).all()
    backdated = session.execute(
        select(
            ManualPayment.received_on,
            since(ManualPayment.created_at),
            ManualPayment.amount_kobo,
            ManualPayment.method,
            ManualPayment.recorded_by,
        )
        .where(
            ManualPayment.status != ManualPaymentStatus.REJECTED,
            since(ManualPayment.created_at).between(start, end),
            since(ManualPayment.created_at) - ManualPayment.received_on >= LATE_ENTRY_DAYS,
        )
        .order_by(ManualPayment.created_at.desc())
        .limit(MAX_ITEMS)
    ).all()
    reductions = session.execute(
        select(since(InvoiceLine.created_at), InvoiceLine.amount_kobo, InvoiceLine.reason, InvoiceLine.created_by)
        .add_columns(Invoice.id, Invoice.number)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.kind == LineKind.ADJUSTMENT,
            InvoiceLine.amount_kobo < 0,
            since(InvoiceLine.created_at).between(start, end),
        )
        .order_by(InvoiceLine.amount_kobo, InvoiceLine.created_at.desc())  # the biggest first
        .limit(MAX_ITEMS)
    ).all()
    voids = session.execute(
        select(since(AuditLog.created_at), AuditLog.changes, AuditLog.actor_user_id, AuditLog.target_id)
        .where(AuditLog.action == AuditAction.INVOICE_VOIDED.value, since(AuditLog.created_at).between(start, end))
        .order_by(AuditLog.created_at.desc())
        .limit(MAX_ITEMS)
    ).all()
    write_offs = session.execute(
        select(
            since(WriteOff.decided_at),
            WriteOff.amount_kobo,
            WriteOff.reason,
            WriteOff.requested_by,
            WriteOff.decided_by,
            Invoice.id,
            Invoice.number,
        )
        .join(Invoice, Invoice.id == WriteOff.invoice_id)
        .where(WriteOff.status == WriteOffStatus.APPROVED, since(WriteOff.decided_at).between(start, end))
        .order_by(WriteOff.decided_at.desc())
        .limit(MAX_ITEMS)
    ).all()
    rejected_banking = session.execute(
        select(CashDeposit.banked_on, CashDeposit.amount_kobo, CashDeposit.rejection_reason, CashDeposit.recorded_by)
        .where(CashDeposit.status == CashDepositStatus.REJECTED, since(CashDeposit.decided_at).between(start, end))
        .order_by(CashDeposit.banked_on.desc())
        .limit(MAX_ITEMS)
    ).all()
    other = aliased(Expense)
    duplicates = session.execute(
        select(Expense.spent_on, other.spent_on, Expense.amount_kobo, Expense.payee, Expense.recorded_by)
        .join(
            other,
            and_(
                other.id != Expense.id,
                # Each pair once: the later entry is the suspect (ids break a tie in time)
                tuple_(other.created_at, other.id) < tuple_(Expense.created_at, Expense.id),
                func.lower(other.payee) == func.lower(Expense.payee),
                other.amount_kobo == Expense.amount_kobo,
                func.abs(other.spent_on - Expense.spent_on) <= DUPLICATE_WINDOW_DAYS,
                other.status.notin_((ExpenseStatus.REJECTED, ExpenseStatus.VOIDED)),
            ),
        )
        .where(
            Expense.status.notin_((ExpenseStatus.REJECTED, ExpenseStatus.VOIDED)),
            Expense.spent_on.between(start, end),
        )
        .order_by(Expense.spent_on.desc())
        .limit(MAX_ITEMS)
    ).all()
    book = cash_book(session, start, end)

    for rows, columns in (
        (reversals, (3,)),
        (backdated, (4,)),
        (reductions, (3,)),
        (voids, (2,)),
        (write_offs, (3, 4)),
        (rejected_banking, (3,)),
        (duplicates, (4,)),
    ):
        for row in rows:
            for c in columns:
                value = row[c]
                if isinstance(value, uuid.UUID):
                    who_ids.add(value)
    names: dict[uuid.UUID, str] = {}
    if who_ids:  # staff aren't tenant rows: the school (and its group's head office) is named explicitly
        group = select(School.group_id).where(School.id == school_id).scalar_subquery()
        users = select(User.id, User.full_name).where(
            User.id.in_(who_ids), or_(User.school_id == school_id, User.group_id == group)
        )
        names = {row.id: row.full_name for row in session.execute(users)}

    def name(user_id: uuid.UUID | None) -> str:
        return names.get(user_id, "") if user_id else ""

    by_person: dict[uuid.UUID, int] = {}
    for *_, reverser, _student in reversals:
        if isinstance(reverser, uuid.UUID):
            by_person[reverser] = by_person.get(reverser, 0) + 1
    many = [name(u) for u, n in by_person.items() if n >= REVERSALS_BY_ONE]

    found = [
        Signal(
            "reversals",
            "Payments reversed",
            "A reversal undoes a receipt and puts the debt back. "
            "Check each had a real reason (a bounced teller, a duplicate)."
            + (f" {', '.join(many)} reversed {REVERSALS_BY_ONE} or more." if many else ""),
            serious=bool(many),
            items=[
                Item(d, note or "No reason given", int(amount), name(by), f"/console/students/{student}")
                for d, amount, note, by, student in reversals
            ],
        ),
        Signal(
            "late_after_close",
            "Recorded after the day was closed",
            "Cash, banking or spending entered for a day after its drawer count: that's where cash goes missing.",
            serious=bool(book.late),
            items=[
                Item(e.day, f"{e.kind.title()} recorded on {e.recorded_at:%d %b}", e.amount_kobo, e.recorded_by)
                for e in book.late[:MAX_ITEMS]
            ],
        ),
        Signal(
            "short_counts",
            "Drawer counts that came up short",
            "The cash counted at the end of the day was less than the cash book expected.",
            serious=any(d.difference_kobo is not None and d.difference_kobo < 0 for d in book.days),
            items=[
                Item(d.day, "Counted less than expected", d.difference_kobo)
                for d in reversed(book.days)
                if d.difference_kobo is not None and d.difference_kobo < 0
            ][:MAX_ITEMS],
        ),
        Signal(
            "backdated_cash",
            "Payments entered days after they were taken",
            f"Recorded {LATE_ENTRY_DAYS} or more days after the date the money was received.",
            items=[
                Item(
                    recorded,
                    f"{method.value.replace('_', ' ').title()} received {received:%d %b}",
                    int(amount),
                    name(by),
                )
                for received, recorded, amount, method, by in backdated
            ],
        ),
        Signal(
            "bill_reductions",
            "Bills reduced or voided",
            "Corrections that lower a bill, biggest first, and bills cancelled. "
            "Each should match a decision you know of.",
            serious=any(-amount >= BIG_REDUCTION_KOBO for _, amount, _, _, _, _ in reductions),
            items=[
                Item(d, f"Bill #{number}: {reason or 'no reason'}", int(amount), name(by), f"/console/invoices/{iid}")
                for d, amount, reason, by, iid, number in reductions
            ]
            + [
                Item(
                    d,
                    f"Bill #{changes.get('number', '')} voided: {changes.get('reason', '')}",
                    -int(changes.get("total_kobo") or 0),
                    name(by),
                    f"/console/invoices/{target}" if target else "",
                )
                for d, changes, by, target in voids
            ],
        ),
        Signal(
            "write_offs",
            "Debt written off",
            "Money the school stopped chasing. A proprietor's own write-off has no second check.",
            items=[
                Item(
                    d,
                    f"Bill #{number}: {reason}" + (" (the proprietor's own)" if asked == decided else ""),
                    -int(amount),
                    name(asked),
                    f"/console/invoices/{iid}",
                )
                for d, amount, reason, asked, decided, iid, number in write_offs
            ],
        ),
        Signal(
            "rejected_banking",
            "Banking that didn't match the bank",
            "Cash recorded as banked that the second person couldn't find on the statement: it still counts as held.",
            serious=bool(rejected_banking),
            items=[
                Item(d, f"Banking rejected: {reason}", int(amount), name(by))
                for d, amount, reason, by in rejected_banking
            ],
        ),
        Signal(
            "duplicate_spending",
            "Spending that looks entered twice",
            f"The same payee and amount within {DUPLICATE_WINDOW_DAYS} days.",
            items=[
                Item(d, f"{payee}, also on {first:%d %b}", int(amount), name(by), "/console/expenses")
                for d, first, amount, payee, by in duplicates
            ],
        ),
    ]
    return Signals(start, end, [s for s in found if s.items])
