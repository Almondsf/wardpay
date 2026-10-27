import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import outbox
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES, DomainError, NotFound
from app.core.http import RequestMeta, request_meta
from app.core.pagination import Page, PageParams, page_params, paginate_rows
from app.core.ratelimit import get_limiter
from app.core.repo import get_or_404
from app.modules.accounts.deps import StaffContext, require
from app.modules.accounts.permissions import Capability
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.fees.setup_service import MAX_AMOUNT_KOBO
from app.modules.receipts.service import receipts_for_payments
from app.modules.schools.models import ChargePayer, FamilySplitRule, School
from app.modules.students.models import Student

from . import accounts, cashbook, checkout, earmarks, manual, paycards, providers, service, settlements, webhooks
from .allocation import credit_by_student
from .gateways import FakeGateway, GatewayCode, InvalidSignature, enabled_gateways
from .models import (
    CashDepositStatus,
    IntentStatus,
    ManualPayment,
    ManualPaymentStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    VirtualAccount,
    VirtualAccountStatus,
)

router = APIRouter(tags=["payments"], responses=ERROR_RESPONSES)
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)
VIEW = Depends(require(Capability.PAYMENTS_VIEW))
RECORD = Depends(require(Capability.PAYMENTS_RECORD))
APPROVE = Depends(require(Capability.PAYMENTS_APPROVE))
REVERSE = Depends(require(Capability.PAYMENTS_REVERSE))
SETUP = Depends(require(Capability.PAYMENTS_SETUP))

Kobo = Annotated[int, Field(gt=0, le=MAX_AMOUNT_KOBO, description="Amount in kobo")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


# --- Schemas ---------------------------------------------------------------------------------------


class BankOut(BaseModel):
    code: str
    name: str


class PaymentAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway: GatewayCode
    bank_name: str
    account_number_last4: str
    account_name: str


class PaymentAccountIn(BaseModel):
    gateway: GatewayCode
    bank_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20)]
    account_number: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{10}$")]


class PaymentSettingsOut(BaseModel):
    gateways: list[GatewayCode]
    active_provider: GatewayCode = Field(description="The provider new checkouts go through (set by the platform)")
    account: PaymentAccountOut | None = Field(description="The account checkouts use now")
    accounts: list[PaymentAccountOut] = Field(description="Every gateway the school is set up on")


class OnboardingOut(BaseModel):
    url: str


class VirtualAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_number: str
    account_name: str
    bank_name: str
    gateway: GatewayCode


class ProvisionIn(BaseModel):
    student_ids: list[uuid.UUID] | None = Field(default=None, description="Default: every active student without one")


class ProvisionOut(BaseModel):
    requested: int


class StudentBrief(BaseModel):
    id: uuid.UUID
    admission_number: str
    full_name: str


class AppliedOut(BaseModel):
    invoice_id: uuid.UUID
    invoice_number: int
    amount_kobo: int


class PaymentOut(BaseModel):
    id: uuid.UUID
    student: StudentBrief
    kind: PaymentKind
    amount_kobo: int
    method: PaymentMethod
    gateway: GatewayCode | None
    gateway_reference: str | None
    reference: str
    note: str
    received_at: datetime
    reversed: bool
    reverses_payment_id: uuid.UUID | None
    applied: list[AppliedOut]
    unapplied_kobo: int = Field(description="Kept as credit for the student's next invoices")
    receipt_id: uuid.UUID | None
    receipt_number: int | None
    for_fees: list[str] = Field(description="The fees it was marked for; empty: tuition first, then the rest")


class ReverseIn(BaseModel):
    reason: Reason


class MarkFeesIn(BaseModel):
    fees: list[Annotated[str, StringConstraints(max_length=200)]] = Field(min_length=1, max_length=30)


class ManualIn(BaseModel):
    student_id: uuid.UUID
    amount_kobo: Kobo
    method: Literal[PaymentMethod.CASH, PaymentMethod.TELLER, PaymentMethod.POS, PaymentMethod.BANK_TRANSFER]
    received_on: date
    reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""
    fees: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list,
        max_length=30,
        description="What it's for (fee names); empty: tuition first, then the rest",
    )


class ManualOut(BaseModel):
    id: uuid.UUID
    student: StudentBrief
    amount_kobo: int
    method: PaymentMethod
    received_on: date
    reference: str
    note: str
    status: ManualPaymentStatus
    recorded_by: uuid.UUID
    decided_by: uuid.UUID | None
    rejection_reason: str
    payment_id: uuid.UUID | None
    created_at: datetime
    for_fees: list[str] = Field(description="The fees it's for, if the person recording it chose")


class PaymentLinkIn(BaseModel):
    email: EmailStr
    amount_kobo: Kobo | None = Field(default=None, description="Default: everything the student owes")


class PaymentLinkOut(BaseModel):
    reference: str
    amount_kobo: int = Field(description="For the fees: what the bill is credited with")
    charge_kobo: int = Field(description="The provider's charge the parent pays on top (school's choice)")
    authorization_url: str


class VerifyIn(BaseModel):
    reference: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^SP[0-9A-F]{20}$")]


class VerifyOut(BaseModel):
    reference: str
    status: IntentStatus
    amount_kobo: int


class SimulateTransferIn(BaseModel):
    account_number: Annotated[str, StringConstraints(pattern=r"^\d{10}$")]
    amount_kobo: Kobo


# --- Helpers ---------------------------------------------------------------------------------------


def _brief(student: Student) -> StudentBrief:
    return StudentBrief(id=student.id, admission_number=student.admission_number, full_name=student.full_name)


def _payments_out(db: Session, rows: list[tuple[Payment, Student]]) -> list[PaymentOut]:
    ids = [p.id for p, _ in rows]
    applied, reversed_ = service.applied_to(db, ids), service.reversed_ids(db, ids)
    receipts = receipts_for_payments(db, ids)
    marked = earmarks.marked_fees(db, ids)
    out = []
    for payment, student in rows:
        used = [AppliedOut(**a.__dict__) for a in applied[payment.id]]
        receipt = receipts.get(payment.id)
        out.append(
            PaymentOut(
                id=payment.id,
                student=_brief(student),
                kind=payment.kind,
                amount_kobo=payment.amount_kobo,
                method=payment.method,
                gateway=payment.gateway,
                gateway_reference=payment.gateway_reference,
                reference=payment.reference,
                note=payment.note,
                received_at=payment.received_at,
                reversed=payment.id in reversed_,
                reverses_payment_id=payment.reverses_payment_id,
                applied=used,
                unapplied_kobo=payment.amount_kobo - sum(a.amount_kobo for a in used),
                receipt_id=receipt.id if receipt else None,
                receipt_number=receipt.number if receipt else None,
                for_fees=marked[payment.id],
            )
        )
    return out


def _manual_out(entry: ManualPayment, student: Student) -> ManualOut:
    return ManualOut(
        id=entry.id,
        student=_brief(student),
        amount_kobo=entry.amount_kobo,
        method=entry.method,
        received_on=entry.received_on,
        reference=entry.reference,
        note=entry.note,
        status=entry.status,
        recorded_by=entry.recorded_by,
        decided_by=entry.decided_by,
        rejection_reason=entry.rejection_reason,
        payment_id=entry.payment_id,
        created_at=entry.created_at,
        for_fees=list(dict.fromkeys(str(e.get("name", "")) for e in entry.earmarks)),
    )


# --- Setup -----------------------------------------------------------------------------------------


@router.get(
    "/payment-settings",
    response_model=PaymentSettingsOut,
    summary="Which gateways are available and where the school's money settles",
)
def get_settings_(ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> PaymentSettingsOut:
    account = accounts.payment_account(db)
    return PaymentSettingsOut(
        gateways=enabled_gateways(),
        active_provider=providers.active_payment_gateway(db),
        account=PaymentAccountOut.model_validate(account) if account else None,
        accounts=[PaymentAccountOut.model_validate(a) for a in accounts.school_accounts(db)],
    )


class FamilySplitRuleIo(BaseModel):
    rule: FamilySplitRule


@router.get(
    "/payment-settings/family-split-rule",
    response_model=FamilySplitRuleIo,
    summary="How one amount a parent pays for several children is shared",
)
def get_family_split_rule(ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> FamilySplitRuleIo:
    school = db.get(School, ctx.school_id)
    return FamilySplitRuleIo(rule=school.family_split_rule if school else FamilySplitRule.CLEAR_MOST)


@router.put(
    "/payment-settings/family-split-rule",
    response_model=FamilySplitRuleIo,
    summary="Choose how one amount for several children is shared (proprietor; audited)",
)
def set_family_split_rule(
    body: FamilySplitRuleIo, ctx: StaffContext = SETUP, db: Session = Depends(get_db)
) -> FamilySplitRuleIo:
    school = db.get(School, ctx.school_id, with_for_update=True)
    if school is None:
        raise NotFound("School not found.")
    before = school.family_split_rule
    if before != body.rule:
        school.family_split_rule = body.rule
        record(
            db,
            AuditAction.FAMILY_SPLIT_RULE_CHANGED,
            school_id=ctx.school_id,
            actor=ctx.actor,
            meta=ctx.meta,
            target=school,
            changes={"before": before, "after": body.rule},
        )
        db.commit()
    return FamilySplitRuleIo(rule=school.family_split_rule)


class CheckoutChargesIo(BaseModel):
    payer: ChargePayer = Field(description="Who pays the provider's charge on online checkouts")


@router.get(
    "/payment-settings/checkout-charges",
    response_model=CheckoutChargesIo,
    summary="Who pays the provider's charge on online checkouts (card, transfer or USSD on the payment page)",
)
def get_checkout_charges(ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> CheckoutChargesIo:
    payer = db.scalar(select(School.checkout_charge_payer).where(School.id == ctx.school_id))
    return CheckoutChargesIo(payer=payer or ChargePayer.SCHOOL)


@router.put(
    "/payment-settings/checkout-charges",
    response_model=CheckoutChargesIo,
    summary="Choose who pays the provider's charge on online checkouts (proprietor; audited)",
)
def set_checkout_charges(
    body: CheckoutChargesIo, ctx: StaffContext = SETUP, db: Session = Depends(get_db)
) -> CheckoutChargesIo:
    """Owner, 2026-10-27: each school chooses; the school bears it unless it says otherwise. Only checkouts started
    from now on are affected."""
    school = db.get(School, ctx.school_id, with_for_update=True)
    if school is None:
        raise NotFound("School not found.")
    before = school.checkout_charge_payer
    if before != body.payer:
        school.checkout_charge_payer = body.payer
        record(
            db,
            AuditAction.CHECKOUT_CHARGE_PAYER_CHANGED,
            school_id=ctx.school_id,
            actor=ctx.actor,
            meta=ctx.meta,
            target=school,
            changes={"before": before, "after": body.payer},
        )
        db.commit()
    return CheckoutChargesIo(payer=school.checkout_charge_payer)


@router.post(
    "/payment-settings/bachs/onboarding",
    response_model=OnboardingOut,
    summary="Set the school up on Bachs: a link to Bachs' own onboarding (business, identity, payout bank)",
)
def bachs_onboarding(ctx: StaffContext = SETUP, db: Session = Depends(get_db)) -> OnboardingOut:
    get_limiter().hit("payment-setup:school", "10/hour", str(ctx.school_id))
    back = f"{get_settings().frontend_url}/console/payment-settings"
    return OnboardingOut(url=accounts.start_bachs_onboarding(db, ctx, return_url=back))


@router.get("/payment-settings/banks", response_model=list[BankOut], summary="Banks the gateway can pay out to")
def list_banks(gateway: GatewayCode, ctx: StaffContext = SETUP) -> list[BankOut]:
    return [BankOut(code=b.code, name=b.name) for b in accounts.list_banks(gateway)]


@router.put(
    "/payment-settings",
    response_model=PaymentAccountOut,
    summary="Connect the school's bank account (the gateway confirms the account name)",
)
def set_up_account(
    body: PaymentAccountIn, ctx: StaffContext = SETUP, db: Session = Depends(get_db)
) -> PaymentAccountOut:
    get_limiter().hit("payment-setup:school", "10/hour", str(ctx.school_id))
    return PaymentAccountOut.model_validate(
        accounts.set_up_payment_account(
            db, ctx, gateway=body.gateway, bank_code=body.bank_code, account_number=body.account_number
        )
    )


@router.post(
    "/virtual-accounts/provision",
    response_model=ProvisionOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request account numbers for students who don't have one (done in the background)",
)
def provision(
    body: ProvisionIn, ctx: StaffContext = Depends(require(Capability.PAYMENTS_APPROVE)), db: Session = Depends(get_db)
) -> ProvisionOut:
    return ProvisionOut(requested=accounts.request_virtual_accounts(db, ctx, body.student_ids))


@router.get(
    "/students/{student_id}/virtual-account",
    response_model=VirtualAccountOut | None,
    summary="The account number this student's fees can be paid into",
)
def get_virtual_account(
    student_id: uuid.UUID, ctx: StaffContext = Depends(require(Capability.STUDENTS_VIEW)), db: Session = Depends(get_db)
) -> VirtualAccount | None:
    get_or_404(db, Student, student_id, label="Student")
    return db.scalar(
        select(VirtualAccount).where(
            VirtualAccount.student_id == student_id, VirtualAccount.status == VirtualAccountStatus.ACTIVE
        )
    )


PDF_ONLY: dict[int | str, dict[str, Any]] = {200: {"content": {"application/pdf": {}}, "description": "A PDF to print"}}


@router.get(
    "/students/{student_id}/pay-card",
    response_class=Response,
    responses=PDF_ONLY,
    summary="A printable pay card for one pupil: account number and a QR code to the parent's payment page",
)
def student_pay_card(student_id: uuid.UUID, ctx: StaffContext = RECORD, db: Session = Depends(get_db)) -> Response:
    student = get_or_404(db, Student, student_id, label="Student")
    return _pay_cards(db, ctx, f"pay-card-{student.admission_number}", student_id=student_id)


@router.get(
    "/pay-cards",
    response_class=Response,
    responses=PDF_ONLY,
    summary="Printable pay cards for every current pupil in a class, four to an A4 page",
)
def class_pay_cards(class_level_id: uuid.UUID, ctx: StaffContext = RECORD, db: Session = Depends(get_db)) -> Response:
    return _pay_cards(db, ctx, "pay-cards", class_level_id=class_level_id)


def _pay_cards(db: Session, ctx: StaffContext, filename: str, **scope: uuid.UUID) -> Response:
    get_limiter().hit("pay-cards:school", "30/hour", str(ctx.school_id))
    cards = paycards.pay_cards(db, **scope)
    if not cards:
        raise DomainError("There are no current pupils in that class.", field="class_level_id")
    content = paycards.render_pay_cards(get_or_404(db, School, ctx.school_id, label="School"), cards)
    # The cards carry parents' payment links: who printed them is recorded
    record(
        db,
        AuditAction.REPORT_EXPORTED,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        changes={"report": "pay_cards", "cards": len(cards), **{k: str(v) for k, v in scope.items()}},
    )
    db.commit()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in filename)
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"', "Cache-Control": "no-store"},
    )


# --- Payments --------------------------------------------------------------------------------------


@router.get("/payments", response_model=Page[PaymentOut], summary="Payments received, newest first")
def list_payments(
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
    student_id: uuid.UUID | None = None,
    method: PaymentMethod | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = Query(default=None, max_length=100),
) -> Page[PaymentOut]:
    stmt = service.payments_query(student_id=student_id, method=method, since=since, until=until, search=search)
    rows, total = paginate_rows(db, stmt, params)
    return Page(
        items=_payments_out(db, [tuple(r) for r in rows]), total=total, limit=params.limit, offset=params.offset
    )


@router.get("/payments/{payment_id}", response_model=PaymentOut, summary="A payment and where it was applied")
def get_payment(payment_id: uuid.UUID, ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> PaymentOut:
    row = db.execute(service.payments_query().where(Payment.id == payment_id)).first()
    if row is None:
        raise NotFound("Payment not found.")
    payment, student = row
    return _payments_out(db, [(payment, student)])[0]


@router.post(
    "/payments/{payment_id}/reverse",
    response_model=PaymentOut,
    summary="Reverse a payment recorded in error (adds a negative entry; nothing is deleted)",
)
def reverse(
    payment_id: uuid.UUID, body: ReverseIn, ctx: StaffContext = REVERSE, db: Session = Depends(get_db)
) -> PaymentOut:
    reversal = service.reverse_payment(db, ctx, payment_id, body.reason)
    return get_payment(reversal.id, ctx, db)


@router.post(
    "/payments/{payment_id}/fees",
    response_model=PaymentOut,
    summary="Say what a payment was for after it arrived (once): which fees show it as paid",
)
def mark_for_fees(
    payment_id: uuid.UUID, body: MarkFeesIn, ctx: StaffContext = RECORD, db: Session = Depends(get_db)
) -> PaymentOut:
    earmarks.mark_payment(db, ctx, payment_id, body.fees)
    return get_payment(payment_id, ctx, db)


@router.get(
    "/students/{student_id}/credit",
    response_model=int,
    summary="Money the student has paid that isn't applied to an invoice yet (kobo)",
)
def student_credit(student_id: uuid.UUID, ctx: StaffContext = VIEW, db: Session = Depends(get_db)) -> int:
    get_or_404(db, Student, student_id, label="Student")
    return credit_by_student(db, [student_id])[student_id]


# --- Manual payments -------------------------------------------------------------------------------


@router.post(
    "/manual-payments",
    response_model=ManualOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record cash, teller, POS or a direct transfer; someone else approves it",
)
def record_manual(body: ManualIn, ctx: StaffContext = RECORD, db: Session = Depends(get_db)) -> ManualOut:
    entry = manual.record_manual(
        db,
        ctx,
        student_id=body.student_id,
        amount_kobo=body.amount_kobo,
        method=body.method,
        received_on=body.received_on,
        reference=body.reference,
        note=body.note,
        fees=body.fees,
    )
    return _manual_out(entry, get_or_404(db, Student, entry.student_id))


@router.get("/manual-payments", response_model=Page[ManualOut], summary="Manual payments (default: awaiting approval)")
def list_manual(
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
    status_filter: ManualPaymentStatus | None = Query(default=ManualPaymentStatus.PENDING, alias="status"),
) -> Page[ManualOut]:
    rows, total = paginate_rows(db, manual.manual_query(status_filter), params)
    return Page(items=[_manual_out(e, s) for e, s in rows], total=total, limit=params.limit, offset=params.offset)


@router.post(
    "/manual-payments/{entry_id}/approve",
    response_model=ManualOut,
    summary="Approve (must be someone other than who recorded it)",
)
def approve_manual(entry_id: uuid.UUID, ctx: StaffContext = APPROVE, db: Session = Depends(get_db)) -> ManualOut:
    entry = manual.approve(db, ctx, entry_id)
    return _manual_out(entry, get_or_404(db, Student, entry.student_id))


@router.post("/manual-payments/{entry_id}/reject", response_model=ManualOut, summary="Reject with a reason")
def reject_manual(
    entry_id: uuid.UUID, body: ReverseIn, ctx: StaffContext = APPROVE, db: Session = Depends(get_db)
) -> ManualOut:
    entry = manual.reject(db, ctx, entry_id, body.reason)
    return _manual_out(entry, get_or_404(db, Student, entry.student_id))


# --- Card checkout ---------------------------------------------------------------------------------


@router.post(
    "/students/{student_id}/payment-link",
    response_model=PaymentLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="A card checkout link to send to the parent",
)
def payment_link(
    student_id: uuid.UUID, body: PaymentLinkIn, ctx: StaffContext = RECORD, db: Session = Depends(get_db)
) -> PaymentLinkOut:
    intent = checkout.start_checkout(
        db,
        student_id=student_id,
        amount_kobo=body.amount_kobo,
        email=body.email,
        actor=ctx.actor,
        meta=ctx.meta,
        created_by=ctx.user.id,
    )
    return PaymentLinkOut(
        reference=intent.reference,
        amount_kobo=intent.amount_kobo,
        charge_kobo=intent.charge_kobo,
        authorization_url=intent.authorization_url,
    )


@router.post(
    "/payments/verify",
    response_model=VerifyOut,
    summary="After the gateway redirects back: confirm the checkout (the outcome comes from the gateway)",
)
def verify_checkout(body: VerifyIn, meta: RequestMeta = Depends(request_meta)) -> VerifyOut:
    get_limiter().hit("payment-verify:ip", "30/minute", meta.ip)
    intent = checkout.confirm(body.reference)
    if intent is None:
        raise NotFound("Payment not found.")
    return VerifyOut(reference=intent.reference, status=intent.status, amount_kobo=intent.amount_kobo)


# --- Cash book ----------------------------------------------------------------------------------------


class CashDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    received_kobo: int
    payments: int
    reversed_kobo: int
    banked_kobo: int
    spent_kobo: int = Field(description="Expenses paid from the drawer that day")
    held_kobo: int = Field(description="What should be in the drawer at the end of the day")
    counted_kobo: int | None = Field(description="The drawer count, if the day was closed")
    difference_kobo: int | None = Field(description="Counted minus expected at close (negative: cash short)")


class StaffCashOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    name: str
    received_kobo: int
    payments: int
    banked_kobo: int
    spent_kobo: int


class LateEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["cash", "banking", "expense"]
    day: date
    amount_kobo: int
    recorded_at: datetime
    recorded_by: str


class CashBookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start: date
    end: date
    opening_kobo: int
    days: list[CashDayOut]
    staff: list[StaffCashOut]
    late: list[LateEntryOut] = Field(description="Cash or banking recorded for a closed day after it was closed")
    held_now_kobo: int = Field(description="Cash that should be in the drawer now")
    unchecked_banking_kobo: int = Field(description="Banked, not yet confirmed against the bank statement")
    last_closed: date | None


class CashDepositIn(BaseModel):
    amount_kobo: Kobo
    banked_on: date
    bank_reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class CashDepositOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount_kobo: int
    banked_on: date
    bank_reference: str
    note: str
    status: CashDepositStatus
    recorded_by: uuid.UUID
    decided_by: uuid.UUID | None
    rejection_reason: str
    created_at: datetime


class CashDecisionIn(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class CashCloseIn(BaseModel):
    day: date
    counted_kobo: Annotated[int, Field(ge=0, le=MAX_AMOUNT_KOBO)]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class CashCloseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    expected_kobo: int
    counted_kobo: int
    note: str
    closed_at: datetime


@router.get("/cash-book", response_model=CashBookOut, summary="Cash taken, cash banked, and what should be held")
def get_cash_book(
    start: date | None = None, end: date | None = None, ctx: StaffContext = VIEW, db: Session = Depends(get_db)
) -> CashBookOut:
    default_start, default_end = cashbook.today_range()
    return CashBookOut.model_validate(cashbook.cash_book(db, start or default_start, end or default_end))


@router.get("/cash-deposits", response_model=list[CashDepositOut], summary="Cash taken to the bank, newest first")
def list_cash_deposits(
    status_: CashDepositStatus | None = Query(default=None, alias="status"),
    ctx: StaffContext = VIEW,
    db: Session = Depends(get_db),
) -> list[CashDepositOut]:
    return [CashDepositOut.model_validate(d) for d in cashbook.deposits(db, status=status_)]


@router.post(
    "/cash-deposits",
    response_model=CashDepositOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record cash taken to the bank (a second person checks it against the statement)",
)
def record_cash_deposit(
    body: CashDepositIn, ctx: StaffContext = RECORD, db: Session = Depends(get_db)
) -> CashDepositOut:
    return CashDepositOut.model_validate(
        cashbook.record_deposit(
            db,
            ctx,
            amount_kobo=body.amount_kobo,
            banked_on=body.banked_on,
            bank_reference=body.bank_reference,
            note=body.note,
        )
    )


@router.post(
    "/cash-deposits/{deposit_id}/confirm",
    response_model=CashDepositOut,
    summary="Confirm banking against the bank statement (not by the person who recorded it)",
)
def confirm_cash_deposit(
    deposit_id: uuid.UUID, ctx: StaffContext = APPROVE, db: Session = Depends(get_db)
) -> CashDepositOut:
    return CashDepositOut.model_validate(cashbook.decide_deposit(db, ctx, deposit_id, confirm=True))


@router.post(
    "/cash-deposits/{deposit_id}/reject",
    response_model=CashDepositOut,
    summary="Reject banking that doesn't match the bank statement (the cash counts as still held)",
)
def reject_cash_deposit(
    deposit_id: uuid.UUID, body: CashDecisionIn, ctx: StaffContext = APPROVE, db: Session = Depends(get_db)
) -> CashDepositOut:
    return CashDepositOut.model_validate(
        cashbook.decide_deposit(db, ctx, deposit_id, confirm=False, reason=body.reason)
    )


@router.post(
    "/cash-book/close",
    response_model=CashCloseOut,
    status_code=status.HTTP_201_CREATED,
    summary="Close a cash day: the drawer count beside what was expected",
)
def close_cash_day(body: CashCloseIn, ctx: StaffContext = APPROVE, db: Session = Depends(get_db)) -> CashCloseOut:
    return CashCloseOut.model_validate(
        cashbook.close_day(db, ctx, day=body.day, counted_kobo=body.counted_kobo, note=body.note)
    )


# --- Webhooks --------------------------------------------------------------------------------------


async def _webhook(gateway: GatewayCode, request: Request, db: Session) -> Response:
    if gateway not in enabled_gateways():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    # Notifications are signed and checked with the gateway; the limit only stops floods. Paystack sends a
    # whole morning's transfers from a few addresses, so 600 a minute held back its own retries (load test)
    get_limiter().hit("webhook:ip", "3000/minute", request.client.host if request.client else "")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:  # the DB work is synchronous: keep it off the event loop
        await run_in_threadpool(webhooks.receive, db, gateway, headers, body)
    except InvalidSignature:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    return Response(status_code=status.HTTP_200_OK)


@webhook_router.post("/paystack")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    return await _webhook(GatewayCode.PAYSTACK, request, db)


@webhook_router.post("/flutterwave")
async def flutterwave_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    return await _webhook(GatewayCode.FLUTTERWAVE, request, db)


@webhook_router.post("/bachs")
async def bachs_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    return await _webhook(GatewayCode.BACHS, request, db)


@webhook_router.post("/fake")
async def fake_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    return await _webhook(GatewayCode.FAKE, request, db)


# --- Development only: pretend a parent paid --------------------------------------------------------

dev_router = APIRouter(prefix="/dev/fake-gateway", tags=["development"], include_in_schema=False)


@dev_router.post("/transfers", status_code=status.HTTP_202_ACCEPTED)
def simulate_transfer(body: SimulateTransferIn, db: Session = Depends(get_db)) -> dict[str, str]:
    """A transfer into a fake virtual account, delivered as a signed webhook. Development and tests only."""
    if get_settings().is_production or GatewayCode.FAKE not in enabled_gateways():
        raise NotFound("Not found.")
    headers, payload = FakeGateway.shared().simulate_transfer(body.account_number, body.amount_kobo)
    webhooks.receive(db, GatewayCode.FAKE, headers, payload)
    return {"status": "sent"}


# --- What the gateway paid out ----------------------------------------------------------------------


class PayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    settled_on: date | None
    status: str
    gross_kobo: int = Field(description="What parents paid in the transactions it covers")
    charges_kobo: int = Field(description="Taken off before the bank: the provider's fees and any split")
    paid_out_kobo: int = Field(description="What reached the school's bank")
    transactions: int
    matched: int = Field(description="Transactions matched to a WardPay payment")


class UnsettledOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: uuid.UUID
    received_at: datetime
    receipt_number: int | None
    admission_number: str
    method: PaymentMethod
    amount_kobo: int
    reference: str


class UnknownItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    settled_on: date | None
    reference: str
    amount_kobo: int


class ReconciliationOut(BaseModel):
    start: date
    end: date
    connected: bool = Field(description="The school is on a provider that reports its payouts")
    checked_at: datetime | None = Field(description="When WardPay last asked the provider")
    gross_kobo: int
    charges_kobo: int
    paid_out_kobo: int
    payouts: list[PayoutOut]
    unsettled: list[UnsettledOut] = Field(description="Payments not paid out a few days after they arrived")
    unsettled_kobo: int
    unknown: list[UnknownItemOut] = Field(description="Paid out, but WardPay has no payment with that reference")


@router.get(
    "/settlements",
    response_model=ReconciliationOut,
    summary="What the payment provider paid into the bank, matched to payments",
)
def get_settlements(
    start: date | None = None,
    end: date | None = None,
    ctx: StaffContext = Depends(require(Capability.REPORTS_VIEW)),
    db: Session = Depends(get_db),
) -> ReconciliationOut:
    default_start, default_end = cashbook.today_range(30)
    report = settlements.reconciliation(db, start or default_start, end or default_end)
    return ReconciliationOut(
        start=report.start,
        end=report.end,
        connected=report.connected,
        checked_at=report.checked_at,
        gross_kobo=report.gross_kobo,
        charges_kobo=report.charges_kobo,
        paid_out_kobo=report.paid_out_kobo,
        payouts=[PayoutOut.model_validate(p) for p in report.payouts],
        unsettled=[UnsettledOut.model_validate(u) for u in report.unsettled],
        unsettled_kobo=report.unsettled_kobo,
        unknown=[UnknownItemOut.model_validate(u) for u in report.unknown],
    )


@router.post(
    "/settlements/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask the payment provider for the latest payouts now",
)
def refresh_settlements(ctx: StaffContext = APPROVE, db: Session = Depends(get_db)) -> dict[str, str]:
    get_limiter().hit("settlements-refresh:school", "6/hour", str(ctx.school_id))
    outbox.enqueue(db, settlements.SYNC_TASK, {"school_id": str(ctx.school_id)})
    db.commit()
    return {"status": "queued"}
