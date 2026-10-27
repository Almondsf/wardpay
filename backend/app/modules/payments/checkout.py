"""
Card payments through the gateway's hosted checkout (no card data touches us).

1. `start_checkout()` creates a PaymentIntent and gets a checkout link.
2. The parent pays on the gateway's page.
3. The browser comes back with the reference (`confirm()` via /payments/verify)
   and the gateway sends a webhook. Both end up in `confirm()`, which asks the
   gateway what really happened and records the payment exactly once.

If the school has chosen that parents pay the provider's charge (`checkout_charges.py`), the
checkout is for the fees plus that charge (`charge_kobo`); the bills are only ever credited with
the fees.

A family checkout (`family.py`) pays for several children at once: `confirm()`
records one payment per child, for the amounts the parent chose, each with its own
gateway reference (`<reference>-1`, `-2`, ...) so it's still recorded only once.
"""

import logging
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import DomainError
from app.core.http import RequestMeta
from app.core.repo import get_or_404
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record
from app.modules.fees.invoicing import account_summaries
from app.modules.fees.setup_service import MAX_AMOUNT_KOBO
from app.modules.schools.models import School
from app.modules.students.models import Guardian, Student

from .accounts import require_payment_account
from .checkout_charges import ChargePayer, parent_charge
from .earmarks import Earmark
from .gateways import GatewayCode, TransactionStatus, get_gateway
from .gateways.base import parse_paid_at
from .models import IntentStatus, PaymentIntent, PaymentIntentItem, method_for_channel
from .service import NewPayment, record_payments

logger = logging.getLogger(__name__)


def checkout_charge(session: Session, school_id: uuid.UUID, gateway: GatewayCode, amount_kobo: int) -> int:
    """The provider's charge the parent pays on top of `amount_kobo`: 0 unless the school chose so."""
    payer = session.scalar(select(School.checkout_charge_payer).where(School.id == school_id))
    return parent_charge(gateway, payer or ChargePayer.SCHOOL, amount_kobo)


def new_reference() -> str:
    return "SP" + secrets.token_hex(10).upper()


def checkout_email(guardian: Guardian, school: School) -> str:
    """The gateway needs an email: the parent's, or a routing address if the school has none on file."""
    return guardian.email or f"guardian-{guardian.id}@{school.slug}.wardpay.ng"


def start_checkout(
    session: Session,
    *,
    student_id: uuid.UUID,
    amount_kobo: int | None,
    email: str,
    actor: Actor,
    meta: RequestMeta,
    created_by: uuid.UUID | None,
) -> PaymentIntent:
    """A checkout link for paying (part of) a student's fees. Amount defaults to everything owed."""
    account = require_payment_account(session)
    student = get_or_404(session, Student, student_id, label="Student", field="student_id")
    owed = account_summaries(session, [student.id])[student.id].outstanding_kobo
    amount = amount_kobo if amount_kobo is not None else owed
    if not 0 < amount <= MAX_AMOUNT_KOBO:
        raise DomainError(
            "Nothing is owed. Enter the amount to pay." if amount == 0 else "Enter a valid amount.", field="amount_kobo"
        )
    intent = PaymentIntent(
        id=uuid.uuid4(),
        reference=new_reference(),
        student_id=student.id,
        gateway=account.gateway,
        amount_kobo=amount,
        charge_kobo=checkout_charge(session, student.school_id, account.gateway, amount),
        email=email,
        created_by=created_by,
    )
    session.add(intent)
    settings = get_settings()
    checkout = get_gateway(account.gateway).initialize_payment(
        reference=intent.reference,
        amount_kobo=amount + intent.charge_kobo,
        email=email,
        subaccount_code=account.subaccount_code,
        callback_url=f"{settings.frontend_url}{settings.payment_callback_path}",
        description=f"School fees: {student.full_name} ({student.admission_number})",
        platform_fee_bp=account.platform_fee_bp,
    )
    intent.authorization_url, intent.provider_reference = checkout.authorization_url, checkout.provider_reference
    record(
        session,
        AuditAction.PAYMENT_LINK_CREATED,
        school_id=student.school_id,
        actor=actor,
        meta=meta,
        target=intent,
        changes={
            "student": student.id,
            "amount_kobo": amount,
            "charge_kobo": intent.charge_kobo,
            "reference": intent.reference,
        },
    )
    session.commit()
    return intent


def confirm(reference: str, payment_id: str | None = None) -> PaymentIntent | None:
    """
    Settle a checkout from the gateway's own record. Safe to call any number of
    times, from the redirect and the webhook at once. Returns None for an
    unknown reference.
    """
    with SessionLocal() as lookup:
        scope_platform_wide(lookup)
        school_id = lookup.scalar(select(PaymentIntent.school_id).where(PaymentIntent.reference == reference))
    if school_id is None:
        return None
    with SessionLocal() as session:
        scope_to_school(session, school_id)
        # The lock makes the redirect and the webhook take turns
        intent = session.scalar(select(PaymentIntent).where(PaymentIntent.reference == reference).with_for_update())
        if intent is None or intent.status != IntentStatus.PENDING:
            return intent
        verification = get_gateway(intent.gateway).verify(
            reference, provider_reference=intent.provider_reference or None, payment_id=payment_id
        )
        if verification.status == TransactionStatus.PENDING:
            return intent
        if verification.status == TransactionStatus.FAILED:
            intent.status = IntentStatus.FAILED
            session.commit()
            return intent
        items = list(
            session.scalars(
                select(PaymentIntentItem)
                .where(PaymentIntentItem.intent_id == intent.id)
                .order_by(PaymentIntentItem.position)
            )
        )
        expected = intent.amount_kobo + intent.charge_kobo
        # A family payment is split as chosen, and a payment with the parent's charge on top is credited with the
        # fees only: both need exactly the amount the checkout asked for
        wrong_total = (bool(items) or intent.charge_kobo > 0) and verification.amount_kobo != expected
        wrong_payment = verification.reference != reference  # a payment id that belongs to another checkout
        if verification.currency != "NGN" or verification.amount_kobo <= 0 or wrong_total or wrong_payment:
            intent.status = IntentStatus.NEEDS_REVIEW
            intent.review_reason = (
                f"Paid {verification.amount_kobo} kobo in {verification.currency or '?'}"
                + (f", expected {expected}" if wrong_total else "")
                + (f", for another reference ({verification.reference[:40]})" if wrong_payment else "")
            )
            record(
                session,
                AuditAction.PAYMENT_NEEDS_REVIEW,
                school_id=school_id,
                actor=Actor.system("gateway"),
                target=intent,
                changes={"reference": reference, "reason": intent.review_reason},
            )
            session.commit()
            logger.warning("Checkout %s needs review: %s", reference, intent.review_reason)
            return intent
        received_at = parse_paid_at(verification.paid_at)
        if items:  # a family checkout: each child's share, as the parent chose it
            parts = [
                (item.student_id, item.amount_kobo, f"{reference}-{item.position}", item.earmarks) for item in items
            ]
        elif intent.student_id is not None:  # the verified amount, less any charge the parent paid on top
            parts = [(intent.student_id, verification.amount_kobo - intent.charge_kobo, reference, [])]
        else:
            raise RuntimeError(f"Checkout {reference} has no child to pay for.")
        record_payments(
            session,
            school_id=school_id,
            payments=[
                NewPayment(
                    student_id=student_id,
                    amount_kobo=amount,
                    method=method_for_channel(verification.channel),
                    received_at=received_at,
                    gateway=intent.gateway,
                    gateway_reference=gateway_reference,
                    reference=reference,
                    earmarks=tuple(Earmark.from_json(e) for e in marks),
                )
                for student_id, amount, gateway_reference, marks in parts
            ],
        )
        intent.status = IntentStatus.SUCCEEDED
        session.commit()
        session.refresh(intent)
        return intent
