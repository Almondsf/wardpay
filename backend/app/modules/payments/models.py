"""
Payments.

- `SchoolPaymentAccount`: the school's gateway subaccount (its own bank
  account), through which every payment settles. We never hold school money.
- `VirtualAccount`: a student's dedicated bank account number. Any transfer
  into it belongs to that student.
- `PaymentIntent`: a card checkout we started (staff payment link, parent portal, or a
  family link paying for several children: one `PaymentIntentItem` per child).
- `FamilyPayLink`: a link a parent keeps to pay for all their children without signing in.
- `ManualPayment`: cash, teller or POS money recorded by staff. It becomes a
  `Payment` only when a second person approves it.
- `Payment` and `Allocation` are the ledger, and both are **append-only**
  (database triggers). A receipt is a positive payment; a reversal is a
  negative payment that points at the one it reverses, with negative
  allocations undoing the original ones. Allocations tie payments to invoices,
  oldest invoice first. A student's **credit** (money not yet allocated) is
  always computed: payments minus allocations.
- `WebhookEvent`: the inbox of gateway notifications, de-duplicated by key and
  processed in the background.
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, UUIDPrimaryKey, enum_type
from app.core.crypto import BLIND_INDEX_TYPE, EncryptedString
from app.core.tenancy import TenantMixin

from .gateways import GatewayCode

GATEWAY_ENUM = enum_type(GatewayCode, "payment_gateway")


class SchoolPaymentAccount(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    Where a school's money settles, on one gateway: a Paystack/Flutterwave subaccount on the
    school's bank account, or a Bachs connected account (bank details held by Bachs, so the
    bank fields are blank). A school can be set up on more than one gateway.
    """

    __tablename__ = "school_payment_accounts"
    __table_args__ = (UniqueConstraint("school_id", "gateway"),)

    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    subaccount_code: Mapped[str] = mapped_column(String(100))
    bank_code: Mapped[str] = mapped_column(String(20))
    bank_name: Mapped[str] = mapped_column(String(100))
    account_number: Mapped[str] = mapped_column(EncryptedString)
    account_number_last4: Mapped[str] = mapped_column(String(4))
    account_name: Mapped[str] = mapped_column(String(150))
    platform_fee_bp: Mapped[int] = mapped_column(SmallInteger)
    set_up_by: Mapped[uuid.UUID | None] = mapped_column()
    # When WardPay last asked the gateway what it paid out (payments/settlements.py)
    settlements_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VirtualAccountStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"  # e.g. the student left; transfers still credit them if they arrive


class VirtualAccount(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "virtual_accounts"
    __table_args__ = (
        # Webhooks find the student by account number before the school is known
        UniqueConstraint("gateway", "account_number"),
        Index(
            "uq_virtual_accounts_one_active_per_student",
            "student_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    account_number: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[str] = mapped_column(String(150))
    bank_name: Mapped[str] = mapped_column(String(100))
    provider_reference: Mapped[str] = mapped_column(String(100))
    status: Mapped[VirtualAccountStatus] = mapped_column(
        enum_type(VirtualAccountStatus, "virtual_account_status"), default=VirtualAccountStatus.ACTIVE
    )


class IntentStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"  # paid, but not what we expected (amount/currency)


class PaymentIntent(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """A card checkout we started. The outcome always comes from the gateway, never the browser."""

    __tablename__ = "payment_intents"
    __table_args__ = (
        UniqueConstraint("reference"),
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("charge_kobo >= 0", name="charge_not_negative"),
        # One child, or a family link whose children are the intent's items
        CheckConstraint("(student_id IS NULL) <> (family_link_id IS NULL)", name="one_child_or_family"),
    )

    reference: Mapped[str] = mapped_column(String(40))
    student_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"), index=True)
    family_link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("family_pay_links.id", ondelete="RESTRICT"))
    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)  # for the fees: what the bills are credited with
    # The provider's charge the parent pays on top (school's choice; 0 when the school bears it). The checkout is
    # for amount_kobo + charge_kobo.
    charge_kobo: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    email: Mapped[str] = mapped_column(EncryptedString)
    status: Mapped[IntentStatus] = mapped_column(
        enum_type(IntentStatus, "payment_intent_status"), default=IntentStatus.PENDING
    )
    authorization_url: Mapped[str] = mapped_column(String(500), default="")
    # The gateway's own id for the checkout, when it differs from our reference (Bachs: chk_...)
    provider_reference: Mapped[str] = mapped_column(String(100), default="", server_default="")
    review_reason: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column()  # staff user (payment link) or None (parent)
    # A platform admin's note on how a payment in review was sorted out (the money record isn't changed)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column()
    resolution_note: Mapped[str] = mapped_column(String(300), default="", server_default="")


class PaymentIntentItem(TenantMixin, Base):
    """One child's share of a family checkout, as the parent chose it. Each becomes its own payment."""

    __tablename__ = "payment_intent_items"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        UniqueConstraint("intent_id", "position"),
    )

    intent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payment_intents.id", ondelete="CASCADE"), primary_key=True)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"), primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger)  # 1, 2, ...: the payment's gateway reference suffix
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    # The fees this share is for, when the parent chose some (see PaymentEarmark); [] = tuition first
    earmarks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")


class FamilyPayLink(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    A link a parent keeps and opens any time to pay for all their children, with no
    sign-in (it goes in bills and reminders). Whoever has it can see the children's
    first names, classes and balances and pay, so it expires, staff can replace it,
    and erasing the parent ends it. The token is looked up by its keyed hash and kept
    encrypted so the same link can be sent again.
    """

    __tablename__ = "family_pay_links"
    __table_args__ = (
        UniqueConstraint("token_index"),
        Index(
            "uq_family_pay_links_one_live_per_guardian",
            "guardian_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    guardian_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("guardians.id", ondelete="CASCADE"))
    token_index: Mapped[str] = mapped_column(BLIND_INDEX_TYPE)
    token: Mapped[str] = mapped_column(EncryptedString)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column()  # staff user, or None (made for a message)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentMethod(enum.StrEnum):
    VIRTUAL_ACCOUNT = "virtual_account"  # transfer into the student's dedicated account
    CARD = "card"  # online checkout, paid by card
    CHECKOUT_TRANSFER = "checkout_transfer"  # online checkout, paid by transfer to a temporary account
    USSD = "ussd"  # online checkout, paid by USSD
    CASH = "cash"
    TELLER = "teller"  # bank deposit slip
    POS = "pos"  # card terminal at the school
    BANK_TRANSFER = "bank_transfer"  # to the school's main account, matched by staff


def method_for_channel(channel: str) -> "PaymentMethod":
    """How a parent paid on the gateway's checkout page (its own channel names)."""
    channel = channel.lower()
    if channel in {"bank_transfer", "banktransfer", "bank", "account", "pay_with_bank_transfer"}:
        return PaymentMethod.CHECKOUT_TRANSFER
    if channel == "ussd":
        return PaymentMethod.USSD
    return PaymentMethod.CARD


MANUAL_METHODS = (PaymentMethod.CASH, PaymentMethod.TELLER, PaymentMethod.POS, PaymentMethod.BANK_TRANSFER)


class PaymentKind(enum.StrEnum):
    RECEIPT = "receipt"
    REVERSAL = "reversal"


class Payment(TenantMixin, UUIDPrimaryKey, Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'receipt' AND amount_kobo > 0) OR (kind = 'reversal' AND amount_kobo < 0)",
            name="sign_matches_kind",
        ),
        CheckConstraint("(kind = 'reversal') = (reverses_payment_id IS NOT NULL)", name="reversal_points_back"),
        # One payment per gateway transaction, however many times the webhook arrives
        UniqueConstraint("gateway", "gateway_reference"),
        UniqueConstraint("reverses_payment_id"),
        Index("ix_payments_school_student", "school_id", "student_id"),
        Index("ix_payments_school_received", "school_id", "received_at"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    kind: Mapped[PaymentKind] = mapped_column(enum_type(PaymentKind, "payment_kind"))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[PaymentMethod] = mapped_column(enum_type(PaymentMethod, "payment_method"))
    gateway: Mapped[GatewayCode | None] = mapped_column(GATEWAY_ENUM)
    gateway_reference: Mapped[str | None] = mapped_column(String(100))
    reverses_payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Teller number, POS slip number, bank narration...
    reference: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    recorded_by: Mapped[uuid.UUID | None] = mapped_column()  # None: recorded automatically from the gateway
    approved_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Allocation(TenantMixin, UUIDPrimaryKey, Base):
    """Part of a payment applied to an invoice. Negative allocations belong to reversals."""

    __tablename__ = "allocations"
    __table_args__ = (
        CheckConstraint("amount_kobo <> 0", name="amount_nonzero"),
        Index("ix_allocations_invoice", "invoice_id"),
        Index("ix_allocations_payment", "payment_id"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManualPaymentStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ManualPayment(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    Money staff received outside the gateway (cash, teller, POS, a transfer to
    the school's own account). One person records it and a different person
    approves it; only then does it become a Payment.
    """

    __tablename__ = "manual_payments"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("status = 'pending' OR decided_by IS NOT NULL", name="decision_has_decider"),
        CheckConstraint("decided_by IS NULL OR decided_by <> recorded_by", name="maker_checker"),
        Index("ix_manual_payments_school_status", "school_id", "status"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[PaymentMethod] = mapped_column(enum_type(PaymentMethod, "payment_method"))
    received_on: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[ManualPaymentStatus] = mapped_column(
        enum_type(ManualPaymentStatus, "manual_payment_status"), default=ManualPaymentStatus.PENDING
    )
    recorded_by: Mapped[uuid.UUID] = mapped_column()
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str] = mapped_column(String(300), default="")
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"), unique=True)
    # The fees this money is for, when the person recording it chose some (see PaymentEarmark)
    earmarks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")


class WebhookStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"  # not for us, failed at the gateway, or a duplicate of a recorded payment
    NEEDS_REVIEW = "needs_review"  # money arrived but couldn't be matched safely
    FAILED = "failed"  # kept failing; an operator must look


class WebhookEvent(UUIDPrimaryKey, Base):
    """
    Gateway notifications, stored before anything else happens. Platform-level
    (the school is only known after processing), so not a tenant table.
    The payload is redacted; the fields needed to process it are kept in columns.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("gateway", "event_key"),
        Index("ix_webhook_events_pending", "received_at", postgresql_where=text("status = 'received'")),
    )

    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    event_key: Mapped[str] = mapped_column(String(150))
    event_type: Mapped[str] = mapped_column(String(60))
    reference: Mapped[str | None] = mapped_column(String(100))
    receiver_account_number: Mapped[str | None] = mapped_column(String(20))
    provider_payment_id: Mapped[str | None] = mapped_column(String(100))  # verify by this id (Bachs: ch_...)
    claimed_amount_kobo: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[WebhookStatus] = mapped_column(
        enum_type(WebhookStatus, "webhook_status"), default=WebhookStatus.RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(300), default="")
    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderSetting(UUIDPrimaryKey, Base):
    """
    Which provider a platform-wide service uses right now (e.g. payment: paystack or bachs),
    switched by a platform admin without a deploy. No row: the configured default.
    """

    __tablename__ = "provider_settings"
    __table_args__ = (UniqueConstraint("service"),)

    service: Mapped[str] = mapped_column(String(30))
    active_provider: Mapped[str] = mapped_column(String(30))
    updated_by: Mapped[uuid.UUID | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentEarmark(TenantMixin, UUIDPrimaryKey, Base):
    """
    Part of a payment the payer meant for one fee on one bill (e.g. WAEC), when they chose.
    The money goes to that bill first (even before older debts), and the fee-by-fee views
    show that fee paid. Append-only, like the payments it belongs to; a reversed payment's
    earmarks stop counting.
    """

    __tablename__ = "payment_earmarks"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        Index("ix_payment_earmarks_payment", "payment_id"),
        Index("ix_payment_earmarks_invoice", "invoice_id"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    fee_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"))
    description: Mapped[str] = mapped_column(String(200), default="")  # a one-off charge without a fee item
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CashDepositStatus(enum.StrEnum):
    PENDING = "pending"  # recorded; a second person still has to check it against the bank statement
    CONFIRMED = "confirmed"
    REJECTED = "rejected"  # didn't match the bank: the cash counts as still held


class CashDeposit(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    Cash taken from the school's drawer to the bank (the cash book's money out). Recorded by one
    person, confirmed or rejected by another against the bank statement. Never deleted (trigger).
    """

    __tablename__ = "cash_deposits"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("(status = 'pending') = (decided_by IS NULL)", name="decided_unless_pending"),
        CheckConstraint("decided_by IS NULL OR decided_by <> recorded_by", name="maker_checker"),
        Index("ix_cash_deposits_school_day", "school_id", "banked_on"),
    )

    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    banked_on: Mapped[date] = mapped_column(Date)
    bank_reference: Mapped[str] = mapped_column(String(100), default="")  # teller or deposit slip number
    note: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[CashDepositStatus] = mapped_column(
        enum_type(CashDepositStatus, "cash_deposit_status"), default=CashDepositStatus.PENDING
    )
    recorded_by: Mapped[uuid.UUID] = mapped_column()
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str] = mapped_column(String(300), default="")


class CashDayClose(TenantMixin, UUIDPrimaryKey, Base):
    """
    The end of a cash day: the cash the system expected in the drawer and what was counted. One per
    school per day, never changed (trigger). Cash recorded for a closed day afterwards is flagged.
    """

    __tablename__ = "cash_day_closes"
    __table_args__ = (
        UniqueConstraint("school_id", "day"),
        CheckConstraint("counted_kobo >= 0", name="counted_not_negative"),
    )

    day: Mapped[date] = mapped_column(Date)
    expected_kobo: Mapped[int] = mapped_column(BigInteger)
    counted_kobo: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str] = mapped_column(String(300), default="")
    closed_by: Mapped[uuid.UUID] = mapped_column()
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GatewaySettlement(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    A payout from the gateway into the school's bank account, as the gateway reports it. Refreshed by
    payments/settlements.py: the gateway's record is the source, so these rows follow it.
    """

    __tablename__ = "gateway_settlements"
    __table_args__ = (
        UniqueConstraint("school_id", "gateway", "settlement_id"),
        CheckConstraint("gross_kobo >= 0 AND paid_out_kobo >= 0", name="amounts_not_negative"),
        Index("ix_gateway_settlements_school_day", "school_id", "settled_on"),
    )

    gateway: Mapped[GatewayCode] = mapped_column(GATEWAY_ENUM)
    settlement_id: Mapped[str] = mapped_column(String(64))  # the gateway's id
    status: Mapped[str] = mapped_column(String(20))
    settled_on: Mapped[date | None] = mapped_column(Date)
    gross_kobo: Mapped[int] = mapped_column(BigInteger)  # what parents paid
    charges_kobo: Mapped[int] = mapped_column(BigInteger)  # taken off before the bank
    paid_out_kobo: Mapped[int] = mapped_column(BigInteger)  # what reached the bank


class SettlementItem(TenantMixin, UUIDPrimaryKey, Base):
    """A transaction a payout covered, matched to WardPay's payment by the gateway reference when known."""

    __tablename__ = "settlement_items"
    __table_args__ = (
        UniqueConstraint("settlement_id", "reference"),
        Index("ix_settlement_items_payment", "payment_id"),
    )

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gateway_settlements.id", ondelete="CASCADE"))
    reference: Mapped[str] = mapped_column(String(100))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    fees_kobo: Mapped[int] = mapped_column(BigInteger)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"))
