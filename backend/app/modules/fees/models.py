"""
Fees and invoices.

Money is integer kobo everywhere (`BigInteger`); no floats.

The ledger: an `Invoice` is a set of `InvoiceLine`s, and its total is the sum
of its lines. Lines are append-only (a database trigger refuses UPDATE and
DELETE): a correction is a new `adjustment` line, never an edit. Discounts are
negative lines, so every naira on an invoice is traceable. A deferred
constraint trigger refuses any invoice whose lines sum below zero.

Balances are always computed from the ledger (lines minus payments); nothing
stores a balance that could drift.
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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, Timestamped, UUIDPrimaryKey, enum_type
from app.core.tenancy import TenantMixin


class FeeCategory(enum.StrEnum):
    TUITION = "tuition"
    LEVY = "levy"  # PTA, development, sports...
    SERVICE = "service"  # bus, lunch, boarding
    MATERIALS = "materials"  # books, uniforms
    OTHER = "other"


class FeeItem(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """Something the school charges for, e.g. "Tuition", "School bus", "PTA levy"."""

    __tablename__ = "fee_items"
    __table_args__ = (
        UniqueConstraint("school_id", "name"),
        CheckConstraint("default_amount_kobo IS NULL OR default_amount_kobo > 0", name="price_positive"),
    )

    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[FeeCategory] = mapped_column(enum_type(FeeCategory, "fee_category"), default=FeeCategory.OTHER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # A standard price for items charged to individual pupils (uniform, textbooks); class fees are set per term
    default_amount_kobo: Mapped[int | None] = mapped_column(BigInteger)


class FeeSchedule(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """What one fee item costs for one class level in one term."""

    __tablename__ = "fee_schedules"
    __table_args__ = (
        UniqueConstraint("term_id", "class_level_id", "fee_item_id"),
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        Index("ix_fee_schedules_term", "term_id"),
    )

    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id", ondelete="RESTRICT"))
    class_level_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("class_levels.id", ondelete="RESTRICT"))
    fee_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    # Optional items (bus, lunch) are only billed to students opted in for the term
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False)


class StudentFeeExemption(TenantMixin, Base):
    """
    A pupil doesn't pay a class fee this term (e.g. not sitting WAEC, not going on the
    end-of-year trip). Term bills generated afterwards leave the item out; a bill already
    issued gets a correcting line when the exemption is added.
    """

    __tablename__ = "student_fee_exemptions"

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), primary_key=True)
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id", ondelete="RESTRICT"), primary_key=True)
    fee_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudentFeeChoice(TenantMixin, Base):
    """A student opted in to an optional fee item for a term."""

    __tablename__ = "student_fee_choices"

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), primary_key=True)
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id", ondelete="RESTRICT"), primary_key=True)
    fee_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"), primary_key=True)


class DiscountKind(enum.StrEnum):
    PERCENT = "percent"  # value in basis points: 1000 = 10%
    FIXED = "fixed"  # value in kobo


class DiscountRule(enum.StrEnum):
    MANUAL = "manual"  # given to chosen students (scholarship, staff child)
    SIBLING = "sibling"  # automatic: every child after the eldest sharing a guardian


class Discount(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "discounts"
    __table_args__ = (
        UniqueConstraint("school_id", "name"),
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint("kind <> 'percent' OR value <= 10000", name="percent_at_most_100"),
    )

    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[DiscountKind] = mapped_column(enum_type(DiscountKind, "discount_kind"))
    value: Mapped[int] = mapped_column(BigInteger)
    rule: Mapped[DiscountRule] = mapped_column(enum_type(DiscountRule, "discount_rule"), default=DiscountRule.MANUAL)
    # Only this item is discounted (e.g. tuition); None means every fee on the invoice
    fee_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StudentDiscount(TenantMixin, Base):
    """A manual discount given to a student, applied whenever their term invoices are generated."""

    __tablename__ = "student_discounts"

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), primary_key=True)
    discount_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discounts.id", ondelete="RESTRICT"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvoiceKind(enum.StrEnum):
    TERM = "term"  # the term's fees: at most one live one per student per term
    OPENING_BALANCE = "opening_balance"  # what a student owed before the school joined WardPay
    ADHOC = "adhoc"  # one-off charges (replacement ID card, excursion)


class Invoice(TenantMixin, UUIDPrimaryKey, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("school_id", "number"),
        Index(
            "uq_invoices_one_live_term_invoice",
            "student_id",
            "term_id",
            unique=True,
            postgresql_where=text("kind = 'term' AND voided_at IS NULL"),
        ),
        CheckConstraint("(kind = 'term') = (term_id IS NOT NULL)", name="term_matches_kind"),
        CheckConstraint("(voided_at IS NULL) = (void_reason IS NULL)", name="void_has_reason"),
        Index("ix_invoices_school_student", "school_id", "student_id"),
        Index("ix_invoices_term", "term_id"),
    )

    number: Mapped[int] = mapped_column(BigInteger)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    kind: Mapped[InvoiceKind] = mapped_column(enum_type(InvoiceKind, "invoice_kind"))
    term_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("terms.id", ondelete="RESTRICT"))
    # Class at billing time, kept for reports even if the student moves
    class_level_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("class_levels.id", ondelete="RESTRICT"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(300))

    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", order_by="InvoiceLine.position", lazy="raise"
    )


class LineKind(enum.StrEnum):
    FEE = "fee"  # positive
    DISCOUNT = "discount"  # negative
    ADJUSTMENT = "adjustment"  # a later correction, either sign
    WRITE_OFF = "write_off"  # negative: a debt the school won't collect (fees/writeoffs.py, approved)


class InvoiceLine(TenantMixin, UUIDPrimaryKey, Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("kind <> 'fee' OR amount_kobo > 0", name="fee_positive"),
        CheckConstraint("kind <> 'discount' OR amount_kobo < 0", name="discount_negative"),
        CheckConstraint("kind <> 'adjustment' OR amount_kobo <> 0", name="adjustment_nonzero"),
        # Compared as text: the enum value is added in the same migration
        CheckConstraint("kind::text <> 'write_off' OR amount_kobo < 0", name="write_off_negative"),
        UniqueConstraint("invoice_id", "position"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[LineKind] = mapped_column(enum_type(LineKind, "invoice_line_kind"))
    description: Mapped[str] = mapped_column(String(200))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    fee_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fee_items.id", ondelete="RESTRICT"))
    discount_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("discounts.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(String(300))

    invoice: Mapped[Invoice] = relationship(back_populates="lines", lazy="raise")


class PaymentPlan(TenantMixin, UUIDPrimaryKey, Base):
    """
    An agreement to pay what's left on a bill in instalments. It changes when the
    bill counts as late (the next unpaid instalment, not the bill's due date) and
    what reminders ask for; payments are applied exactly as without a plan.
    A plan is never edited: it's cancelled and a new one made.
    """

    __tablename__ = "payment_plans"
    __table_args__ = (
        # One live plan per bill
        Index("uq_payment_plans_one_live", "invoice_id", unique=True, postgresql_where=text("cancelled_at IS NULL")),
        CheckConstraint("covered_kobo > 0 AND paid_at_start_kobo >= 0", name="amounts_valid"),
        CheckConstraint("(cancelled_at IS NULL) = (cancel_reason IS NULL)", name="cancel_has_reason"),
        Index("ix_payment_plans_student", "student_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    # What had been paid on the bill when the plan started; the plan covers the rest
    paid_at_start_kobo: Mapped[int] = mapped_column(BigInteger)
    covered_kobo: Mapped[int] = mapped_column(BigInteger)  # the sum of the instalments
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column()  # None: the parent chose it themselves
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    # One of the school's own plans (fees/plan_offers.py), when it came from one
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plan_templates.id", ondelete="RESTRICT"))
    # The WardPay bill that charged the school its per-plan fee (platform/billing.py)
    subscription_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscription_invoices.id", ondelete="SET NULL")
    )


class PlanInstalment(TenantMixin, UUIDPrimaryKey, Base):
    """One dated amount in a payment plan. Append-only (database trigger)."""

    __tablename__ = "plan_instalments"
    __table_args__ = (
        UniqueConstraint("plan_id", "number"),
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("number BETWEEN 1 AND 12", name="number_range"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payment_plans.id", ondelete="RESTRICT"))
    number: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)


class WriteOffStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for the proprietor
    APPROVED = "approved"  # the debt is cleared: a write_off line is on the bill
    REJECTED = "rejected"


class WriteOff(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    A request to stop chasing (part of) a bill the school won't collect. Only the proprietor approves;
    approval adds a write_off line, so the bill's balance and the ledger follow. Never deleted (trigger).
    """

    __tablename__ = "write_offs"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="amount_positive"),
        CheckConstraint("length(reason) >= 3", name="has_reason"),
        CheckConstraint("(status = 'pending') = (decided_by IS NULL)", name="decided_unless_pending"),
        CheckConstraint("(status = 'approved') = (line_id IS NOT NULL)", name="approved_has_line"),
        # One open request per bill
        Index("uq_write_offs_one_pending", "invoice_id", unique=True, postgresql_where=text("status = 'pending'")),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"), index=True)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[WriteOffStatus] = mapped_column(
        enum_type(WriteOffStatus, "write_off_status"), default=WriteOffStatus.PENDING
    )
    requested_by: Mapped[uuid.UUID] = mapped_column()
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str] = mapped_column(String(300), default="")
    line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoice_lines.id", ondelete="RESTRICT"))


class PlanEligibility(enum.StrEnum):
    ANYONE = "anyone"  # any parent can choose it
    NO_OLD_DEBTS = "no_old_debts"  # only when nothing is owed on the pupil's other bills
    STAFF_ONLY = "staff_only"  # parents don't see it; staff can put a family on it


class PlanChargeKind(enum.StrEnum):
    NONE = "none"
    FIXED = "fixed"  # charge_value is kobo
    PERCENT = "percent"  # charge_value is basis points of what's owed (250 = 2.5%)


class PlanTemplate(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    One of the school's own instalment plans ("Pay in 3: 50% / 25% / 25%"), which parents can choose
    themselves. Each part is a share of what's owed and a day counted from the start of the term. Editing
    one never changes plans already chosen: those are copied into PaymentPlan when chosen.
    """

    __tablename__ = "plan_templates"
    __table_args__ = (
        UniqueConstraint("school_id", "name"),
        CheckConstraint(
            "(charge_kind = 'none' AND charge_value = 0) OR (charge_kind <> 'none' AND charge_value > 0)",
            name="charge_valid",
        ),
        CheckConstraint("charge_kind <> 'percent' OR charge_value <= 2000", name="percent_at_most_20"),
    )

    name: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    eligibility: Mapped[PlanEligibility] = mapped_column(
        enum_type(PlanEligibility, "plan_eligibility"), default=PlanEligibility.NO_OLD_DEBTS
    )
    charge_kind: Mapped[PlanChargeKind] = mapped_column(
        enum_type(PlanChargeKind, "plan_charge_kind"), default=PlanChargeKind.NONE
    )
    charge_value: Mapped[int] = mapped_column(BigInteger, default=0)
    # [{"percent_bp": 5000, "day": 0}, ...]: shares add up to 10000, days rise, 2 to 12 parts
    parts: Mapped[list[dict[str, int]]] = mapped_column(JSONB)
