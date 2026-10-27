"""The demo seed builds a consistent school in a fixed number of statements."""

from sqlalchemy import func, select, text

from app import demo, demo_extras
from app.core.crypto import blind_index
from app.core.db import SessionLocal
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.accounting.models import BookClosing
from app.modules.accounts.models import User
from app.modules.expenses.models import Expense
from app.modules.fees.invoicing import account_summaries
from app.modules.fees.models import Invoice, PaymentPlan, PlanTemplate, StudentFeeExemption, WriteOff
from app.modules.notifications.models import MessageLog, MessageStatus
from app.modules.payments.models import (
    CashDayClose,
    CashDeposit,
    GatewaySettlement,
    ManualPayment,
    ManualPaymentStatus,
    Payment,
    PaymentEarmark,
    PaymentKind,
)
from app.modules.platform.models import SubscriptionInvoice, SubscriptionPayment
from app.modules.receipts.models import Receipt
from app.modules.schools.models import School
from app.modules.students.models import Guardian, GuardianStudent, Student
from app.modules.students.summary import roll_summary
from tests.factories import QueryCounter


def _demo_school(db) -> School:
    scope_platform_wide(db)
    return db.scalar(select(School).where(School.slug == demo.DEMO_SLUG))


def test_lived_in_demo_reconciles(db, connection, monkeypatch):
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # the ledger's deferred checks run on every statement
    extras: list[int] = []
    real_extras = demo_extras.add_extras

    def counted_extras(*args, **kwargs):
        before = counter.count
        real_extras(*args, **kwargs)
        extras.append(counter.count - before)

    monkeypatch.setattr(demo_extras, "add_extras", counted_extras)
    with QueryCounter(connection) as counter:
        demo.seed_demo("JBSWY3DPEHPK3PXP", reset=False)
    [later_features] = extras
    assert counter.count - later_features < 120  # bulk statements, never one per student
    # The later features go through the real services, a fixed amount of work whatever the size of the school
    assert later_features < 900  # about 740: each step is a few service calls, none per pupil

    school = _demo_school(db)
    sid = school.id
    student_ids = list(db.scalars(select(Student.id).where(Student.school_id == sid)))
    assert len(student_ids) == 246  # 240 billed, plus the loose ends (not billed yet)

    receipts = list(db.scalars(select(Receipt.number).where(Receipt.school_id == sid).order_by(Receipt.number)))
    payments = db.scalar(select(func.count()).where(Payment.school_id == sid, Payment.kind == PaymentKind.RECEIPT))
    assert receipts == list(range(1, payments + 1))  # one per payment, no gaps
    assert db.scalar(select(func.count()).where(Payment.school_id == sid, Payment.kind == PaymentKind.REVERSAL)) == 1
    assert (
        db.scalar(
            select(func.count()).where(
                ManualPayment.school_id == sid, ManualPayment.status == ManualPaymentStatus.PENDING
            )
        )
        == 2
    )

    # Money in = money applied + credit, and every student's balance comes from the ledger
    summaries = account_summaries(db, student_ids)
    received = db.scalar(select(func.sum(Payment.amount_kobo)).where(Payment.school_id == sid))
    assert sum(s.paid_kobo for s in summaries.values()) <= received
    assert all(s.outstanding_kobo == s.billed_kobo - s.paid_kobo for s in summaries.values())
    assert db.scalar(select(func.count()).where(Invoice.school_id == sid)) > 240  # term bills + opening balances

    # The first family: one child paid up, the other still owing (with last session's debt)
    family = list(
        db.scalars(
            select(GuardianStudent.student_id)
            .join(Guardian, Guardian.id == GuardianStudent.guardian_id)
            .where(Guardian.school_id == sid, Guardian.phone_index == blind_index(demo.FIRST_PARENT_PHONE))
        )
    )
    assert sorted(summaries[s].outstanding_kobo == 0 for s in family) == [False, True]

    statuses = dict(
        db.execute(
            select(MessageLog.status, func.count()).where(MessageLog.school_id == sid).group_by(MessageLog.status)
        ).all()
    )
    assert statuses[MessageStatus.SENT] > 240 and statuses[MessageStatus.SKIPPED] > 0

    # The school overview has realistic loose ends to show
    with SessionLocal() as scoped:
        scope_to_school(scoped, sid)
        roll = roll_summary(scoped)
    assert (roll.on_roll, roll.unplaced, roll.left_this_term) == (245, 2, 1)
    assert (roll.no_parent, roll.no_consent, roll.no_account_number) == (1, 2, 5)
    assert 0 < roll.joined_this_term < 100  # the intakes and a few transfers, not everyone

    others = set(db.scalars(select(School.slug).where(School.slug.like("demo-%"))))
    assert {"demo-kings-queens", "demo-bright-futures", "demo-sunrise"} <= others
    _every_later_feature_has_data(db, sid)


def _every_later_feature_has_data(db, sid) -> None:
    scope_platform_wide(db)

    def count(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    expenses = dict(
        db.execute(select(Expense.status, func.count()).where(Expense.school_id == sid).group_by(Expense.status)).all()
    )
    assert set(expenses) == {"approved", "pending", "rejected", "voided"}
    assert count(CashDeposit, CashDeposit.school_id == sid) >= 2
    closes = db.execute(
        select(CashDayClose.counted_kobo, CashDayClose.expected_kobo).where(CashDayClose.school_id == sid)
    )
    assert any(counted < expected for counted, expected in closes)  # one short count
    assert count(PlanTemplate, PlanTemplate.school_id == sid) == 3
    assert count(PaymentPlan, PaymentPlan.school_id == sid) >= 5
    assert {s for (s,) in db.execute(select(WriteOff.status).where(WriteOff.school_id == sid))} == {
        "approved",
        "pending",
    }
    assert count(StudentFeeExemption, StudentFeeExemption.school_id == sid) == 1
    assert count(PaymentEarmark, PaymentEarmark.school_id == sid) >= 1
    assert count(BookClosing, BookClosing.school_id == sid) == 1
    assert count(GatewaySettlement, GatewaySettlement.school_id == sid) > 0
    # The Lekki branch, the group and its head office, and a parent with children at both
    branch = db.scalar(select(School).where(School.slug == demo_extras.BRANCH_SLUG))
    assert branch.group_id is not None and db.get(School, sid).group_id == branch.group_id
    assert count(User, User.group_id == branch.group_id) == 2
    assert count(Payment, Payment.school_id == branch.id) > 10
    assert count(Guardian, Guardian.phone_index == blind_index(demo.FIRST_PARENT_PHONE)) == 2
    # WardPay billed both schools, and the branch has paid
    assert count(SubscriptionInvoice, SubscriptionInvoice.school_id.in_([sid, branch.id])) == 2
    assert count(SubscriptionPayment) == 1


def test_bare_demo_for_browser_tests(db):
    demo.seed_demo("JBSWY3DPEHPK3PXP", reset=False, bare=True)
    school = _demo_school(db)
    assert db.scalar(select(func.count()).where(Student.school_id == school.id)) == 60
    assert db.scalar(select(func.count()).where(Invoice.school_id == school.id)) == 0


def test_demo_payouts_use_paystack_fees():
    from app.demo_extras import paystack_fee
    from app.modules.payments.models import PaymentMethod

    transfer, card = PaymentMethod.VIRTUAL_ACCOUNT, PaymentMethod.CARD
    assert paystack_fee(10_000 * 100, transfer) == 100 * 100  # 1%
    assert paystack_fee(150_000 * 100, transfer) == 300 * 100  # capped at ₦300
    assert paystack_fee(2_000 * 100, card) == 30 * 100  # 1.5%, no ₦100 under ₦2,500
    assert paystack_fee(10_000 * 100, card) == 250 * 100  # 1.5% + ₦100
    assert paystack_fee(500_000 * 100, card) == 2_000 * 100  # capped at ₦2,000
