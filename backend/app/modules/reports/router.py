import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.clock import lagos_today
from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES
from app.core.pagination import PageParams, page_params
from app.core.ratelimit import get_limiter
from app.core.repo import get_or_404
from app.modules.accounts.deps import HeadOffice, StaffContext, require, require_head_office
from app.modules.accounts.permissions import Capability
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.payments.models import PaymentMethod
from app.modules.schools.models import ClassLevel, School
from app.modules.students.models import Student, StudentStatus

from . import exports, fee_status, group, insights, service, signals
from .service import AgingBucket, DebtorFilters, DebtorSort

router = APIRouter(prefix="/reports", tags=["reports"], responses=ERROR_RESPONSES)
VIEW = Depends(require(Capability.REPORTS_VIEW))
EXPORT_RATE = "60/hour"


class ClassRowOut(BaseModel):
    class_level_id: uuid.UUID | None
    class_name: str
    invoices: int
    billed_kobo: int
    collected_kobo: int
    outstanding_kobo: int
    students_owing: int


class DayTotalOut(BaseModel):
    day: date
    amount_kobo: int
    payments: int


class MethodTotalOut(BaseModel):
    method: PaymentMethod
    amount_kobo: int
    payments: int


class AgingRowOut(BaseModel):
    bucket: AgingBucket
    amount_kobo: int
    invoices: int


class OverviewOut(BaseModel):
    term_id: uuid.UUID
    term_label: str
    term_start: date
    term_end: date
    invoices: int
    billed_kobo: int
    collected_kobo: int
    outstanding_kobo: int
    total_outstanding_kobo: int
    older_outstanding_kobo: int
    students_owing: int
    credit_kobo: int
    received_today_kobo: int
    received_7_days_kobo: int
    received_this_term_kobo: int
    pending_approval: int
    pending_approval_kobo: int
    by_class: list[ClassRowOut]
    by_day: list[DayTotalOut]
    by_method: list[MethodTotalOut]
    aging: list[AgingRowOut]


class DebtorOut(BaseModel):
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
    on_plan: bool


class DebtorsOut(BaseModel):
    items: list[DebtorOut]
    total: int
    limit: int
    offset: int
    total_owed_kobo: int


class CollectionsOut(BaseModel):
    start: date
    end: date
    received_kobo: int
    reversed_kobo: int
    net_kobo: int
    payments: int
    by_day: list[DayTotalOut]
    by_method: list[MethodTotalOut]


def debtor_filters(
    class_level_id: uuid.UUID | None = None,
    min_owed_kobo: int = Query(default=1, ge=1, le=10**13),
    overdue_only: bool = False,
    search: str = Query(default="", max_length=100),
    sort: DebtorSort = DebtorSort.OWED,
) -> DebtorFilters:
    return DebtorFilters(class_level_id, min_owed_kobo, overdue_only, search, sort)


def date_range(
    start: date | None = Query(default=None, description="Lagos date; default: 30 days ago"),
    end: date | None = Query(default=None, description="Lagos date, inclusive; default: today"),
) -> tuple[date, date]:
    end = end or lagos_today()
    return start or end - timedelta(days=29), end


@router.get("/overview", response_model=OverviewOut, summary="Billed, collected and outstanding for a term")
def get_overview(
    term_id: uuid.UUID | None = Query(default=None, description="Default: the current term"),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> OverviewOut:
    return OverviewOut.model_validate(service.overview(db, term_id), from_attributes=True)


@router.get("/debtors", response_model=DebtorsOut, summary="Students who owe, most first")
def get_debtors(
    filters: DebtorFilters = Depends(debtor_filters),
    params: PageParams = Depends(page_params),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> DebtorsOut:
    page = service.debtors(db, filters, params)
    return DebtorsOut(
        items=[DebtorOut.model_validate(d, from_attributes=True) for d in page.items],
        total=page.total,
        limit=params.limit,
        offset=params.offset,
        total_owed_kobo=page.total_owed_kobo,
    )


@router.get("/collections", response_model=CollectionsOut, summary="Money received over a period")
def get_collections(
    period: tuple[date, date] = Depends(date_range), ctx: StaffContext = VIEW, db: Session = Depends(get_db)
) -> CollectionsOut:
    return CollectionsOut.model_validate(service.collections(db, *period), from_attributes=True)


class TermCurveOut(BaseModel):
    term_id: uuid.UUID
    label: str
    start: date
    billed_kobo: int = Field(description="Term bills (not voided)")
    days: int = Field(description="Days the curve covers: to today for the current term, else the whole term")
    collected_kobo: list[int] = Field(description="Cumulative collected on the term's bills, one per day from day 0")


class WindowOut(BaseModel):
    days: int
    due_kobo: int = Field(description="Falling due in the window and still unpaid (bills, and plan instalments only)")
    pace_in_kobo: int = Field(description="Money in over the window at the pace of the last 30 days")
    pace_out_kobo: int = Field(description="Approved spending over the window at the pace of the last 30 days")


class InsightsOut(BaseModel):
    today: date
    current: TermCurveOut | None
    previous: TermCurveOut | None = Field(description="The term before the current one, for comparison")
    overdue_kobo: int = Field(description="Owed now and past due (on a plan: only the missed instalments)")
    windows: list[WindowOut] = Field(description="The next 30, 60 and 90 days")


@router.get(
    "/insights", response_model=InsightsOut, summary="Collection curve against last term, and the cash forecast"
)
def get_insights(ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> InsightsOut:
    return InsightsOut.model_validate(insights.insights(db, lagos_today()), from_attributes=True)


def _download(content: bytes, kind: Literal["xlsx", "pdf"], name: str) -> Response:
    return Response(
        content,
        media_type=exports.XLSX if kind == "xlsx" else exports.PDF,
        headers={"Content-Disposition": f'attachment; filename="{name}.{kind}"', "Cache-Control": "no-store"},
    )


def _school_name(db: Session, ctx: StaffContext) -> str:
    school = db.get(School, ctx.school_id)
    return school.name if school else ""


@router.get(
    "/debtors/export",
    summary="Download the debtors list (Excel or PDF)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX: {}, exports.PDF: {}}}},
)
def export_debtors(
    format: Literal["xlsx", "pdf"] = "xlsx",
    filters: DebtorFilters = Depends(debtor_filters),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> Response:
    get_limiter().hit("reports-export:school", EXPORT_RATE, str(ctx.school_id))
    scope = "All classes"
    if filters.class_level_id:
        scope = get_or_404(db, ClassLevel, filters.class_level_id, label="Class", field="class_level_id").name
    if filters.overdue_only:
        scope += ", overdue only"
    rows = service.all_debtors(db, filters)
    school = _school_name(db, ctx)
    content = (exports.debtors_xlsx if format == "xlsx" else exports.debtors_pdf)(school, scope, rows)
    # The list carries parents' names and phone numbers: who downloaded it is recorded
    record(
        db,
        AuditAction.REPORT_EXPORTED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        changes={"report": "debtors", "format": format, "rows": len(rows), "scope": scope},
    )
    db.commit()
    return _download(content, format, f"debtors-{lagos_today().isoformat()}")


@router.get(
    "/collections/export",
    summary="Download money received over a period (Excel or PDF)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX: {}, exports.PDF: {}}}},
)
def export_collections(
    format: Literal["xlsx", "pdf"] = "xlsx",
    period: tuple[date, date] = Depends(date_range),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> Response:
    get_limiter().hit("reports-export:school", EXPORT_RATE, str(ctx.school_id))
    start, end = period
    summary = service.collections(db, start, end)
    school = _school_name(db, ctx)
    if format == "xlsx":
        payments = service.collected_payments(db, start, end)
        content, rows = exports.collections_xlsx(school, summary, payments), len(payments)
    else:
        content, rows = exports.collections_pdf(school, summary), summary.payments
    record(
        db,
        AuditAction.REPORT_EXPORTED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        changes={"report": "collections", "format": format, "rows": rows, "from": start, "to": end},
    )
    db.commit()
    return _download(content, format, f"money-received-{start.isoformat()}-to-{end.isoformat()}")


@router.get(
    "/fee-status/export",
    summary="Download each pupil's fees for a term as paid, part paid or unpaid (Excel or PDF)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX: {}, exports.PDF: {}}}},
)
def export_fee_status(
    format: Literal["xlsx", "pdf"] = "xlsx",
    term_id: uuid.UUID | None = Query(default=None, description="Defaults to the current term"),
    class_level_id: uuid.UUID | None = Query(default=None, description="One class; all classes if left out"),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> Response:
    get_limiter().hit("reports-export:school", EXPORT_RATE, str(ctx.school_id))
    report = fee_status.fee_status(db, term_id or service.current_term_id(db), class_level_id)
    school = _school_name(db, ctx)
    content = (exports.fee_status_xlsx if format == "xlsx" else exports.fee_status_pdf)(school, report)
    record(
        db,
        AuditAction.REPORT_EXPORTED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        changes={"report": "fee_status", "format": format, "rows": len(report.pupils), "scope": report.scope},
    )
    db.commit()
    slug = "all-classes" if class_level_id is None else report.scope.lower().replace(" ", "-")
    return _download(content, format, f"fee-status-{slug}-{lagos_today().isoformat()}")


class FeeLineOut(BaseModel):
    name: str
    billed_kobo: int
    paid_kobo: int
    left_kobo: int
    status: Literal["Paid", "Part paid", "Unpaid"]


class FeeBreakdownOut(BaseModel):
    term_label: str | None = Field(description="The term the fees are for; null when no term is current")
    fees: list[FeeLineOut] = Field(
        description="This term's fees in bill order (payments not marked for a fee cover tuition first)"
    )
    earlier_left_kobo: int = Field(description="Still owed from earlier terms, balances brought forward and one-offs")
    total_left_kobo: int


def breakdown_out(breakdown: fee_status.FeeBreakdown) -> FeeBreakdownOut:
    return FeeBreakdownOut(
        term_label=breakdown.term_label,
        fees=[FeeLineOut.model_validate(f, from_attributes=True) for f in breakdown.fees],
        earlier_left_kobo=breakdown.earlier_left_kobo,
        total_left_kobo=breakdown.total_left_kobo,
    )


@router.get(
    "/students/{student_id}/fee-breakdown",
    response_model=FeeBreakdownOut,
    summary="A pupil's fees for a term one by one (charged, paid, left), plus earlier debts",
)
def student_fee_breakdown(
    student_id: uuid.UUID,
    term_id: uuid.UUID | None = Query(default=None, description="Defaults to the current term"),
    ctx: StaffContext = Depends(require(Capability.FEES_VIEW)),
    db: Session = Depends(get_db),
) -> FeeBreakdownOut:
    get_or_404(db, Student, student_id, label="Student")
    return breakdown_out(fee_status.fee_breakdowns(db, [student_id], term_id)[student_id])


class SignalItemOut(BaseModel):
    day: date
    what: str
    amount_kobo: int | None
    who: str = Field(description="Who recorded it, when known")
    link: str = Field(description="A console path to look at, or empty")


class SignalOut(BaseModel):
    key: str
    title: str
    why: str
    serious: bool
    items: list[SignalItemOut]


class SignalsOut(BaseModel):
    start: date
    end: date
    count: int
    signals: list[SignalOut] = Field(description="Only the kinds with something to look at")


@router.get(
    "/unusual-activity",
    response_model=SignalsOut,
    summary="Things worth a look: reversals, late cash, short counts, reduced bills, write-offs (proprietor)",
)
def unusual_activity(
    days: int = Query(default=30, ge=1, le=signals.MAX_DAYS),
    ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)),
    db: Session = Depends(get_db),
) -> SignalsOut:
    found = signals.signals(db, ctx.school_id, days)
    return SignalsOut(
        start=found.start,
        end=found.end,
        count=found.count,
        signals=[SignalOut.model_validate(s, from_attributes=True) for s in found.signals],
    )


# --- Head office of a school group -------------------------------------------------------------------

HEAD_OFFICE = Depends(require_head_office(Capability.REPORTS_VIEW))


class BranchFiguresOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    school_id: uuid.UUID | None = Field(description="None on the totals row")
    name: str
    term_label: str = Field(description="The branch's current term; empty if none is set")
    pupils: int
    billed_kobo: int
    collected_kobo: int
    outstanding_kobo: int
    older_outstanding_kobo: int
    total_outstanding_kobo: int
    students_owing: int
    credit_kobo: int
    received_today_kobo: int
    received_7_days_kobo: int
    received_30_days_kobo: int
    pending_approval: int
    pending_approval_kobo: int


class GroupDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    amount_kobo: int


class GroupOverviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    group_name: str
    today: date
    branches: list[BranchFiguresOut]
    totals: BranchFiguresOut
    by_day: list[GroupDayOut] = Field(description="Money received across the group, each of the last 30 days")


@router.get(
    "/group",
    response_model=GroupOverviewOut,
    summary="Head office: every branch's figures side by side, and the group's totals",
)
def get_group_overview(ho: HeadOffice = HEAD_OFFICE, db: Session = Depends(get_db)) -> GroupOverviewOut:
    return GroupOverviewOut.model_validate(group.overview(db, ho.group_id, ho.branch_ids))


@router.get(
    "/group/export",
    summary="Head office: download every branch's figures (Excel)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX: {}}}},
)
def export_group_overview(ho: HeadOffice = HEAD_OFFICE, db: Session = Depends(get_db)) -> Response:
    get_limiter().hit("reports-export:group", EXPORT_RATE, str(ho.group_id))
    report = group.overview(db, ho.group_id, ho.branch_ids)
    record(
        db,
        AuditAction.REPORT_EXPORTED,
        school_id=None,
        actor=ho.actor,
        meta=ho.meta,
        changes={"report": "group", "group": str(ho.group_id), "branches": len(report.branches)},
    )
    db.commit()
    return _download(exports.group_xlsx(report), "xlsx", f"all-branches-{report.today.isoformat()}")
