import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES
from app.core.pagination import Page, PageParams, page_params, paginate_rows
from app.core.repo import get_or_404
from app.modules.accounts.deps import StaffContext, require
from app.modules.accounts.permissions import Capability
from app.modules.schools.models import Term
from app.modules.students.models import Student

from . import charges, invoicing, plan_offers, plans, setup_service, writeoffs
from .models import (
    DiscountKind,
    DiscountRule,
    FeeCategory,
    Invoice,
    InvoiceKind,
    LineKind,
    PlanChargeKind,
    PlanEligibility,
    PlanTemplate,
    WriteOffStatus,
)

router = APIRouter(tags=["fees"], responses=ERROR_RESPONSES)
FEES_VIEW = Depends(require(Capability.FEES_VIEW))
FEES_MANAGE = Depends(require(Capability.FEES_MANAGE))
INVOICES_ISSUE = Depends(require(Capability.INVOICES_ISSUE))

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]
Kobo = Annotated[int, Field(gt=0, le=setup_service.MAX_AMOUNT_KOBO, description="Amount in kobo (₦1 = 100)")]
InvoiceStatus = Literal["unpaid", "part_paid", "paid", "void", "outstanding"]


# --- Schemas ---------------------------------------------------------------------------------------


class FeeItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: FeeCategory
    is_active: bool
    default_amount_kobo: int | None = Field(description="Standard price for charging one pupil (uniform, books)")


class FeeItemIn(BaseModel):
    name: Name
    category: FeeCategory = FeeCategory.OTHER
    default_amount_kobo: Kobo | None = None


class FeeItemUpdateIn(BaseModel):
    name: Name | None = None
    category: FeeCategory | None = None
    is_active: bool | None = None
    default_amount_kobo: Kobo | None = Field(default=None, description="Send null to clear the standard price")


class ClassChargeIn(BaseModel):
    fee_item_id: uuid.UUID | None = Field(default=None, description="An existing fee, or name a new one")
    new_item_name: Name | None = None
    amount_kobo: Kobo
    class_level_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    excluded_student_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    dry_run: bool = True


class ChargeStudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    student_id: uuid.UUID
    full_name: str
    admission_number: str
    class_name: str
    billed: bool
    excluded: bool


class ClassChargeOut(BaseModel):
    dry_run: bool
    item_name: str
    amount_kobo: int
    charged_now: int
    total_kobo: int
    students: list[ChargeStudentOut]


class ExemptionIn(BaseModel):
    term_id: uuid.UUID
    fee_item_id: uuid.UUID
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class ExemptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    term_id: uuid.UUID
    fee_item_id: uuid.UUID
    item_name: str
    reason: str


class ExemptionChangeOut(BaseModel):
    changed_kobo: int = Field(description="Taken off (exempt) or put back on (unexempt) the pupil's bill")


class PupilChargeIn(BaseModel):
    fee_item_id: uuid.UUID | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    unit_kobo: Kobo | None = Field(default=None, description="Default: the item's standard price")
    quantity: int = Field(default=1, ge=1, le=100)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class PupilChargeOut(BaseModel):
    invoice_id: uuid.UUID


class ScheduleEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fee_item_id: uuid.UUID
    class_level_id: uuid.UUID
    amount_kobo: int
    is_optional: bool


class ScheduleEntryIn(BaseModel):
    fee_item_id: uuid.UUID
    class_level_id: uuid.UUID
    amount_kobo: Kobo | None = Field(default=None, description="null removes this fee for the class")
    is_optional: bool = False


class ScheduleIn(BaseModel):
    entries: list[ScheduleEntryIn] = Field(min_length=1, max_length=2000)


class CopyScheduleIn(BaseModel):
    from_term_id: uuid.UUID


class CopyResult(BaseModel):
    copied: int


class ChoiceOut(BaseModel):
    student_id: uuid.UUID
    fee_item_id: uuid.UUID


class ChoicesIn(BaseModel):
    fee_item_id: uuid.UUID
    student_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    selected: bool


class ChangedCount(BaseModel):
    changed: int


class DiscountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: DiscountKind
    value: int = Field(description="Basis points for percent (1000 = 10%), kobo for fixed")
    rule: DiscountRule
    fee_item_id: uuid.UUID | None
    is_active: bool


class DiscountIn(BaseModel):
    name: Name
    kind: DiscountKind
    value: int = Field(gt=0, le=setup_service.MAX_AMOUNT_KOBO)
    rule: DiscountRule = DiscountRule.MANUAL
    fee_item_id: uuid.UUID | None = None


class DiscountUpdateIn(BaseModel):
    name: Name | None = None
    value: int | None = Field(default=None, gt=0, le=setup_service.MAX_AMOUNT_KOBO)
    is_active: bool | None = None


class GiveDiscountIn(BaseModel):
    discount_id: uuid.UUID


class GenerateIn(BaseModel):
    due_date: date | None = None
    class_level_ids: list[uuid.UUID] | None = Field(default=None, description="Only these classes (default: all)")
    dry_run: bool = True
    notify_parents: bool = Field(default=False, description="Send each primary parent a fee notice")


class DraftLineOut(BaseModel):
    kind: LineKind
    description: str
    amount_kobo: int


class DraftInvoiceOut(BaseModel):
    student_id: uuid.UUID
    admission_number: str
    student_name: str
    class_name: str
    total_kobo: int
    lines: list[DraftLineOut]


class ClassTotalOut(BaseModel):
    class_level_id: uuid.UUID
    class_name: str
    invoices: int
    total_kobo: int


class GenerationOut(BaseModel):
    dry_run: bool
    invoices: int
    total_kobo: int
    discounts_kobo: int
    already_invoiced: int
    no_fees_set: list[str] = Field(description="Classes with students but no fees set for the term")
    by_class: list[ClassTotalOut]
    sample: list[DraftInvoiceOut]
    notices_queued: int = Field(description="Fee notices queued for parents (when notify_parents)")


class StudentBrief(BaseModel):
    id: uuid.UUID
    admission_number: str
    full_name: str


class InvoiceSummaryOut(BaseModel):
    id: uuid.UUID
    number: int
    kind: InvoiceKind
    term_id: uuid.UUID | None
    student: StudentBrief
    issued_at: datetime
    due_date: date | None
    total_kobo: int
    paid_kobo: int
    balance_kobo: int
    status: Literal["unpaid", "part_paid", "paid", "void"]


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    kind: LineKind
    description: str
    amount_kobo: int
    reason: str | None
    created_at: datetime


class InstalmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    number: int
    due_date: date
    amount_kobo: int
    paid_kobo: int
    status: Literal["paid", "part_paid", "overdue", "due_today", "upcoming"]


class PaymentPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    note: str
    created_at: datetime
    covered_kobo: int
    paid_on_plan_kobo: int
    behind_kobo: int
    next_due: date | None
    next_amount_kobo: int
    next_number: int | None
    instalments: list[InstalmentOut]


class InvoiceOut(InvoiceSummaryOut):
    void_reason: str | None
    lines: list[InvoiceLineOut]
    plan: PaymentPlanOut | None = Field(description="The live payment plan, if the bill is being paid in instalments")


class InstalmentIn(BaseModel):
    due_date: date
    amount_kobo: Kobo


class PaymentPlanIn(BaseModel):
    instalments: list[InstalmentIn] = Field(min_length=2, max_length=12)
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class CancelPlanIn(BaseModel):
    reason: Reason


class AddLineIn(BaseModel):
    kind: Literal[LineKind.FEE, LineKind.ADJUSTMENT]
    description: Description
    amount_kobo: int = Field(description="Kobo; negative adjustments reduce the invoice")
    reason: Reason
    fee_item_id: uuid.UUID | None = None


class VoidIn(BaseModel):
    reason: Reason


class NewLineIn(BaseModel):
    description: Description
    amount_kobo: Kobo
    fee_item_id: uuid.UUID | None = None


class NewInvoiceIn(BaseModel):
    student_id: uuid.UUID
    kind: Literal[InvoiceKind.OPENING_BALANCE, InvoiceKind.ADHOC]
    lines: list[NewLineIn] = Field(min_length=1, max_length=50)
    due_date: date | None = None


class AccountOut(BaseModel):
    student: StudentBrief
    billed_kobo: int
    paid_kobo: int
    outstanding_kobo: int
    invoices: list[InvoiceSummaryOut]


class TermSummaryOut(BaseModel):
    invoices: int
    billed_kobo: int
    paid_kobo: int
    outstanding_kobo: int
    students_enrolled: int
    students_invoiced: int


# --- Helpers ---------------------------------------------------------------------------------------


def _brief(student: Student) -> StudentBrief:
    return StudentBrief(id=student.id, admission_number=student.admission_number, full_name=student.full_name)


def _summary(invoice: Invoice, student: Student, total: int, paid: int) -> InvoiceSummaryOut:
    amounts = invoicing.InvoiceAmounts(int(total), int(paid))
    return InvoiceSummaryOut(
        id=invoice.id,
        number=invoice.number,
        kind=invoice.kind,
        term_id=invoice.term_id,
        student=_brief(student),
        issued_at=invoice.issued_at,
        due_date=invoice.due_date,
        total_kobo=amounts.total_kobo,
        paid_kobo=amounts.paid_kobo,
        balance_kobo=amounts.balance_kobo,
        status=invoicing.status_of(invoice, amounts),
    )


def _invoice_out(db: Session, invoice_id: uuid.UUID) -> InvoiceOut:
    invoice, student, amounts = invoicing.invoice_with_lines(db, invoice_id)
    summary = _summary(invoice, student, amounts.total_kobo, amounts.paid_kobo)
    plan = plans.plan_views(db, [invoice.id]).get(invoice.id)
    return InvoiceOut(
        **summary.model_dump(),
        void_reason=invoice.void_reason,
        lines=[InvoiceLineOut.model_validate(line) for line in invoice.lines],
        plan=PaymentPlanOut.model_validate(plan) if plan else None,
    )


# --- Fee items -------------------------------------------------------------------------------------


@router.get("/fee-items", response_model=list[FeeItemOut], summary="Things the school charges for")
def list_fee_items(ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> list[FeeItemOut]:
    return [FeeItemOut.model_validate(i) for i in setup_service.list_fee_items(db)]


@router.post("/fee-items", response_model=FeeItemOut, status_code=status.HTTP_201_CREATED, summary="Add a fee item")
def create_fee_item(body: FeeItemIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)) -> FeeItemOut:
    return FeeItemOut.model_validate(
        setup_service.create_fee_item(
            db, ctx, name=body.name, category=body.category, default_amount_kobo=body.default_amount_kobo
        )
    )


@router.patch("/fee-items/{item_id}", response_model=FeeItemOut, summary="Rename, recategorise or retire a fee")
def update_fee_item(
    item_id: uuid.UUID, body: FeeItemUpdateIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> FeeItemOut:
    return FeeItemOut.model_validate(
        setup_service.update_fee_item(
            db,
            ctx,
            item_id,
            name=body.name,
            category=body.category,
            is_active=body.is_active,
            set_price="default_amount_kobo" in body.model_fields_set,
            default_amount_kobo=body.default_amount_kobo,
        )
    )


# --- Schedules and optional items -----------------------------------------------------------------


@router.get(
    "/terms/{term_id}/fee-schedule", response_model=list[ScheduleEntryOut], summary="Fee amounts per class for a term"
)
def get_schedule(
    term_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)
) -> list[ScheduleEntryOut]:
    return [ScheduleEntryOut.model_validate(s) for s in setup_service.schedule_for_term(db, term_id)]


class MissingFeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    item_name: str
    class_names: list[str]
    bills: int
    total_kobo: int


class BillSyncOut(BaseModel):
    dry_run: bool
    fees: list[MissingFeeOut]
    bills_changed: int
    total_kobo: int


class ScheduleSavedOut(BaseModel):
    entries: list[ScheduleEntryOut]
    bills: BillSyncOut = Field(
        description="Fees added to the term's bills already sent, because their class now has them"
    )


@router.put(
    "/terms/{term_id}/fee-schedule",
    response_model=ScheduleSavedOut,
    summary="Set amounts for fee/class pairs (null removes one); new class fees go onto bills already sent",
)
def set_schedule(
    term_id: uuid.UUID, body: ScheduleIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> ScheduleSavedOut:
    entries = [
        setup_service.ScheduleEntry(e.fee_item_id, e.class_level_id, e.amount_kobo, e.is_optional) for e in body.entries
    ]
    saved, report = charges.save_schedule(db, ctx, term_id, entries)
    return ScheduleSavedOut(entries=[ScheduleEntryOut.model_validate(s) for s in saved], bills=_bill_sync_out(report))


@router.post(
    "/terms/{term_id}/fee-schedule/copy",
    response_model=CopyResult,
    summary="Copy another term's amounts (amounts already set here are kept)",
)
def copy_schedule(
    term_id: uuid.UUID, body: CopyScheduleIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> CopyResult:
    return CopyResult(copied=setup_service.copy_schedule(db, ctx, from_term_id=body.from_term_id, to_term_id=term_id))


@router.get(
    "/terms/{term_id}/optional-fees",
    response_model=list[ChoiceOut],
    summary="Which students take which optional items this term",
)
def list_choices(
    term_id: uuid.UUID,
    ctx: StaffContext = FEES_VIEW,
    db: Session = Depends(get_db),
    fee_item_id: uuid.UUID | None = None,
) -> list[ChoiceOut]:
    get_or_404(db, Term, term_id, label="Term")
    return [
        ChoiceOut(student_id=s, fee_item_id=f)
        for s, f in db.execute(setup_service.choices_for_term(db, term_id, fee_item_id))
    ]


@router.put(
    "/terms/{term_id}/optional-fees",
    response_model=ChangedCount,
    summary="Opt students in or out of an optional item (affects invoices generated afterwards)",
)
def set_choices(
    term_id: uuid.UUID, body: ChoicesIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> ChangedCount:
    return ChangedCount(
        changed=setup_service.set_choices(
            db, ctx, term_id=term_id, fee_item_id=body.fee_item_id, student_ids=body.student_ids, selected=body.selected
        )
    )


# --- Discounts -------------------------------------------------------------------------------------


@router.get("/discounts", response_model=list[DiscountOut], summary="Discounts and scholarships")
def list_discounts(ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> list[DiscountOut]:
    return [DiscountOut.model_validate(d) for d in setup_service.list_discounts(db)]


@router.post("/discounts", response_model=DiscountOut, status_code=status.HTTP_201_CREATED, summary="Add a discount")
def create_discount(body: DiscountIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)) -> DiscountOut:
    return DiscountOut.model_validate(
        setup_service.create_discount(
            db, ctx, name=body.name, kind=body.kind, value=body.value, rule=body.rule, fee_item_id=body.fee_item_id
        )
    )


@router.patch("/discounts/{discount_id}", response_model=DiscountOut, summary="Change or retire a discount")
def update_discount(
    discount_id: uuid.UUID, body: DiscountUpdateIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> DiscountOut:
    return DiscountOut.model_validate(
        setup_service.update_discount(db, ctx, discount_id, name=body.name, value=body.value, is_active=body.is_active)
    )


@router.get("/students/{student_id}/discounts", response_model=list[DiscountOut], summary="A student's discounts")
def get_student_discounts(
    student_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)
) -> list[DiscountOut]:
    get_or_404(db, Student, student_id, label="Student")
    return [DiscountOut.model_validate(d) for d in setup_service.student_discounts(db, student_id)]


@router.post(
    "/students/{student_id}/discounts",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Give a student a discount (applies to invoices generated afterwards)",
)
def give_discount(
    student_id: uuid.UUID, body: GiveDiscountIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> None:
    setup_service.give_discount(db, ctx, student_id, body.discount_id)


@router.delete(
    "/students/{student_id}/discounts/{discount_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a discount away",
)
def take_discount(
    student_id: uuid.UUID, discount_id: uuid.UUID, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> None:
    setup_service.take_discount(db, ctx, student_id, discount_id)


# --- Invoices --------------------------------------------------------------------------------------


@router.post(
    "/terms/{term_id}/invoices/generate",
    response_model=GenerationOut,
    summary="Invoice every enrolled student for the term (dry_run=true to preview); safe to re-run",
)
def generate(
    term_id: uuid.UUID, body: GenerateIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> GenerationOut:
    report = invoicing.generate_term_invoices(
        db,
        ctx,
        term_id=term_id,
        due_date=body.due_date,
        class_level_ids=set(body.class_level_ids) if body.class_level_ids else None,
        dry_run=body.dry_run,
        notify_parents=body.notify_parents,
    )
    return GenerationOut(
        dry_run=report.dry_run,
        invoices=report.invoices,
        total_kobo=report.total_kobo,
        discounts_kobo=report.discounts_kobo,
        already_invoiced=report.already_invoiced,
        notices_queued=report.notices_queued,
        no_fees_set=report.no_fees_set,
        by_class=[ClassTotalOut(**c.__dict__) for c in report.by_class],
        sample=[
            DraftInvoiceOut(
                student_id=d.student_id,
                admission_number=d.admission_number,
                student_name=d.student_name,
                class_name=d.class_name,
                total_kobo=d.total_kobo,
                lines=[
                    DraftLineOut(kind=ln.kind, description=ln.description, amount_kobo=ln.amount_kobo) for ln in d.lines
                ],
            )
            for d in report.sample
        ],
    )


@router.get("/terms/{term_id}/billing-summary", response_model=TermSummaryOut, summary="Billed, paid and owed")
def billing_summary(term_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> TermSummaryOut:
    return TermSummaryOut(**invoicing.term_summary(db, term_id).__dict__)


@router.get("/invoices", response_model=Page[InvoiceSummaryOut], summary="Invoices with what's been paid on each")
def list_invoices(
    ctx: StaffContext = FEES_VIEW,
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
    term_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    class_level_id: uuid.UUID | None = None,
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=100),
) -> Page[InvoiceSummaryOut]:
    stmt = invoicing.invoices_query(
        term_id=term_id, student_id=student_id, class_level_id=class_level_id, status=status_filter, search=search
    )
    rows, total = paginate_rows(db, stmt, params)
    return Page(items=[_summary(*row) for row in rows], total=total, limit=params.limit, offset=params.offset)


@router.post(
    "/invoices",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record an opening balance (debt from before joining) or a one-off charge",
)
def create_invoice(body: NewInvoiceIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)) -> InvoiceOut:
    invoice = invoicing.create_invoice(
        db,
        ctx,
        student_id=body.student_id,
        kind=body.kind,
        due_date=body.due_date,
        lines=[
            invoicing.DraftLine(LineKind.FEE, ln.description, ln.amount_kobo, fee_item_id=ln.fee_item_id)
            for ln in body.lines
        ],
    )
    return _invoice_out(db, invoice.id)


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut, summary="An invoice with its lines")
def get_invoice(invoice_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> InvoiceOut:
    return _invoice_out(db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/lines",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a charge or a correction (lines are never edited)",
)
def add_line(
    invoice_id: uuid.UUID, body: AddLineIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> InvoiceOut:
    invoicing.add_line(
        db,
        ctx,
        invoice_id,
        kind=body.kind,
        description=body.description,
        amount_kobo=body.amount_kobo,
        reason=body.reason,
        fee_item_id=body.fee_item_id,
    )
    return _invoice_out(db, invoice_id)


class BillSyncIn(BaseModel):
    dry_run: bool = True


@router.post(
    "/terms/{term_id}/bills/sync",
    response_model=BillSyncOut,
    summary="Add fees on the term's fee list that its bills are missing (dry_run to preview)",
)
def sync_bills(
    term_id: uuid.UUID, body: BillSyncIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> BillSyncOut:
    return _bill_sync_out(charges.sync_bills(db, ctx, term_id=term_id, dry_run=body.dry_run))


def _bill_sync_out(report: charges.BillSyncReport) -> BillSyncOut:
    return BillSyncOut(
        dry_run=report.dry_run,
        fees=[MissingFeeOut.model_validate(f) for f in report.fees],
        bills_changed=report.bills_changed,
        total_kobo=report.total_kobo,
    )


@router.post(
    "/terms/{term_id}/class-charges",
    response_model=ClassChargeOut,
    summary="Add a fee to whole classes this term (e.g. WAEC), now and on later bills; dry_run to preview",
)
def class_charge(
    term_id: uuid.UUID, body: ClassChargeIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> ClassChargeOut:
    report = charges.charge_classes(
        db,
        ctx,
        term_id=term_id,
        fee_item_id=body.fee_item_id,
        new_item_name=body.new_item_name,
        amount_kobo=body.amount_kobo,
        class_level_ids=set(body.class_level_ids),
        excluded_student_ids=set(body.excluded_student_ids),
        dry_run=body.dry_run,
    )
    return ClassChargeOut(
        dry_run=report.dry_run,
        item_name=report.item_name,
        amount_kobo=report.amount_kobo,
        charged_now=report.charged_now,
        total_kobo=report.total_kobo,
        students=[ChargeStudentOut.model_validate(s) for s in report.students],
    )


@router.get(
    "/students/{student_id}/exemptions", response_model=list[ExemptionOut], summary="Class fees this pupil doesn't pay"
)
def list_exemptions(
    student_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)
) -> list[ExemptionOut]:
    get_or_404(db, Student, student_id, label="Student")
    return [ExemptionOut.model_validate(e) for e in charges.exemptions(db, student_id)]


@router.post(
    "/students/{student_id}/exemptions",
    response_model=ExemptionChangeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Exempt a pupil from a class fee this term (comes off their bill if already billed)",
)
def add_exemption(
    student_id: uuid.UUID, body: ExemptionIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> ExemptionChangeOut:
    return ExemptionChangeOut(
        changed_kobo=charges.exempt(db, ctx, student_id, body.term_id, body.fee_item_id, body.reason)
    )


@router.delete(
    "/students/{student_id}/exemptions/{fee_item_id}",
    response_model=ExemptionChangeOut,
    summary="Charge the pupil the fee again",
)
def remove_exemption(
    student_id: uuid.UUID,
    fee_item_id: uuid.UUID,
    term_id: uuid.UUID,
    ctx: StaffContext = INVOICES_ISSUE,
    db: Session = Depends(get_db),
) -> ExemptionChangeOut:
    return ExemptionChangeOut(changed_kobo=charges.unexempt(db, ctx, student_id, term_id, fee_item_id))


@router.post(
    "/students/{student_id}/charges",
    response_model=PupilChargeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Charge one pupil for an item (uniform, books), on this term's bill or a one-off bill",
)
def pupil_charge(
    student_id: uuid.UUID, body: PupilChargeIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> PupilChargeOut:
    invoice_id = charges.charge_pupil(
        db,
        ctx,
        student_id,
        fee_item_id=body.fee_item_id,
        description=body.description,
        unit_kobo=body.unit_kobo,
        quantity=body.quantity,
        reason=body.reason,
    )
    return PupilChargeOut(invoice_id=invoice_id)


@router.post(
    "/invoices/{invoice_id}/plan",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Agree to take what's left on a bill in instalments (2 to 12, adding up to what's owed)",
)
def create_payment_plan(
    invoice_id: uuid.UUID, body: PaymentPlanIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> InvoiceOut:
    plans.create_plan(db, ctx, invoice_id, [(i.due_date, i.amount_kobo) for i in body.instalments], body.note)
    return _invoice_out(db, invoice_id)


@router.post("/invoices/{invoice_id}/plan/cancel", response_model=InvoiceOut, summary="End a bill's payment plan")
def cancel_payment_plan(
    invoice_id: uuid.UUID, body: CancelPlanIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> InvoiceOut:
    plans.cancel_plan(db, ctx, invoice_id, body.reason)
    return _invoice_out(db, invoice_id)


@router.post(
    "/invoices/{invoice_id}/void",
    response_model=InvoiceOut,
    summary="Cancel an invoice issued by mistake (only while nothing is paid on it)",
)
def void_invoice(
    invoice_id: uuid.UUID, body: VoidIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> InvoiceOut:
    invoicing.void_invoice(db, ctx, invoice_id, body.reason)
    return _invoice_out(db, invoice_id)


@router.get(
    "/students/{student_id}/account",
    response_model=AccountOut,
    summary="A student's invoices and balance (oldest first)",
)
def student_account(student_id: uuid.UUID, ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> AccountOut:
    student = get_or_404(db, Student, student_id, label="Student")
    rows = db.execute(invoicing.invoices_query(student_id=student.id).order_by(None).order_by(Invoice.number)).all()
    summary = invoicing.account_summaries(db, [student.id])[student.id]
    return AccountOut(
        student=_brief(student),
        billed_kobo=summary.billed_kobo,
        paid_kobo=summary.paid_kobo,
        outstanding_kobo=summary.outstanding_kobo,
        invoices=[_summary(*row) for row in rows],
    )


# --- Writing off debt ------------------------------------------------------------------------------


class WriteOffIn(BaseModel):
    amount_kobo: Annotated[int, Field(gt=0, le=setup_service.MAX_AMOUNT_KOBO)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class WriteOffOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_number: int
    student_id: uuid.UUID
    student_name: str
    admission_number: str
    amount_kobo: int
    reason: str
    status: WriteOffStatus
    requested_by: uuid.UUID
    decided_by: uuid.UUID | None
    rejection_reason: str
    created_at: datetime


class RejectIn(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


def _write_off_out(row: writeoffs.WriteOffRow) -> WriteOffOut:
    w = row.write_off
    return WriteOffOut(
        id=w.id,
        invoice_id=w.invoice_id,
        invoice_number=row.invoice_number,
        student_id=row.student_id,
        student_name=row.student_name,
        admission_number=row.admission_number,
        amount_kobo=w.amount_kobo,
        reason=w.reason,
        status=w.status,
        requested_by=w.requested_by,
        decided_by=w.decided_by,
        rejection_reason=w.rejection_reason,
        created_at=w.created_at,
    )


def _one(db: Session, write_off_id: uuid.UUID) -> WriteOffOut:
    [row] = writeoffs.listing(db, write_off_id=write_off_id)
    return _write_off_out(row)


@router.post(
    "/invoices/{invoice_id}/write-off",
    response_model=WriteOffOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask to write off debt on a bill (the proprietor approves; the proprietor's own applies at once)",
)
def request_write_off(
    invoice_id: uuid.UUID,
    body: WriteOffIn,
    ctx: StaffContext = Depends(require(Capability.PAYMENTS_APPROVE)),
    db: Session = Depends(get_db),
) -> WriteOffOut:
    write_off = writeoffs.request(db, ctx, invoice_id, amount_kobo=body.amount_kobo, reason=body.reason)
    return _one(db, write_off.id)


@router.get("/write-offs", response_model=list[WriteOffOut], summary="Write-offs, newest first")
def list_write_offs(
    write_off_status: WriteOffStatus | None = Query(default=None, alias="status"),
    ctx: StaffContext = Depends(require(Capability.PAYMENTS_VIEW)),
    db: Session = Depends(get_db),
) -> list[WriteOffOut]:
    return [_write_off_out(r) for r in writeoffs.listing(db, status=write_off_status)]


@router.post("/write-offs/{write_off_id}/approve", response_model=WriteOffOut, summary="Approve a write-off")
def approve_write_off(
    write_off_id: uuid.UUID,
    ctx: StaffContext = Depends(require(Capability.DEBTS_WRITE_OFF)),
    db: Session = Depends(get_db),
) -> WriteOffOut:
    writeoffs.approve(db, ctx, write_off_id)
    return _one(db, write_off_id)


@router.post("/write-offs/{write_off_id}/reject", response_model=WriteOffOut, summary="Reject a write-off")
def reject_write_off(
    write_off_id: uuid.UUID,
    body: RejectIn,
    ctx: StaffContext = Depends(require(Capability.DEBTS_WRITE_OFF)),
    db: Session = Depends(get_db),
) -> WriteOffOut:
    writeoffs.reject(db, ctx, write_off_id, body.reason)
    return _one(db, write_off_id)


# --- The school's own instalment plans ---------------------------------------------------------------


class PlanPart(BaseModel):
    percent_bp: Annotated[int, Field(gt=0, le=10_000, description="Share of the bill in basis points (2500 = 25%)")]
    day: Annotated[int, Field(ge=0, le=plan_offers.MAX_DAY, description="Days after the term starts")]


class PlanTemplateIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    is_active: bool = True
    eligibility: PlanEligibility = PlanEligibility.NO_OLD_DEBTS
    charge_kind: PlanChargeKind = PlanChargeKind.NONE
    charge_value: Annotated[int, Field(ge=0, le=setup_service.MAX_AMOUNT_KOBO)] = Field(
        default=0, description="Kobo for a fixed charge; basis points (250 = 2.5%) for a percentage"
    )
    parts: list[PlanPart] = Field(min_length=2, max_length=12)


class PlanTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_active: bool
    eligibility: PlanEligibility
    charge_kind: PlanChargeKind
    charge_value: int
    parts: list[PlanPart]


class OfferInstalmentOut(BaseModel):
    due_date: date
    amount_kobo: int


class PlanOfferOut(BaseModel):
    invoice_id: uuid.UUID
    template_id: uuid.UUID
    name: str
    charge_kobo: int = Field(description="The plan charge added to the bill if chosen")
    total_kobo: int = Field(description="What's owed plus the charge")
    instalments: list[OfferInstalmentOut]


class ChooseOfferIn(BaseModel):
    template_id: uuid.UUID


def offer_out(offer: plan_offers.Offer) -> PlanOfferOut:
    return PlanOfferOut(
        invoice_id=offer.invoice_id,
        template_id=offer.template_id,
        name=offer.name,
        charge_kobo=offer.charge_kobo,
        total_kobo=offer.total_kobo,
        instalments=[OfferInstalmentOut(due_date=d, amount_kobo=a) for d, a in offer.instalments],
    )


def _template_out(t: PlanTemplate) -> PlanTemplateOut:
    return PlanTemplateOut.model_validate(t)


@router.get("/plan-templates", response_model=list[PlanTemplateOut], summary="The school's instalment plans")
def list_plan_templates(ctx: StaffContext = FEES_VIEW, db: Session = Depends(get_db)) -> list[PlanTemplateOut]:
    return [_template_out(t) for t in plan_offers.templates(db)]


@router.post(
    "/plan-templates",
    response_model=PlanTemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add an instalment plan parents can choose",
)
def add_plan_template(
    body: PlanTemplateIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> PlanTemplateOut:
    return _template_out(_save_template(db, ctx, None, body))


@router.put(
    "/plan-templates/{template_id}",
    response_model=PlanTemplateOut,
    summary="Change an instalment plan (plans already chosen don't change)",
)
def change_plan_template(
    template_id: uuid.UUID, body: PlanTemplateIn, ctx: StaffContext = FEES_MANAGE, db: Session = Depends(get_db)
) -> PlanTemplateOut:
    return _template_out(_save_template(db, ctx, template_id, body))


def _save_template(db: Session, ctx: StaffContext, template_id: uuid.UUID | None, body: PlanTemplateIn) -> PlanTemplate:
    return plan_offers.save_template(
        db,
        ctx,
        template_id,
        name=body.name,
        is_active=body.is_active,
        eligibility=body.eligibility,
        charge_kind=body.charge_kind,
        charge_value=body.charge_value,
        parts=[(p.percent_bp, p.day) for p in body.parts],
    )


@router.get(
    "/invoices/{invoice_id}/plan-offers",
    response_model=list[PlanOfferOut],
    summary="The school's plans this bill can go on, with dates and amounts (staff see staff-only plans too)",
)
def bill_plan_offers(
    invoice_id: uuid.UUID, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> list[PlanOfferOut]:
    invoice = get_or_404(db, Invoice, invoice_id, label="Invoice")
    found = plan_offers.offers(db, [invoice.student_id], staff=True).get(invoice.id, [])
    return [offer_out(o) for o in found]


@router.post(
    "/invoices/{invoice_id}/plan/school-plan",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Put a bill on one of the school's plans (its plan charge is added to the bill)",
)
def choose_school_plan(
    invoice_id: uuid.UUID, body: ChooseOfferIn, ctx: StaffContext = INVOICES_ISSUE, db: Session = Depends(get_db)
) -> InvoiceOut:
    plan_offers.choose(
        db,
        invoice_id,
        body.template_id,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        created_by=ctx.user.id,
        staff=True,
    )
    return _invoice_out(db, invoice_id)
