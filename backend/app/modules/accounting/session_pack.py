"""
The end-of-session pack: one workbook that closes a school year for the proprietor and the accountant.

Sheets: a summary; each term's bills (charged, discounts, paid, still owed); money in by how it came
and approved spending by category; who still owes, as of today; the cash book month by month; and the
general journal for the whole session (journal, account totals, chart of accounts).

For the current session the pack runs to today. A fixed number of queries; the session is scoped.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.clock import lagos_today
from app.core.errors import NotFound
from app.modules.expenses.service import IncomeExpenditure, income_expenditure
from app.modules.fees.invoicing import paid_subquery
from app.modules.fees.models import Invoice, InvoiceKind, InvoiceLine, LineKind
from app.modules.payments.cashbook import CashBook, cash_book
from app.modules.reports.service import Debtor, DebtorFilters, all_debtors
from app.modules.schools.models import AcademicSession, Term

from .journal import Journal, journal

TERM_NAMES = ("First", "Second", "Third")


@dataclass(frozen=True)
class TermRow:
    label: str
    bills: int
    charged_kobo: int  # fees and adjustments
    discounts_kobo: int  # a positive number
    written_off_kobo: int  # approved write-offs, a positive number
    paid_kobo: int  # paid against these bills, whenever it was paid
    owed_kobo: int  # still owed on these bills
    pupils_owing: int

    @property
    def billed_kobo(self) -> int:
        return self.charged_kobo - self.discounts_kobo


@dataclass(frozen=True)
class CashMonth:
    month: date  # the first of the month
    received_kobo: int
    reversed_kobo: int
    banked_kobo: int
    spent_kobo: int
    held_kobo: int  # should be in the drawer at the end of the month


@dataclass
class SessionPack:
    name: str
    start: date
    end: date
    terms: list[TermRow]
    money: IncomeExpenditure
    debtors: list[Debtor]
    cash: CashBook
    months: list[CashMonth]
    journal: Journal


def _session(session: Session, session_id: uuid.UUID | None, today: date) -> AcademicSession:
    stmt = select(AcademicSession)
    if session_id is not None:
        stmt = stmt.where(AcademicSession.id == session_id)
    else:  # the one running today, else the latest that has started
        stmt = stmt.where(AcademicSession.start_date <= today).order_by(AcademicSession.start_date.desc()).limit(1)
    found = session.scalar(stmt)
    if found is None:
        raise NotFound("No such school year.")
    return found


def find_session(session: Session, session_id: uuid.UUID | None, today: date | None = None) -> AcademicSession:
    """The school year a pack is for (404 if it isn't the school's). One query."""
    return _session(session, session_id, today or lagos_today())


def _terms(session: Session, academic: AcademicSession) -> list[TermRow]:
    """Each term's bills in one query."""
    lines = (
        select(
            InvoiceLine.invoice_id,
            func.sum(
                case((InvoiceLine.kind.in_((LineKind.DISCOUNT, LineKind.WRITE_OFF)), 0), else_=InvoiceLine.amount_kobo)
            ).label("charged"),
            func.sum(case((InvoiceLine.kind == LineKind.DISCOUNT, -InvoiceLine.amount_kobo), else_=0)).label(
                "discounts"
            ),
            func.sum(case((InvoiceLine.kind == LineKind.WRITE_OFF, -InvoiceLine.amount_kobo), else_=0)).label(
                "written_off"
            ),
            func.sum(InvoiceLine.amount_kobo).label("total"),
        )
        .group_by(InvoiceLine.invoice_id)
        .subquery("lines")
    )
    paid = paid_subquery()
    paid_col = func.coalesce(paid.c.paid_kobo, 0)
    owed = func.greatest(func.coalesce(lines.c.total, 0) - paid_col, 0)
    rows = session.execute(
        select(
            Term.number,
            func.count(Invoice.id),
            func.coalesce(func.sum(lines.c.charged), 0),
            func.coalesce(func.sum(lines.c.discounts), 0),
            func.coalesce(func.sum(lines.c.written_off), 0),
            func.coalesce(func.sum(paid_col), 0),
            func.coalesce(func.sum(owed), 0),
            func.count(func.distinct(case((owed > 0, Invoice.student_id)))),
        )
        .select_from(Term)
        .outerjoin(
            Invoice,
            (Invoice.term_id == Term.id) & (Invoice.kind == InvoiceKind.TERM) & Invoice.voided_at.is_(None),
        )
        .outerjoin(lines, lines.c.invoice_id == Invoice.id)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(Term.session_id == academic.id)
        .group_by(Term.number)
        .order_by(Term.number)
    ).all()
    return [
        TermRow(f"{TERM_NAMES[n - 1]} term", int(b), int(c), int(d), int(x), int(p), int(o), int(w))
        for n, b, c, d, x, p, o, w in rows
    ]


def _months(book: CashBook) -> list[CashMonth]:
    months: dict[date, CashMonth] = {}
    for day in book.days:
        key = day.day.replace(day=1)
        m = months.get(key)
        months[key] = CashMonth(
            key,
            (m.received_kobo if m else 0) + day.received_kobo,
            (m.reversed_kobo if m else 0) + day.reversed_kobo,
            (m.banked_kobo if m else 0) + day.banked_kobo,
            (m.spent_kobo if m else 0) + day.spent_kobo,
            day.held_kobo,  # days are in order: the last one of the month wins
        )
    return list(months.values())


def session_pack(session: Session, session_id: uuid.UUID | None, today: date | None = None) -> SessionPack:
    today = today or lagos_today()
    academic = _session(session, session_id, today)
    start, end = academic.start_date, min(academic.end_date, today)
    if end < start:
        raise NotFound("That school year hasn't started yet.")
    book = cash_book(session, start, end)
    return SessionPack(
        name=academic.name,
        start=start,
        end=end,
        terms=_terms(session, academic),
        money=income_expenditure(session, start, end),
        debtors=all_debtors(session, DebtorFilters(), today, limit=None),  # built in the background
        cash=book,
        months=_months(book),
        journal=journal(session, start, end),
    )
