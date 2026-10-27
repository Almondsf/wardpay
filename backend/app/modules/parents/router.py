import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES
from app.core.http import RequestMeta, request_meta
from app.core.ratelimit import get_limiter
from app.modules.fees import invoicing, plan_offers
from app.modules.fees.models import Invoice, InvoiceKind, LineKind
from app.modules.fees.plans import PlanView, plan_views
from app.modules.fees.router import PaymentPlanOut, PlanOfferOut, offer_out
from app.modules.fees.setup_service import MAX_AMOUNT_KOBO
from app.modules.payments import checkout, family, family_links
from app.modules.payments import service as payment_service
from app.modules.payments.family_router import (
    FamilyCheckoutIn,
    FamilyCheckoutOut,
    FamilyPageOut,
    FamilySplitIn,
    FamilySplitOut,
    page_out,
    split_out,
)
from app.modules.payments.models import PaymentKind, PaymentMethod
from app.modules.receipts.pdf import render_receipt
from app.modules.receipts.service import is_reversed, receipts_for_payments
from app.modules.reports.router import FeeLineOut, breakdown_out

from . import auth_service, branches, portal
from .deps import ParentContext, get_parent, get_parent_across_branches

REFRESH_COOKIE = "sp_parent_refresh"
REFRESH_PATH = "/api/v1/parent/auth"

auth_router = APIRouter(prefix="/parent/auth", tags=["parent-auth"], responses=ERROR_RESPONSES)
router = APIRouter(prefix="/parent", tags=["parent"], responses=ERROR_RESPONSES)
Phone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=7, max_length=25)]


# --- Schemas ---------------------------------------------------------------------------------------


class CodeRequestIn(BaseModel):
    phone: Phone


class CodeVerifyIn(BaseModel):
    phone: Phone
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]


class SchoolOption(BaseModel):
    guardian_id: uuid.UUID
    school_name: str
    children: list[str] = Field(description="First names of the parent's children there")


class ParentSignedIn(BaseModel):
    status: Literal["signed_in"] = "signed_in"
    access_token: str
    expires_in: int


class ParentChooseSchool(BaseModel):
    status: Literal["choose_school"] = "choose_school"
    ticket: str
    schools: list[SchoolOption]


ParentLoginResult = Annotated[ParentSignedIn | ParentChooseSchool, Field(discriminator="status")]


class ChooseSchoolIn(BaseModel):
    ticket: Annotated[str, StringConstraints(max_length=2000)]
    guardian_id: uuid.UUID


class PayInAccount(BaseModel):
    account_number: str
    account_name: str
    bank_name: str


class ChildOut(BaseModel):
    id: uuid.UUID
    full_name: str
    first_name: str
    admission_number: str
    class_name: str | None
    owed_kobo: int
    paid_kobo: int
    credit_kobo: int
    pay_in: PayInAccount | None
    fees: list[FeeLineOut] = Field(description="This term's fees one by one, in bill order")
    earlier_left_kobo: int = Field(description="Still owed from earlier terms")


class HomeOut(BaseModel):
    guardian_name: str
    school_name: str
    children: list[ChildOut]


class ParentInvoiceOut(BaseModel):
    id: uuid.UUID
    number: int
    kind: InvoiceKind
    issued_at: datetime
    due_date: date | None
    total_kobo: int
    paid_kobo: int
    balance_kobo: int
    status: str
    plan: PaymentPlanOut | None = None
    plan_offers: list[PlanOfferOut] = Field(default_factory=list, description="The school's plans this bill can go on")


class ParentLineOut(BaseModel):
    kind: LineKind
    description: str
    amount_kobo: int


class ParentInvoiceDetailOut(ParentInvoiceOut):
    lines: list[ParentLineOut]


class ParentPaymentOut(BaseModel):
    id: uuid.UUID
    kind: PaymentKind
    method: PaymentMethod
    amount_kobo: int
    received_at: datetime
    reversed: bool
    receipt_id: uuid.UUID | None
    receipt_number: int | None


class ChildAccountOut(BaseModel):
    child: ChildOut
    invoices: list[ParentInvoiceOut]
    payments: list[ParentPaymentOut]


class PayIn(BaseModel):
    amount_kobo: Annotated[int, Field(gt=0, le=MAX_AMOUNT_KOBO)] | None = Field(
        default=None, description="Default: everything owed"
    )


class CheckoutOut(BaseModel):
    reference: str
    amount_kobo: int
    authorization_url: str


# --- Sign-in ---------------------------------------------------------------------------------------


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_days * 86400,
        path=REFRESH_PATH,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
    )


def _signed_in(response: Response, issued: auth_service.IssuedParentSession) -> ParentSignedIn:
    _set_cookie(response, issued.refresh_token)
    return ParentSignedIn(access_token=issued.access_token, expires_in=issued.expires_in)


@auth_router.post(
    "/request-code",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Text a sign-in code to a parent's phone (same answer whether or not it's registered)",
)
def request_code(
    body: CodeRequestIn, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> dict[str, str]:
    settings, limiter = get_settings(), get_limiter()
    limiter.hit("otp-request:ip", settings.otp_rate_per_ip, meta.ip)
    _, index = auth_service.normalized(body.phone)
    limiter.hit("otp-request:phone", settings.otp_rate_per_phone, index)
    auth_service.request_code(db, body.phone)
    return {"detail": "If this number is registered with a school, a code is on its way."}


@auth_router.post("/verify", response_model=ParentLoginResult, summary="Sign in with the texted code")
def verify(
    body: CodeVerifyIn, response: Response, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> ParentSignedIn | ParentChooseSchool:
    limiter = get_limiter()
    limiter.hit("otp-verify:ip", "30/10minutes", meta.ip)
    _, index = auth_service.normalized(body.phone)
    limiter.hit("otp-verify:phone", "10/10minutes", index)
    result = auth_service.verify_code(db, body.phone, body.code, meta)
    if isinstance(result, auth_service.ChooseSchool):
        return ParentChooseSchool(
            ticket=result.ticket,
            schools=[
                SchoolOption(guardian_id=c.guardian_id, school_name=c.school_name, children=list(c.children))
                for c in result.schools
            ],
        )
    return _signed_in(response, result)


@auth_router.post("/choose-school", response_model=ParentSignedIn, summary="Pick a school (children at several)")
def choose_school(
    body: ChooseSchoolIn, response: Response, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> ParentSignedIn:
    get_limiter().hit("otp-verify:ip", "30/10minutes", meta.ip)
    return _signed_in(response, auth_service.choose_school(db, body.ticket, body.guardian_id, meta))


@auth_router.post("/refresh", response_model=ParentSignedIn, summary="New access token from the refresh cookie")
def refresh(
    response: Response,
    meta: RequestMeta = Depends(request_meta),
    db: Session = Depends(get_db),
    sp_parent_refresh: str | None = Cookie(default=None, include_in_schema=False),
) -> ParentSignedIn:
    get_limiter().hit("refresh:ip", "60/minute", meta.ip)
    try:
        issued = auth_service.refresh(db, sp_parent_refresh or "", meta)
    except Exception:
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
        raise
    return _signed_in(response, issued)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out this device")
def logout(response: Response, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)) -> None:
    auth_service.sign_out(db, ctx.session_id)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)


class SwitchBranchIn(BaseModel):
    guardian_id: uuid.UUID = Field(description="One of this parent's records from GET /parent/branches")


@auth_router.post(
    "/switch-branch",
    response_model=ParentSignedIn,
    summary="Children in several branches of a school group: look at another branch (no new code)",
)
def switch_branch(
    body: SwitchBranchIn, ctx: ParentContext = Depends(get_parent_across_branches), db: Session = Depends(get_db)
) -> ParentSignedIn:
    get_limiter().hit("parent-branch:session", "60/minute", str(ctx.session_id))
    issued = branches.switch(db, ctx, body.guardian_id)
    return ParentSignedIn(access_token=issued.access_token, expires_in=issued.expires_in)


# --- Portal ----------------------------------------------------------------------------------------


class ParentBranchOut(BaseModel):
    guardian_id: uuid.UUID
    school_name: str
    children: list[str] = Field(description="First names of the children at that branch")
    owed_kobo: int
    current: bool = Field(description="The branch this device is looking at")


@router.get(
    "/branches",
    response_model=list[ParentBranchOut],
    summary="Children in several branches of a school group: what's owed at each (empty for other schools)",
)
def list_branches(
    ctx: ParentContext = Depends(get_parent_across_branches), db: Session = Depends(get_db)
) -> list[ParentBranchOut]:
    rows = branches.branches(db, ctx)
    if len(rows) < 2:
        return []
    return [
        ParentBranchOut(
            guardian_id=b.guardian_id,
            school_name=b.school_name,
            children=list(b.children),
            owed_kobo=b.owed_kobo,
            current=b.current,
        )
        for b in rows
    ]


def _child_out(child: portal.ChildOverview) -> ChildOut:
    va = child.virtual_account
    return ChildOut(
        id=child.student.id,
        full_name=child.student.full_name,
        first_name=child.student.first_name,
        admission_number=child.student.admission_number,
        class_name=child.class_name,
        owed_kobo=child.summary.outstanding_kobo,
        paid_kobo=child.summary.paid_kobo,
        credit_kobo=child.credit_kobo,
        pay_in=PayInAccount(account_number=va.account_number, account_name=va.account_name, bank_name=va.bank_name)
        if va
        else None,
        fees=breakdown_out(child.breakdown).fees,
        earlier_left_kobo=child.breakdown.earlier_left_kobo,
    )


def _invoice_out(
    invoice: Invoice, total: int, paid: int, plan: PlanView | None = None, offers: list[plan_offers.Offer] | None = None
) -> ParentInvoiceOut:
    amounts = invoicing.InvoiceAmounts(int(total), int(paid))
    return ParentInvoiceOut(
        plan=PaymentPlanOut.model_validate(plan) if plan else None,
        plan_offers=[offer_out(o) for o in offers or []],
        id=invoice.id,
        number=invoice.number,
        kind=invoice.kind,
        issued_at=invoice.issued_at,
        due_date=invoice.due_date,
        total_kobo=amounts.total_kobo,
        paid_kobo=amounts.paid_kobo,
        balance_kobo=amounts.balance_kobo,
        status=invoicing.status_of(invoice, amounts),
    )


@router.get("/home", response_model=HomeOut, summary="The parent's children with balances and account numbers")
def home(ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)) -> HomeOut:
    house = portal.household(db, ctx)
    return HomeOut(
        guardian_name=house.guardian_name,
        school_name=house.school_name,
        children=[_child_out(c) for c in house.children],
    )


@router.get("/children/{student_id}", response_model=ChildAccountOut, summary="A child's invoices and payments")
def child_account(
    student_id: uuid.UUID, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> ChildAccountOut:
    student = portal.require_child(db, ctx, student_id)
    overview = next(c for c in portal.household(db, ctx).children if c.student.id == student.id)
    invoices = db.execute(invoicing.invoices_query(student_id=student.id).where(Invoice.voided_at.is_(None))).all()
    plans = plan_views(db, [inv.id for inv, *_ in invoices])
    offered = plan_offers.offers(db, ctx.school_id, [student.id])
    payments = [p for p, _ in db.execute(payment_service.payments_query(student_id=student.id).limit(100)).all()]
    ids = [p.id for p in payments]
    receipts, reversed_ = receipts_for_payments(db, ids), payment_service.reversed_ids(db, ids)
    return ChildAccountOut(
        child=_child_out(overview),
        invoices=[
            _invoice_out(inv, total, paid, plans.get(inv.id), offered.get(inv.id))
            for inv, _student, total, paid in invoices
        ],
        payments=[
            ParentPaymentOut(
                id=p.id,
                kind=p.kind,
                method=p.method,
                amount_kobo=p.amount_kobo,
                received_at=p.received_at,
                reversed=p.id in reversed_,
                receipt_id=receipts[p.id].id if p.id in receipts else None,
                receipt_number=receipts[p.id].number if p.id in receipts else None,
            )
            for p in payments
        ],
    )


@router.get("/invoices/{invoice_id}", response_model=ParentInvoiceDetailOut, summary="One invoice with its lines")
def invoice_detail(
    invoice_id: uuid.UUID, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> ParentInvoiceDetailOut:
    portal.require_child_invoice(db, ctx, invoice_id)
    invoice, _student, amounts = invoicing.invoice_with_lines(db, invoice_id)
    base = _invoice_out(invoice, amounts.total_kobo, amounts.paid_kobo, plan_views(db, [invoice.id]).get(invoice.id))
    return ParentInvoiceDetailOut(
        **base.model_dump(),
        lines=[
            ParentLineOut(kind=line.kind, description=line.description, amount_kobo=line.amount_kobo)
            for line in invoice.lines
        ],
    )


class ChoosePlanIn(BaseModel):
    template_id: uuid.UUID


@router.post(
    "/invoices/{invoice_id}/plan",
    response_model=ParentInvoiceDetailOut,
    summary="Choose one of the school's instalment plans for this bill (its plan charge is added)",
)
def choose_plan(
    invoice_id: uuid.UUID, body: ChoosePlanIn, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> ParentInvoiceDetailOut:
    portal.require_child_invoice(db, ctx, invoice_id)
    plan_offers.choose(
        db,
        invoice_id,
        body.template_id,
        school_id=ctx.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        created_by=None,
        staff=False,
    )
    return invoice_detail(invoice_id, ctx, db)


@router.post(
    "/children/{student_id}/pay",
    response_model=CheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pay online for one child: card, transfer or USSD (returns the gateway's checkout page)",
)
def pay(
    student_id: uuid.UUID, body: PayIn, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> CheckoutOut:
    get_limiter().hit("parent-pay:guardian", "20/hour", str(ctx.guardian_id))
    student = portal.require_child(db, ctx, student_id)
    intent = checkout.start_checkout(
        db,
        student_id=student.id,
        amount_kobo=body.amount_kobo,
        email=portal.parent_email(db, ctx),
        actor=ctx.actor,
        meta=ctx.meta,
        created_by=None,
    )
    return CheckoutOut(
        reference=intent.reference, amount_kobo=intent.amount_kobo, authorization_url=intent.authorization_url
    )


@router.get(
    "/family",
    response_model=FamilyPageOut,
    summary="All the parent's children, to pay for them at once (the same as their family payment link)",
)
def family_page(ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)) -> FamilyPageOut:
    return page_out(family.family_page(db, family_links.resolved_for(db, ctx.guardian_id, ctx.school_id)))


@router.post(
    "/family/checkout",
    response_model=FamilyCheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pay for several children at once (card, transfer or USSD): one amount, or chosen per child",
)
def family_checkout(
    body: FamilyCheckoutIn, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> FamilyCheckoutOut:
    get_limiter().hit("parent-pay:guardian", "20/hour", str(ctx.guardian_id))
    found = family_links.resolved_for(db, ctx.guardian_id, ctx.school_id)
    if body.total_kobo is not None:  # one amount, shared by the school's rule
        intent = family.start_family_checkout(db, found, body.total_kobo, ctx.meta)
    else:
        parts = [(p.student_id, p.amount_kobo, p.fees) for p in body.items or []]
        intent = family.start_checkout(db, found, parts, ctx.meta)
    return FamilyCheckoutOut(
        reference=intent.reference, amount_kobo=intent.amount_kobo, authorization_url=intent.authorization_url
    )


@router.post(
    "/family/split",
    response_model=FamilySplitOut,
    summary="How one amount would be shared between the children, by the school's rule (nothing is paid)",
)
def family_split(
    body: FamilySplitIn, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> FamilySplitOut:
    get_limiter().hit("parent-split:guardian", "120/hour", str(ctx.guardian_id))
    return split_out(db, family_links.resolved_for(db, ctx.guardian_id, ctx.school_id), body.total_kobo)


@router.get(
    "/receipts/{receipt_id}/pdf",
    summary="Download a receipt",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def receipt_pdf(
    receipt_id: uuid.UUID, ctx: ParentContext = Depends(get_parent), db: Session = Depends(get_db)
) -> Response:
    receipt = portal.require_child_receipt(db, ctx, receipt_id)
    content = render_receipt(receipt, reversed_=is_reversed(db, receipt.payment_id))
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="receipt-{receipt.number}.pdf"'},
    )
