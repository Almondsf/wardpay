import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES, NotFound, PermissionDenied
from app.core.http import RequestMeta, request_meta
from app.core.pagination import Page, PageParams, page_params, paginate_rows
from app.core.ratelimit import get_limiter
from app.core.tenancy import scope_platform_wide
from app.modules.accounts.deps import Principal, StaffContext, require, require_platform_admin
from app.modules.accounts.models import Role
from app.modules.accounts.permissions import Capability
from app.modules.notifications import providers as sms_providers
from app.modules.payments import providers
from app.modules.payments.gateways import GatewayCode
from app.modules.payments.models import IntentStatus
from app.modules.schools.models import School, SchoolOrigin, SchoolStatus
from app.modules.schools.schemas import SchoolOut

from . import billing, card_payments, console, onboarding, pdf, plans, problems, signup, staff_support, trends
from .models import SubscriptionCheckout, SubscriptionLineKind

router = APIRouter(prefix="/platform", tags=["platform"], responses=ERROR_RESPONSES)
public_router = APIRouter(prefix="/public", tags=["public"], responses=ERROR_RESPONSES)
school_router = APIRouter(tags=["subscription"], responses=ERROR_RESPONSES)
ADMIN = Depends(require_platform_admin)
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=150)]
Kobo = Annotated[int, Field(ge=0, le=10**12)]
PDF = "application/pdf"


# --- Schemas -----------------------------------------------------------------------------------------


class PlatformOverviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schools_active: int
    schools_pending: int
    schools_suspended: int
    signups_30_days: int
    pupils: int
    processed_this_month_kobo: int
    processed_last_month_kobo: int
    payments_this_month: int
    subscription_billed_kobo: int
    subscription_paid_kobo: int
    subscription_outstanding_kobo: int
    subscription_overdue_kobo: int
    problems_open: int = Field(description="Stuck payments, notifications and tasks that need a person")


class PlatformSchoolOut(SchoolOut):
    origin: SchoolOrigin
    plan_name: str
    pupils: int
    proprietor_email: str
    payments_connected: bool
    subscription_owed_kobo: int
    last_sign_in_at: datetime | None


class SchoolStaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str
    role: Role
    is_active: bool
    mfa_enabled: bool
    invited: bool
    last_login_at: datetime | None


class SchoolDetailOut(BaseModel):
    school: PlatformSchoolOut
    plan_id: uuid.UUID | None
    staff: list[SchoolStaffOut]
    students_active: int
    invoices_this_term: int
    collected_this_term_kobo: int
    collected_30_days_kobo: int
    last_activity_at: datetime | None


class PlatformPlanOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    setup_fee_kobo: int
    per_pupil_year_kobo: int
    per_pupil_term_kobo: int
    instalment_plan_fee_kobo: int = Field(description="Charged to the school per instalment plan made")
    plan_charge_per_payment_kobo: int = Field(
        description="Standard plan charge to the parent: this per payment plus the per-plan fee"
    )
    is_default: bool
    is_active: bool
    schools: int


class PlatformPlanIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""
    setup_fee_kobo: Kobo
    per_pupil_year_kobo: Kobo
    instalment_plan_fee_kobo: Kobo = 20_000
    plan_charge_per_payment_kobo: Kobo = 30_000
    is_active: bool = True
    is_default: bool = False


class SchoolPlanIn(BaseModel):
    plan_id: uuid.UUID


class SubscriptionGenerateIn(BaseModel):
    dry_run: bool = True


class SubscriptionDraftLineOut(BaseModel):
    kind: SubscriptionLineKind
    description: str
    quantity: int
    unit_kobo: int
    amount_kobo: int


class SubscriptionDraftBillOut(BaseModel):
    school_id: uuid.UUID
    school_name: str
    plan_name: str
    period: str
    pupils: int
    total_kobo: int
    lines: list[SubscriptionDraftLineOut]


class SubscriptionRunOut(BaseModel):
    dry_run: bool
    bills: list[SubscriptionDraftBillOut]
    total_kobo: int
    already_billed: int
    no_current_term: list[str]


class SubscriptionInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int
    school_id: uuid.UUID
    school_name: str
    plan_name: str
    period: str
    pupils: int
    issued_at: datetime
    due_date: date
    total_kobo: int
    paid_kobo: int
    balance_kobo: int
    status: Literal["unpaid", "overdue", "paid", "void"]


class SubscriptionLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: SubscriptionLineKind
    description: str
    quantity: int
    unit_kobo: int
    amount_kobo: int


class SubscriptionPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    amount_kobo: int
    received_on: date
    reference: str
    gateway: GatewayCode | None = Field(description="Set for a card payment the gateway confirmed; else a transfer")
    created_at: datetime


class SubscriptionInvoiceDetailOut(BaseModel):
    invoice: SubscriptionInvoiceOut
    lines: list[SubscriptionLineOut]
    payments: list[SubscriptionPaymentOut]


class SubscriptionPaymentIn(BaseModel):
    amount_kobo: Annotated[int, Field(gt=0, le=10**12)]
    received_on: date
    reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""


class SubscriptionVoidIn(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class SubscriptionDiscountIn(BaseModel):
    amount_kobo: Annotated[int, Field(gt=0, le=10**12)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=150)]


class SubscriptionDueDateIn(BaseModel):
    due_date: date
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class SupportLookupHitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["school", "staff", "payment", "account_number", "receipt"]
    school_id: uuid.UUID | None
    school_name: str
    title: str
    detail: str


class PublicInfoOut(BaseModel):
    app_name: str
    support_email: str
    signup_open: bool
    setup_fee_kobo: int
    per_pupil_term_kobo: int


class SchoolSignupIn(BaseModel):
    school_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
    proprietor_name: Name
    email: EmailStr
    phone: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 ()-]{7,20}$")]
    address: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""
    accept_terms: bool
    # Left empty by people; bots fill every field in
    website: Annotated[str, StringConstraints(max_length=200)] = ""


class BankDetailsOut(BaseModel):
    bank_name: str
    account_number: str
    account_name: str


class SubscriptionOut(BaseModel):
    plan: PlatformPlanOut
    pupils_this_term: int
    next_term_estimate_kobo: int
    pay_to: BankDetailsOut | None
    invoices: list[SubscriptionInvoiceOut]


class SubscriptionCheckoutOut(BaseModel):
    reference: str
    amount_kobo: int
    authorization_url: str


class SubscriptionVerifyIn(BaseModel):
    reference: Annotated[str, StringConstraints(pattern=r"^WS[0-9A-F]{20}$")]


class SubscriptionVerifyOut(BaseModel):
    reference: str
    status: IntentStatus
    amount_kobo: int
    review_reason: str


class OnboardingStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    hint: str
    link: str
    done: bool


# --- Helpers -----------------------------------------------------------------------------------------


def _plan_out(usage: plans.PlanUsage) -> PlatformPlanOut:
    p = usage.plan
    return PlatformPlanOut(
        id=p.id,
        name=p.name,
        description=p.description,
        setup_fee_kobo=p.setup_fee_kobo,
        per_pupil_year_kobo=p.per_pupil_year_kobo,
        per_pupil_term_kobo=billing.termly(p.per_pupil_year_kobo),
        instalment_plan_fee_kobo=p.instalment_plan_fee_kobo,
        plan_charge_per_payment_kobo=p.plan_charge_per_payment_kobo,
        is_default=p.is_default,
        is_active=p.is_active,
        schools=usage.schools,
    )


def _school_out(row: console.SchoolRow) -> PlatformSchoolOut:
    base = SchoolOut.model_validate(row.school).model_dump()
    return PlatformSchoolOut(
        **base,
        origin=row.school.origin,
        plan_name=row.plan_name,
        pupils=row.pupils,
        proprietor_email=row.proprietor_email,
        payments_connected=row.payments_connected,
        subscription_owed_kobo=row.subscription_owed_kobo,
        last_sign_in_at=row.last_sign_in_at,
    )


def _invoice_out(row: billing.InvoiceRow) -> SubscriptionInvoiceOut:
    return SubscriptionInvoiceOut(
        id=row.id,
        number=row.number,
        school_id=row.school_id,
        school_name=row.school_name,
        plan_name=row.plan_name,
        period=row.period,
        pupils=row.pupils,
        issued_at=row.issued_at,
        due_date=row.due_date,
        total_kobo=row.total_kobo,
        paid_kobo=row.paid_kobo,
        balance_kobo=row.balance_kobo,
        status=row.status,
    )


def _detail_out(db: Session, invoice_id: uuid.UUID) -> SubscriptionInvoiceDetailOut:
    row, lines, payments = billing.invoice_detail(db, invoice_id)
    return SubscriptionInvoiceDetailOut(
        invoice=_invoice_out(row),
        lines=[SubscriptionLineOut.model_validate(ln) for ln in lines],
        payments=[SubscriptionPaymentOut.model_validate(p) for p in payments],
    )


def _pdf_response(db: Session, invoice_id: uuid.UUID) -> Response:
    row, lines, payments = billing.invoice_detail(db, invoice_id)
    school = db.get(School, row.school_id)
    if school is None:
        raise NotFound("Invoice not found.")
    return Response(
        pdf.render_invoice(row, school, lines, payments),
        media_type=PDF,
        headers={"Content-Disposition": f'attachment; filename="wardpay-invoice-{row.number}.pdf"'},
    )


# --- Platform admin ----------------------------------------------------------------------------------


@router.get("/overview", response_model=PlatformOverviewOut, summary="Totals across every school")
def get_overview(admin: Principal = ADMIN, db: Session = Depends(get_db)) -> PlatformOverviewOut:
    return PlatformOverviewOut(**asdict(console.overview(db)), problems_open=problems.count(db))


@router.get("/schools", response_model=Page[PlatformSchoolOut], summary="Schools, newest first")
def list_schools(
    admin: Principal = ADMIN,
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
    search: str | None = Query(default=None, max_length=100),
    school_status: SchoolStatus | None = Query(default=None, alias="status"),
) -> Page[PlatformSchoolOut]:
    rows, total = console.schools(db, search=search, status=school_status, limit=params.limit, offset=params.offset)
    return Page(items=[_school_out(r) for r in rows], total=total, limit=params.limit, offset=params.offset)


@router.get("/schools/{school_id}", response_model=SchoolDetailOut, summary="One school, for support")
def get_school(school_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> SchoolDetailOut:
    d = console.school_detail(db, school_id)
    return SchoolDetailOut(
        school=_school_out(d.row),
        plan_id=d.plan_id,
        staff=[SchoolStaffOut.model_validate(s) for s in d.staff],
        students_active=d.students_active,
        invoices_this_term=d.invoices_this_term,
        collected_this_term_kobo=d.collected_this_term_kobo,
        collected_30_days_kobo=d.collected_30_days_kobo,
        last_activity_at=d.last_activity_at,
    )


@router.put("/schools/{school_id}/plan", response_model=SchoolDetailOut, summary="Move a school to another plan")
def put_school_plan(
    school_id: uuid.UUID, body: SchoolPlanIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SchoolDetailOut:
    plans.set_school_plan(db, admin, school_id, body.plan_id)
    return get_school(school_id, admin, db)


@router.get("/plans", response_model=list[PlatformPlanOut], summary="Subscription plans")
def list_plans(admin: Principal = ADMIN, db: Session = Depends(get_db)) -> list[PlatformPlanOut]:
    return [_plan_out(u) for u in plans.list_plans(db)]


@router.post("/plans", response_model=PlatformPlanOut, status_code=status.HTTP_201_CREATED, summary="Add a plan")
def create_plan(body: PlatformPlanIn, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> PlatformPlanOut:
    plan = plans.save_plan(db, admin, None, **body.model_dump())
    return next(_plan_out(u) for u in plans.list_plans(db) if u.plan.id == plan.id)


@router.put(
    "/plans/{plan_id}", response_model=PlatformPlanOut, summary="Change a plan's prices (affects later bills only)"
)
def update_plan(
    plan_id: uuid.UUID, body: PlatformPlanIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> PlatformPlanOut:
    plan = plans.save_plan(db, admin, plan_id, **body.model_dump())
    return next(_plan_out(u) for u in plans.list_plans(db) if u.plan.id == plan.id)


@router.post(
    "/billing/generate",
    response_model=SubscriptionRunOut,
    summary="Bill every active school for its current term (dry_run=true to preview); safe to re-run",
)
def generate_bills(
    body: SubscriptionGenerateIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionRunOut:
    run = billing.generate(db, admin, dry_run=body.dry_run)
    return SubscriptionRunOut(
        dry_run=run.dry_run,
        bills=[
            SubscriptionDraftBillOut(
                school_id=b.school_id,
                school_name=b.school_name,
                plan_name=b.plan.name,
                period=billing.term_label(b.session_name, b.term_number),
                pupils=b.pupils,
                total_kobo=b.total_kobo,
                lines=[
                    SubscriptionDraftLineOut(
                        kind=ln.kind,
                        description=ln.description,
                        quantity=ln.quantity,
                        unit_kobo=ln.unit_kobo,
                        amount_kobo=ln.amount_kobo,
                    )
                    for ln in b.lines
                ],
            )
            for b in run.bills
        ],
        total_kobo=run.total_kobo,
        already_billed=run.already_billed,
        no_current_term=run.no_current_term,
    )


@router.get(
    "/billing/invoices", response_model=Page[SubscriptionInvoiceOut], summary="Subscription invoices, newest first"
)
def list_invoices(
    admin: Principal = ADMIN,
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
    school_id: uuid.UUID | None = None,
    invoice_status: Literal["unpaid", "overdue", "paid", "void"] | None = Query(default=None, alias="status"),
) -> Page[SubscriptionInvoiceOut]:
    rows, total = paginate_rows(db, billing.invoices_query(school_id=school_id, status=invoice_status), params)
    return Page(
        items=[_invoice_out(billing.to_row(r)) for r in rows], total=total, limit=params.limit, offset=params.offset
    )


@router.get(
    "/billing/invoices/{invoice_id}", response_model=SubscriptionInvoiceDetailOut, summary="One subscription invoice"
)
def get_invoice(
    invoice_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionInvoiceDetailOut:
    return _detail_out(db, invoice_id)


@router.get(
    "/billing/invoices/{invoice_id}/pdf",
    response_class=Response,
    responses={200: {"content": {PDF: {}}}},
    summary="Subscription invoice as PDF",
)
def invoice_pdf(invoice_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> Response:
    return _pdf_response(db, invoice_id)


@router.post(
    "/billing/invoices/{invoice_id}/payments",
    response_model=SubscriptionInvoiceDetailOut,
    summary="Record a school's bank transfer against its invoice",
)
def record_payment(
    invoice_id: uuid.UUID, body: SubscriptionPaymentIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionInvoiceDetailOut:
    billing.record_payment(
        db, admin, invoice_id, amount_kobo=body.amount_kobo, received_on=body.received_on, reference=body.reference
    )
    return _detail_out(db, invoice_id)


@router.post(
    "/billing/invoices/{invoice_id}/void", response_model=SubscriptionInvoiceDetailOut, summary="Cancel an unpaid bill"
)
def void_invoice(
    invoice_id: uuid.UUID, body: SubscriptionVoidIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionInvoiceDetailOut:
    billing.void(db, admin, invoice_id, body.reason)
    return _detail_out(db, invoice_id)


@router.post(
    "/billing/invoices/{invoice_id}/discount",
    response_model=SubscriptionInvoiceDetailOut,
    summary="Take money off a bill (e.g. waive the set-up fee); at most what's still owed",
)
def discount_invoice(
    invoice_id: uuid.UUID, body: SubscriptionDiscountIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionInvoiceDetailOut:
    billing.add_discount(db, admin, invoice_id, amount_kobo=body.amount_kobo, reason=body.reason)
    return _detail_out(db, invoice_id)


@router.put(
    "/billing/invoices/{invoice_id}/due-date",
    response_model=SubscriptionInvoiceDetailOut,
    summary="Change when a bill is due",
)
def change_due_date(
    invoice_id: uuid.UUID, body: SubscriptionDueDateIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> SubscriptionInvoiceDetailOut:
    billing.change_due_date(db, admin, invoice_id, due_date=body.due_date, reason=body.reason)
    return _detail_out(db, invoice_id)


@router.get(
    "/lookup", response_model=list[SupportLookupHitOut], summary="Find a school, staff member, payment or receipt"
)
def support_lookup(
    q: str = Query(min_length=3, max_length=100), admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> list[SupportLookupHitOut]:
    get_limiter().hit("platform-lookup:user", "120/hour", str(admin.user.id))
    return [SupportLookupHitOut.model_validate(h) for h in console.lookup(db, admin, q)]


# --- Public sign-up ------------------------------------------------------------------------------------


@public_router.get(
    "/info", response_model=PublicInfoOut, summary="Name, contact and the standard prices, for the website"
)
def public_info(db: Session = Depends(get_db)) -> PublicInfoOut:
    settings = get_settings()
    scope_platform_wide(db)  # plans are platform-wide; no school is involved
    plan = billing.default_plan(db)
    return PublicInfoOut(
        app_name=settings.app_name,
        support_email=settings.support_email,
        signup_open=settings.signup_enabled,
        setup_fee_kobo=plan.setup_fee_kobo,
        per_pupil_term_kobo=billing.termly(plan.per_pupil_year_kobo),
    )


@public_router.post(
    "/signup",
    status_code=status.HTTP_202_ACCEPTED,
    summary="A school signs itself up; the proprietor confirms by email (same answer for any email)",
)
def school_signup(
    body: SchoolSignupIn, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> dict[str, str]:
    settings = get_settings()
    if not settings.signup_enabled:
        raise PermissionDenied("Sign-up is closed. Please contact us.", code="signup_closed")
    get_limiter().hit("signup:ip", settings.signup_rate_per_ip, meta.ip)
    if not body.accept_terms:
        raise PermissionDenied("Please accept the terms to continue.", code="terms_required")
    if not body.website:  # the honeypot is empty: a person
        signup.sign_up(
            db,
            meta,
            school_name=body.school_name,
            proprietor_name=body.proprietor_name,
            email=str(body.email),
            phone=body.phone,
            address=body.address,
        )
    return {"detail": "Check your email: we've sent a link to confirm your school."}


# --- The school's own -----------------------------------------------------------------------------------


@school_router.get("/subscription", response_model=SubscriptionOut, summary="The school's plan and invoices")
def my_subscription(
    ctx: StaffContext = Depends(require(Capability.REPORTS_VIEW)), db: Session = Depends(get_db)
) -> SubscriptionOut:
    view = billing.school_plan(db, ctx.school_id)
    rows = db.execute(billing.invoices_query(school_id=ctx.school_id).limit(100)).all()
    settings = get_settings()
    return SubscriptionOut(
        plan=_plan_out(plans.PlanUsage(view.plan, 0)),
        pupils_this_term=view.pupils_this_term,
        next_term_estimate_kobo=view.next_term_estimate_kobo,
        pay_to=BankDetailsOut(
            bank_name=settings.platform_bank_name,
            account_number=settings.platform_account_number,
            account_name=settings.platform_account_name,
        )
        if settings.platform_account_number
        else None,
        invoices=[_invoice_out(billing.to_row(r)) for r in rows],
    )


@school_router.get(
    "/subscription/invoices/{invoice_id}/pdf",
    response_class=Response,
    responses={200: {"content": {PDF: {}}}},
    summary="One of the school's subscription invoices as PDF",
)
def my_invoice_pdf(
    invoice_id: uuid.UUID,
    ctx: StaffContext = Depends(require(Capability.REPORTS_VIEW)),
    db: Session = Depends(get_db),
) -> Response:
    return _pdf_response(db, invoice_id)  # the session is scoped: another school's invoice is "not found"


# Paying the platform is a money action: proprietors and bursars (payments.record), not every viewer
PAY = Depends(require(Capability.PAYMENTS_RECORD))


@school_router.post(
    "/subscription/invoices/{invoice_id}/checkout",
    response_model=SubscriptionCheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pay a subscription bill by card: a checkout link for what's left on it",
)
def pay_invoice_by_card(
    invoice_id: uuid.UUID, ctx: StaffContext = PAY, db: Session = Depends(get_db)
) -> SubscriptionCheckoutOut:
    get_limiter().hit("subscription-checkout:user", "20/hour", str(ctx.user.id))
    checkout = card_payments.start(db, ctx, invoice_id)
    return SubscriptionCheckoutOut(
        reference=checkout.reference, amount_kobo=checkout.amount_kobo, authorization_url=checkout.authorization_url
    )


@school_router.post(
    "/subscription/checkouts/verify",
    response_model=SubscriptionVerifyOut,
    summary="Back from the gateway: confirm a subscription card payment (the outcome comes from the gateway)",
)
def verify_subscription_payment(
    body: SubscriptionVerifyIn,
    ctx: StaffContext = Depends(require(Capability.REPORTS_VIEW)),
    db: Session = Depends(get_db),
) -> SubscriptionVerifyOut:
    get_limiter().hit("subscription-verify:user", "60/minute", str(ctx.user.id))
    # The scoped session finds only this school's checkouts
    if db.scalar(select(SubscriptionCheckout.id).where(SubscriptionCheckout.reference == body.reference)) is None:
        raise NotFound("Payment not found.")
    db.rollback()  # release the snapshot before confirming in its own transaction
    checkout = card_payments.confirm(body.reference)
    if checkout is None:
        raise NotFound("Payment not found.")
    return SubscriptionVerifyOut(
        reference=checkout.reference,
        status=checkout.status,
        amount_kobo=checkout.amount_kobo,
        review_reason=checkout.review_reason,
    )


@school_router.get("/school/onboarding", response_model=list[OnboardingStepOut], summary="Getting-started checklist")
def school_onboarding(
    ctx: StaffContext = Depends(require(Capability.SCHOOL_VIEW)), db: Session = Depends(get_db)
) -> list[OnboardingStepOut]:
    school = db.get(School, ctx.school_id)
    if school is None:
        raise NotFound("School not found.")
    return [OnboardingStepOut.model_validate(s) for s in onboarding.checklist(db, school)]


class ProviderOut(BaseModel):
    service: str
    active_provider: GatewayCode
    available: list[GatewayCode] = Field(description="Providers configured on this platform (keys set)")
    updated_at: datetime | None


class ProviderIn(BaseModel):
    provider: GatewayCode


@router.get("/providers", response_model=list[ProviderOut], summary="Which provider each service uses right now")
def get_providers(admin: Principal = ADMIN, db: Session = Depends(get_db)) -> list[ProviderOut]:
    return [ProviderOut.model_validate(providers.payment_provider(db), from_attributes=True)]


@router.put(
    "/providers/payment",
    response_model=ProviderOut,
    summary="Switch the payment provider for new checkouts (payments under way finish where they began)",
)
def put_payment_provider(body: ProviderIn, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> ProviderOut:
    view = providers.set_payment_provider(db, body.provider, actor=admin.actor, meta=admin.meta, user_id=admin.user.id)
    return ProviderOut.model_validate(view, from_attributes=True)


class SmsProviderOut(BaseModel):
    service: str
    active_provider: sms_providers.SmsProvider | None = Field(description="None when no SMS provider has keys")
    available: list[sms_providers.SmsProvider] = Field(description="Providers configured on this platform (keys set)")
    updated_at: datetime | None
    sending: bool = Field(description="False in development: texts are only logged")


class SmsProviderIn(BaseModel):
    provider: sms_providers.SmsProvider


@router.get("/providers/sms", response_model=SmsProviderOut, summary="Which SMS provider texts go through")
def get_sms_provider(admin: Principal = ADMIN, db: Session = Depends(get_db)) -> SmsProviderOut:
    return SmsProviderOut.model_validate(sms_providers.sms_provider(db), from_attributes=True)


@router.put(
    "/providers/sms",
    response_model=SmsProviderOut,
    summary="Switch the SMS provider (texts already sent aren't affected; queued ones use the new one)",
)
def put_sms_provider(body: SmsProviderIn, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> SmsProviderOut:
    view = sms_providers.set_sms_provider(db, body.provider, actor=admin.actor, meta=admin.meta, user_id=admin.user.id)
    return SmsProviderOut.model_validate(view, from_attributes=True)


# --- Problems to fix ---------------------------------------------------------------------------------


class WebhookProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    gateway: GatewayCode
    status: Literal["needs_review", "failed"]
    event_type: str
    reference: str | None
    school_name: str | None
    claimed_amount_kobo: int | None
    note: str
    attempts: int
    received_at: datetime


class CheckoutProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["school_fees", "subscription"]
    reference: str
    school_name: str
    amount_kobo: int
    review_reason: str
    created_at: datetime


class TaskProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_name: str
    attempts: int
    last_error: str
    created_at: datetime


class MessageProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    school_name: str
    failed: int
    last_error: str


class ProblemsOut(BaseModel):
    webhooks: list[WebhookProblemOut]
    checkouts: list[CheckoutProblemOut]
    tasks: list[TaskProblemOut]
    messages: list[MessageProblemOut] = Field(description="Messages that failed in the last 7 days, per school")


class ResolveIn(BaseModel):
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


@router.get("/problems", response_model=ProblemsOut, summary="Everything stuck across schools that needs a person")
def get_problems(admin: Principal = ADMIN, db: Session = Depends(get_db)) -> ProblemsOut:
    found = problems.problems(db)
    return ProblemsOut(
        webhooks=[WebhookProblemOut.model_validate(w) for w in found.webhooks],
        checkouts=[CheckoutProblemOut.model_validate(c) for c in found.checkouts],
        tasks=[TaskProblemOut.model_validate(t) for t in found.tasks],
        messages=[MessageProblemOut.model_validate(m) for m in found.messages],
    )


@router.post(
    "/problems/webhooks/{event_id}/retry",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Process a parked payment notification again",
)
def retry_webhook(event_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> None:
    problems.retry_webhook(db, admin, event_id)


@router.post(
    "/problems/webhooks/{event_id}/resolve",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close a parked payment notification that was dealt with by hand",
)
def resolve_webhook(
    event_id: uuid.UUID, body: ResolveIn, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> None:
    problems.resolve_webhook(db, admin, event_id, body.note)


@router.post(
    "/problems/checkouts/{reference}/resolve",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record how a card payment in review was sorted out",
)
def resolve_checkout(
    body: ResolveIn,
    reference: str = Path(pattern=r"^(SP|WS)[0-9A-F]{20}$"),
    admin: Principal = ADMIN,
    db: Session = Depends(get_db),
) -> None:
    problems.resolve_checkout(db, admin, reference, body.note)


@router.post(
    "/problems/tasks/{task_id}/retry",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Put a failed background task back in the queue",
)
def retry_task(task_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)) -> None:
    problems.retry_task(db, admin, task_id)


# --- Helping a school's staff ------------------------------------------------------------------------


class LinkSentOut(BaseModel):
    sent: Literal["invite", "reset"]


@router.post(
    "/schools/{school_id}/staff/{user_id}/password-link",
    response_model=LinkSentOut,
    summary="Email a staff member their invite again, or a password-reset link",
)
def send_staff_password_link(
    school_id: uuid.UUID, user_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> LinkSentOut:
    get_limiter().hit("platform-staff-link:user", "30/hour", str(admin.user.id))
    return LinkSentOut(sent=staff_support.send_link(db, admin, school_id, user_id))


@router.post(
    "/schools/{school_id}/staff/{user_id}/reset-mfa",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn off a staff member's two-factor sign-in (lost phone); they enrol again at next sign-in",
)
def reset_staff_mfa(
    school_id: uuid.UUID, user_id: uuid.UUID, admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> None:
    staff_support.reset_two_factor(db, admin, school_id, user_id)


# --- Trends ------------------------------------------------------------------------------------------


class TrendMonthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    month: str = Field(description='"2026-10"')
    processed_kobo: int = Field(description="Money parents paid schools through WardPay, net of reversals")
    payments: int
    subscription_received_kobo: int
    schools_joined: int
    schools_suspended: int


@router.get("/trends", response_model=list[TrendMonthOut], summary="The platform month by month, oldest first")
def get_trends(
    months: int = Query(default=12, ge=1, le=trends.MAX_MONTHS), admin: Principal = ADMIN, db: Session = Depends(get_db)
) -> list[TrendMonthOut]:
    return [TrendMonthOut.model_validate(m) for m in trends.trends(db, months)]
