"""
"Generate invoices", run in the background (load test, 2026-10-25: billing 10,000 pupils took about 30 seconds,
and the web app's proxy gives up on a request after about 30).

`start` checks what can fail at once (the term exists and isn't closed, the classes are the school's) and queues
the job; the job (`run`) makes the bills with the real generator and marks the run done in the same transaction,
keeping the result for the screen. Where background tasks run straight away (development, tests), the run has
finished by the time `start` returns, so callers can answer with the result directly.
"""

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import outbox
from app.core.db import BULK_ROWS, refresh_statistics
from app.core.errors import Conflict, DomainError, NotFound
from app.core.http import RequestMeta
from app.core.repo import get_or_404
from app.modules.accounting import periods
from app.modules.accounts.deps import StaffContext
from app.modules.accounts.models import User
from app.modules.schools.models import ClassLevel, Term

from .invoicing import GenerationReport, generate_term_invoices
from .models import BillingRun, BillingRunStatus

logger = logging.getLogger(__name__)

TASK = "fees.generate_bills"


def report_dict(report: GenerationReport) -> dict[str, Any]:
    """The result as the screen shows it (GenerationOut), ready to store as JSON."""
    return {
        "dry_run": report.dry_run,
        "invoices": report.invoices,
        "total_kobo": report.total_kobo,
        "discounts_kobo": report.discounts_kobo,
        "already_invoiced": report.already_invoiced,
        "notices_queued": report.notices_queued,
        "no_fees_set": list(report.no_fees_set),
        "by_class": [
            {
                "class_level_id": str(c.class_level_id),
                "class_name": c.class_name,
                "invoices": c.invoices,
                "total_kobo": c.total_kobo,
            }
            for c in report.by_class
        ],
        "sample": [
            {
                "student_id": str(d.student_id),
                "admission_number": d.admission_number,
                "student_name": d.student_name,
                "class_name": d.class_name,
                "total_kobo": d.total_kobo,
                "lines": [
                    {"kind": ln.kind.value, "description": ln.description, "amount_kobo": ln.amount_kobo}
                    for ln in d.lines
                ],
            }
            for d in report.sample
        ],
    }


def start(
    session: Session,
    ctx: StaffContext,
    *,
    term_id: uuid.UUID,
    due_date: date | None,
    class_level_ids: set[uuid.UUID] | None,
    notify_parents: bool,
) -> BillingRun:
    term = get_or_404(session, Term, term_id, label="Term")
    if periods.term_is_closed(session, ctx.school_id, term):
        raise Conflict("That term is closed. Ask the proprietor to re-open it to bill it again.", code="books_closed")
    if class_level_ids:
        found = set(session.scalars(select(ClassLevel.id).where(ClassLevel.id.in_(class_level_ids))))
        if found != class_level_ids:
            raise DomainError("Unknown class.", field="class_level_ids")
    run = BillingRun(
        id=uuid.uuid4(),
        school_id=ctx.school_id,
        term_id=term.id,
        params={
            "due_date": due_date.isoformat() if due_date else None,
            "class_level_ids": sorted(str(c) for c in class_level_ids) if class_level_ids else None,
            "notify_parents": notify_parents,
        },
        requested_by=ctx.user.id,
    )
    session.add(run)
    outbox.enqueue(session, TASK, {"school_id": str(ctx.school_id), "run_id": str(run.id)})
    session.commit()
    return run


def _finish(session: Session, run_id: uuid.UUID, status: BillingRunStatus, **values: Any) -> None:
    session.execute(
        update(BillingRun)
        .where(BillingRun.id == run_id, BillingRun.status == BillingRunStatus.PENDING)
        .values(status=status, finished_at=datetime.now(UTC), **values)
    )


def run(session: Session, run_id: uuid.UUID) -> BillingRunStatus:
    """The background job: make the bills and mark the run done in the same transaction. Safe to run twice."""
    found = session.scalar(select(BillingRun).where(BillingRun.id == run_id))
    if found is None or found.status != BillingRunStatus.PENDING:
        return found.status if found else BillingRunStatus.FAILED
    user = session.get(User, found.requested_by)
    if user is None:
        _finish(session, run_id, BillingRunStatus.FAILED, error="The person who asked no longer has an account.")
        session.commit()
        return BillingRunStatus.FAILED
    ctx = StaffContext(user=user, session_id=uuid.uuid4(), meta=RequestMeta(ip="background"), school_id=found.school_id)
    params = found.params
    try:
        report = generate_term_invoices(
            session,
            ctx,
            term_id=found.term_id,
            due_date=date.fromisoformat(params["due_date"]) if params.get("due_date") else None,
            class_level_ids={uuid.UUID(c) for c in params["class_level_ids"]}
            if params.get("class_level_ids")
            else None,
            dry_run=False,
            notify_parents=bool(params.get("notify_parents")),
            before_commit=lambda r: _finish(session, run_id, BillingRunStatus.DONE, report=report_dict(r)),
        )
    except Exception as e:
        session.rollback()
        if isinstance(e, DomainError):
            message = e.message
        else:
            logger.exception("Billing run %s failed", run_id)
            message = "Couldn't make the bills. Please try again."
        _finish(session, run_id, BillingRunStatus.FAILED, error=message[:300])
        session.commit()
        return BillingRunStatus.FAILED
    # Nothing new to bill: the generator made no bills (and so didn't commit); the run is still done
    _finish(session, run_id, BillingRunStatus.DONE, report=report_dict(report))
    session.commit()
    if report.invoices >= BULK_ROWS:  # payments and reports follow
        refresh_statistics(session, ("invoices", "invoice_lines", "allocations"))
    return BillingRunStatus.DONE


def get(session: Session, run_id: uuid.UUID) -> BillingRun:
    found = session.scalar(select(BillingRun).where(BillingRun.id == run_id).execution_options(populate_existing=True))
    if found is None:
        raise NotFound("Billing run not found.")
    return found


def run_school(school_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The task's entry point: its own session, scoped to the school."""
    from app.core.db import SessionLocal
    from app.core.tenancy import scope_to_school

    with SessionLocal() as session:
        scope_to_school(session, school_id)
        return run(session, run_id).value
