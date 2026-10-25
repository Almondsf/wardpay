"""
Family payment links over HTTP: the public page a parent opens from a message (no
sign-in; the token is the key) and the staff endpoints to copy or replace a parent's link.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.http import RequestMeta, request_meta
from app.core.ratelimit import get_limiter
from app.core.tenancy import scope_to_school
from app.modules.accounts.deps import StaffContext, require
from app.modules.accounts.permissions import Capability
from app.modules.fees.router import PlanOfferOut, offer_out
from app.modules.fees.setup_service import MAX_AMOUNT_KOBO
from app.modules.reports.router import FeeLineOut, breakdown_out
from app.modules.schools.models import FamilySplitRule, School

from . import family, family_links
from .family_split import load_debts

public_router = APIRouter(prefix="/public/pay", tags=["public"], responses=ERROR_RESPONSES)
router = APIRouter(tags=["payments"], responses=ERROR_RESPONSES)
RECORD = Depends(require(Capability.PAYMENTS_RECORD))

Token = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{20,64}$")]
EXPIRED = "This payment link has expired or been replaced. Ask the school to send you a new one."


class FamilyChildOut(BaseModel):
    student_id: uuid.UUID
    first_name: str
    class_name: str | None
    owed_kobo: int
    suggested_kobo: int = Field(description="What to pay now: everything owed, or up to the next instalment on a plan")
    on_plan: bool
    account_number: str | None = Field(description="The child's own account number for transfers")
    bank_name: str | None
    fees: list[FeeLineOut] = Field(description="This term's fees one by one, in bill order")
    earlier_left_kobo: int = Field(description="Still owed from earlier terms")
    plan_offers: list[PlanOfferOut] = Field(description="The school's instalment plans the parent can choose")


class FamilyPageOut(BaseModel):
    """No surnames: a forwarded link shows first names, classes and amounts only."""

    school_name: str
    card_payments: bool = Field(description="Whether the school takes card payments")
    min_part_kobo: int = Field(description="Smallest card payment per child (unless less is owed)")
    split_rule: FamilySplitRule = Field(description="How the school shares one amount between the children")
    children: list[FamilyChildOut]


class FamilyPartIn(BaseModel):
    student_id: uuid.UUID
    amount_kobo: int = Field(ge=0, le=MAX_AMOUNT_KOBO, description="0: not this time")
    fees: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list,
        max_length=30,
        description="Pay for these fees (from the breakdown); empty: tuition first, then the rest",
    )


class FamilyCheckoutIn(BaseModel):
    """Either one amount for the family (shared by the school's rule) or the amount per child."""

    total_kobo: int | None = Field(default=None, gt=0, le=MAX_AMOUNT_KOBO)
    items: list[FamilyPartIn] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def one_way(self) -> "FamilyCheckoutIn":
        if (self.total_kobo is None) == (self.items is None):
            raise ValueError("Send either total_kobo or items.")
        return self


class FamilySplitIn(BaseModel):
    total_kobo: int = Field(gt=0, le=MAX_AMOUNT_KOBO)


class FamilyShareOut(BaseModel):
    student_id: uuid.UUID
    amount_kobo: int
    clears: bool = Field(description="This pays everything the child owes")


class FamilySplitOut(BaseModel):
    split_rule: FamilySplitRule
    shares: list[FamilyShareOut]


class FamilyCheckoutOut(BaseModel):
    reference: str
    amount_kobo: int
    authorization_url: str


class FamilyLinkOut(BaseModel):
    url: str
    expires_at: datetime


def _link(
    token: Token, db: Session = Depends(get_db), meta: RequestMeta = Depends(request_meta)
) -> family_links.Resolved:
    """The live link for the token, with the session scoped to its school. The same answer for every bad token."""
    get_limiter().hit("family-link:ip", "60/minute", meta.ip)
    found = family_links.resolve(token)
    if found is None:
        raise NotFound(EXPIRED)
    scope_to_school(db, found.school_id)
    return found


@public_router.get("/{token}", response_model=FamilyPageOut, summary="A parent's family payment page (no sign-in)")
def family_page(found: family_links.Resolved = Depends(_link), db: Session = Depends(get_db)) -> FamilyPageOut:
    return page_out(family.family_page(db, found))


def page_out(page: family.FamilyPage) -> FamilyPageOut:
    return FamilyPageOut(
        school_name=page.school_name,
        card_payments=page.card_payments,
        min_part_kobo=family.MIN_PART_KOBO,
        split_rule=page.split_rule,
        children=[
            FamilyChildOut(
                student_id=c.student_id,
                first_name=c.first_name,
                class_name=c.class_name,
                owed_kobo=c.owed_kobo,
                suggested_kobo=c.suggested_kobo,
                on_plan=c.on_plan,
                account_number=c.account.account_number if c.account else None,
                bank_name=c.account.bank_name if c.account else None,
                fees=breakdown_out(c.breakdown).fees,
                earlier_left_kobo=c.breakdown.earlier_left_kobo,
                plan_offers=[offer_out(o) for o in c.plan_offers],
            )
            for c in page.children
        ],
    )


class FamilyPlanIn(BaseModel):
    invoice_id: uuid.UUID
    template_id: uuid.UUID


@public_router.post(
    "/{token}/plan",
    response_model=FamilyPageOut,
    summary="Choose one of the school's instalment plans for a child's bill (its plan charge is added)",
)
def family_choose_plan(
    body: FamilyPlanIn,
    found: family_links.Resolved = Depends(_link),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(request_meta),
) -> FamilyPageOut:
    get_limiter().hit("family-plan:link", "10/hour", str(found.link_id))
    family.choose_plan(db, found, body.invoice_id, body.template_id, meta)
    return page_out(family.family_page(db, found))


@public_router.post(
    "/{token}/checkout",
    response_model=FamilyCheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pay for several children at once (card, transfer or USSD), with the amount chosen per child",
)
def family_checkout(
    body: FamilyCheckoutIn,
    found: family_links.Resolved = Depends(_link),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(request_meta),
) -> FamilyCheckoutOut:
    get_limiter().hit("family-link:checkout", "20/hour", str(found.link_id))
    if body.total_kobo is not None:
        intent = family.start_family_checkout(db, found, body.total_kobo, meta)
    else:
        parts = [(p.student_id, p.amount_kobo, p.fees) for p in body.items or []]
        intent = family.start_checkout(db, found, parts, meta)
    return FamilyCheckoutOut(
        reference=intent.reference, amount_kobo=intent.amount_kobo, authorization_url=intent.authorization_url
    )


@public_router.post(
    "/{token}/split",
    response_model=FamilySplitOut,
    summary="How one amount would be shared between the children, by the school's rule (nothing is paid)",
)
def family_split_preview(
    body: FamilySplitIn, found: family_links.Resolved = Depends(_link), db: Session = Depends(get_db)
) -> FamilySplitOut:
    return split_out(db, found, body.total_kobo)


def split_out(db: Session, found: family_links.Resolved, total_kobo: int) -> FamilySplitOut:
    shares = family.family_shares(db, found, total_kobo)
    owed = {d.student_id: d.owed_kobo for d in load_debts(db, list(shares))}
    school = db.get(School, found.school_id)
    return FamilySplitOut(
        split_rule=school.family_split_rule if school else FamilySplitRule.CLEAR_MOST,
        shares=[
            FamilyShareOut(student_id=sid, amount_kobo=amount, clears=amount > 0 and amount >= owed.get(sid, 0))
            for sid, amount in shares.items()
        ],
    )


@router.get("/guardians/{guardian_id}/pay-link", response_model=FamilyLinkOut, summary="A parent's family payment link")
def get_family_link(guardian_id: uuid.UUID, ctx: StaffContext = RECORD, db: Session = Depends(get_db)) -> FamilyLinkOut:
    link = family_links.link_for(db, ctx, guardian_id)
    return FamilyLinkOut(url=family_links.link_url(link.token), expires_at=link.expires_at)


@router.post(
    "/guardians/{guardian_id}/pay-link/replace",
    response_model=FamilyLinkOut,
    summary="Replace a parent's family payment link (the old one stops working)",
)
def replace_family_link(
    guardian_id: uuid.UUID, ctx: StaffContext = RECORD, db: Session = Depends(get_db)
) -> FamilyLinkOut:
    link = family_links.reissue(db, ctx, guardian_id)
    return FamilyLinkOut(url=family_links.link_url(link.token), expires_at=link.expires_at)
