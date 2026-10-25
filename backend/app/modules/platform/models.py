"""
The platform's own business: subscription plans and what schools pay for WardPay.

The model is that of ParentPay in the UK (PLAN.md §3.4): the school pays, parents use it free.
A plan has a one-off set-up fee and a per-pupil fee per year that is billed a
third each term for the pupils enrolled that term. There is no licence fee
(owner, 2026-10-23).

Subscription invoices follow the same rules as school fee invoices: amounts in
kobo, lines are never edited (an invoice is voided and re-issued instead),
numbers are gapless, and what's paid comes from recorded payments.
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, UUIDPrimaryKey, enum_type
from app.core.crypto import EncryptedString
from app.core.tenancy import TenantMixin
from app.modules.payments.gateways.base import GatewayCode
from app.modules.payments.models import GATEWAY_ENUM, IntentStatus


class Plan(UUIDPrimaryKey, Timestamped, Base):
    """Prices a school pays. Platform-wide (not a school's data); edited by platform admins."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("setup_fee_kobo >= 0 AND per_pupil_year_kobo >= 0", name="prices_not_negative"),
        CheckConstraint("instalment_plan_fee_kobo >= 0", name="plan_fee_not_negative"),
        CheckConstraint("plan_charge_per_payment_kobo >= 0", name="per_payment_not_negative"),
        # Exactly one default plan (for schools without one of their own)
        Index("uq_plans_one_default", "is_default", unique=True, postgresql_where=text("is_default")),
    )

    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    setup_fee_kobo: Mapped[int] = mapped_column(BigInteger)
    per_pupil_year_kobo: Mapped[int] = mapped_column(BigInteger)
    # Charged to the school for each instalment plan made (owner, 2026-10-25: about ₦200, for the extra
    # reminders a plan sends); billed on the school's next termly bill
    instalment_plan_fee_kobo: Mapped[int] = mapped_column(BigInteger, default=20_000, server_default="20000")
    # WardPay's standard plan charge to the parent (owner, 2026-10-25): this per payment (Paystack's ₦300
    # transfer cap) plus the per-plan fee above, so the school recovers both. Schools may only charge more.
    plan_charge_per_payment_kobo: Mapped[int] = mapped_column(BigInteger, default=30_000, server_default="30000")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SubscriptionLineKind(enum.StrEnum):
    SETUP = "setup"  # once, on a school's first bill
    LICENCE = "licence"  # no longer billed (owner, 2026-10-23); kept so older bills still read
    PUPILS = "pupils"  # a third of the yearly per-pupil fee, for the pupils enrolled that term
    INSTALMENT_PLANS = "instalment_plans"  # the per-plan fee, for plans made since the last bill
    DISCOUNT = "discount"  # taken off by a platform admin (e.g. the set-up fee waived): the only negative line


class SubscriptionInvoice(TenantMixin, UUIDPrimaryKey, Base):
    """What a school owes the platform for one term."""

    __tablename__ = "subscription_invoices"
    __table_args__ = (
        UniqueConstraint("number"),
        # One live bill per school per term: generating again can't double-bill
        Index(
            "uq_subscription_invoices_one_per_term",
            "school_id",
            "session_name",
            "term_number",
            unique=True,
            postgresql_where=text("voided_at IS NULL"),
        ),
        CheckConstraint("term_number BETWEEN 1 AND 3", name="term_range"),
        CheckConstraint("(voided_at IS NULL) = (void_reason IS NULL)", name="void_has_reason"),
    )

    number: Mapped[int] = mapped_column(BigInteger)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"))
    session_name: Mapped[str] = mapped_column(String(20))  # "2026/2027"
    term_number: Mapped[int] = mapped_column(SmallInteger)
    pupils: Mapped[int] = mapped_column(Integer)  # enrolled when the bill was made
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_date: Mapped[date] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(300))


class SubscriptionLine(TenantMixin, UUIDPrimaryKey, Base):
    """One charge on a subscription invoice. Append-only (database trigger)."""

    __tablename__ = "subscription_lines"
    __table_args__ = (
        # Charges are never negative; a discount is always negative
        CheckConstraint(
            "quantity >= 0 AND CASE WHEN kind = 'discount' THEN amount_kobo < 0 ELSE amount_kobo >= 0 END",
            name="amount_sign",
        ),
        Index("ix_subscription_lines_invoice", "invoice_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subscription_invoices.id", ondelete="RESTRICT"))
    kind: Mapped[SubscriptionLineKind] = mapped_column(enum_type(SubscriptionLineKind, "subscription_line_kind"))
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_kobo: Mapped[int] = mapped_column(BigInteger)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)


class SubscriptionPayment(TenantMixin, UUIDPrimaryKey, Base):
    """
    Money a school paid the platform. Append-only. Either a bank transfer to the platform's account
    that a platform admin recorded (`recorded_by`), or a card payment the gateway confirmed
    (`gateway_reference`, unique, so a payment is never recorded twice).
    """

    __tablename__ = "subscription_payments"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("recorded_by IS NOT NULL OR gateway_reference IS NOT NULL", name="recorded_or_gateway"),
        CheckConstraint("(gateway IS NULL) = (gateway_reference IS NULL)", name="gateway_with_reference"),
        UniqueConstraint("gateway_reference"),
        Index("ix_subscription_payments_invoice", "invoice_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subscription_invoices.id", ondelete="RESTRICT"))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    received_on: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(100), default="")
    recorded_by: Mapped[uuid.UUID | None] = mapped_column()  # the platform admin; None for a card payment
    gateway: Mapped[GatewayCode | None] = mapped_column(GATEWAY_ENUM)
    gateway_reference: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionCheckout(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    A school paying its subscription bill by card. The money settles to the platform's own
    gateway balance (no school subaccount: it's the platform's income, not school money).
    The outcome always comes from the gateway, never the browser.
    """

    __tablename__ = "subscription_checkouts"
    __table_args__ = (
        UniqueConstraint("reference"),
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        Index("ix_subscription_checkouts_invoice", "invoice_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subscription_invoices.id", ondelete="RESTRICT"))
    reference: Mapped[str] = mapped_column(String(40))
    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    email: Mapped[str] = mapped_column(EncryptedString)
    status: Mapped[IntentStatus] = mapped_column(
        enum_type(IntentStatus, "payment_intent_status"), default=IntentStatus.PENDING
    )
    authorization_url: Mapped[str] = mapped_column(String(500), default="")
    provider_reference: Mapped[str] = mapped_column(String(100), default="")
    review_reason: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[uuid.UUID] = mapped_column()
    # A platform admin's note on how a payment in review was sorted out (the money record isn't changed)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column()
    resolution_note: Mapped[str] = mapped_column(String(300), default="", server_default="")
