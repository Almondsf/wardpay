"""
The rest of the demo: every feature added since the first demo seed, with believable data, so each page has
something to show (owner, 2026-10-27: "seed more meaningful data that cover every feature").

Runs after `demo_activity` and goes through the real services wherever it can (they check the same rules as
the screens), so the books still add up:

- **Spending**: categories and a term of expenses (approved by the proprietor, one waiting, one turned down,
  one cancelled), some paid from the cash drawer.
- **Cash book**: cash banked (confirmed, and one waiting) and the drawer counted at the end of recent days,
  one of them short.
- **Extra fees**: a sports levy for the primary classes, WAEC registration for SSS 3, a pupil exempted, one-off
  items charged to two pupils.
- **Instalment plans**: the school's plans ("Pay in 3" for anyone, a staff-only plan with a charge), families
  put on them by the bursar, and one plan agreed by hand.
- **Write-offs**: an old debt written off (asked by the bursar, approved by the proprietor) and one waiting.
- **Payments marked for a fee** after they arrived ("that transfer was for the PTA levy").
- **Closed books**: last session closed.
- **Paid into the bank** (fake gateway only): recent payouts matched to payments, one with money WardPay
  doesn't know.
- **A second branch** ("Demo Model College, Lekki") with its own pupils, bills and payments; both schools form
  the group "Demo Model Schools" with a group owner and a central bursar. The first demo parent has a child
  at Lekki too, so the portal shows "At other branches".
- **WardPay's bills** to the schools for the term (with the instalment-plan fee), one paid.
"""

import random
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.clock import LAGOS, lagos_today
from app.core.crypto import blind_index
from app.core.db import SessionLocal
from app.core.errors import Conflict, DomainError
from app.core.http import RequestMeta
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.accounting import periods
from app.modules.accounts.deps import Principal, StaffContext
from app.modules.accounts.models import Role, User
from app.modules.accounts.security import hash_password
from app.modules.expenses import service as expenses
from app.modules.expenses.models import PaidFrom
from app.modules.fees import charges, plan_offers, plans, writeoffs
from app.modules.fees.invoicing import amounts_for, generate_term_invoices
from app.modules.fees.models import FeeItem, Invoice, InvoiceKind, PaymentPlan, PlanChargeKind, PlanEligibility
from app.modules.payments import cashbook, earmarks, settlements
from app.modules.payments.gateways import FakeGateway, GatewayCode, enabled_gateways
from app.modules.payments.gateways.base import SettledTransaction
from app.modules.payments.models import Payment, PaymentKind, PaymentMethod, SchoolPaymentAccount, VirtualAccount
from app.modules.payments.service import NewPayment, record_payments
from app.modules.platform import billing
from app.modules.platform.models import SubscriptionInvoice
from app.modules.schools.models import AcademicSession, ClassLevel, School, SchoolGroup, SchoolStatus, Term
from app.modules.students.models import (
    ConsentMethod,
    Enrollment,
    Gender,
    Guardian,
    GuardianStudent,
    Relationship,
    Student,
)

BRANCH_SLUG = "demo-school-lekki"
GROUP_NAME = "Demo Model Schools"
HEAD_OFFICE = [
    ("owner@demo.wardpay.ng", "Chief Adebayo Adeyemi", Role.PROPRIETOR),
    ("centralbursar@demo.wardpay.ng", "Mrs Funke Alabi", Role.BURSAR),
]
BRANCH_STAFF = [
    ("proprietor@lekki.demo.wardpay.ng", "Mrs Ngozi Eze", Role.PROPRIETOR),
    ("bursar@lekki.demo.wardpay.ng", "Mr Tunde Bakare", Role.BURSAR),
]
META = RequestMeta(ip="127.0.0.1")
SURNAMES = ["Okonkwo", "Balogun", "Ibrahim", "Etim", "Ogundipe", "Nwankwo", "Lawal", "Akande", "Uche", "Danjuma"]
FIRST_NAMES = ["Ada", "Tobi", "Musa", "Chioma", "Seyi", "Ifeanyi", "Aisha", "Kunle", "Ebere", "Femi", "Halima", "Dayo"]


def add_extras(school_id: uuid.UUID, term_id: uuid.UUID, mfa_secret: str, first_parent_phone: str) -> None:
    rng = random.Random(1027)  # noqa: S311  (demo data, not security)
    fake = GatewayCode.FAKE in enabled_gateways()
    with SessionLocal(expire_on_commit=False) as session:
        scope_platform_wide(session)
        staff = {
            u.role: u
            for u in session.scalars(select(User).where(User.school_id == school_id, User.is_active.is_(True)))
        }
    proprietor = _ctx(staff[Role.PROPRIETOR], school_id)
    bursar = _ctx(staff[Role.BURSAR], school_id)
    admin = _ctx(staff[Role.SCHOOL_ADMIN], school_id)

    with _school_session(school_id) as session:
        _spending(session, bursar, admin, proprietor, rng)
    with _school_session(school_id) as session:
        _cash(session, bursar, proprietor)
    with _school_session(school_id) as session:
        _extra_fees(session, bursar, term_id)
    with _school_session(school_id) as session:
        _instalment_plans(session, bursar, proprietor, term_id)
    with _school_session(school_id) as session:
        _write_offs(session, bursar, proprietor)
    with _school_session(school_id) as session:
        _marked_payments(session, bursar)
    with _school_session(school_id) as session:
        _close_last_session(session, proprietor)
    if fake:
        with _school_session(school_id) as session:
            _payouts(session, school_id, "FAKE_SUB_DEMO")

    branch_id = _branch(mfa_secret, first_parent_phone, fake)
    with SessionLocal(expire_on_commit=False) as session:
        scope_platform_wide(session)
        _group(session, [school_id, branch_id], mfa_secret)
        platform_admin = session.scalar(select(User).where(User.role == Role.PLATFORM_ADMIN).limit(1))
    if platform_admin is not None:
        with SessionLocal(expire_on_commit=False) as session:
            scope_platform_wide(session)
            _wardpay_bills(session, platform_admin, branch_id)


def retire(session: Session) -> None:
    """Called by `seed --reset` (platform-wide session): the old branch's staff and head office go, the branch is
    suspended and renamed, and the group is emptied so the new demo schools can join it."""
    old = select(School.id).where(School.slug == BRANCH_SLUG).scalar_subquery()
    group = select(SchoolGroup.id).where(SchoolGroup.name == GROUP_NAME).scalar_subquery()
    session.execute(delete(User).where((User.school_id == old) | (User.group_id == group)))
    session.execute(update(School).where(School.group_id == group).values(group_id=None))
    session.execute(
        update(School)
        .where(School.slug == BRANCH_SLUG)
        .values(slug=f"old-demo-lekki-{uuid.uuid4().hex[:8]}", status=SchoolStatus.SUSPENDED)
    )
    session.flush()


# --- Helpers --------------------------------------------------------------------------------------------


def _ctx(user: User, school_id: uuid.UUID) -> StaffContext:
    return StaffContext(user=user, session_id=uuid.uuid4(), meta=META, school_id=school_id, acting_school_id=school_id)


class _school_session:  # noqa: N801  (used like a function)
    """A fresh session scoped to one school: each step commits on its own, like a request."""

    def __init__(self, school_id: uuid.UUID) -> None:
        self.school_id = school_id

    def __enter__(self) -> Session:
        self.session = SessionLocal(expire_on_commit=False)
        scope_to_school(self.session, self.school_id)
        return self.session

    def __exit__(self, *exc: object) -> None:
        self.session.close()


def _working_days(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=n) for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5
    ]


# --- Spending --------------------------------------------------------------------------------------------


def _spending(
    session: Session, bursar: StaffContext, admin: StaffContext, proprietor: StaffContext, rng: random.Random
) -> None:
    names = [
        "Diesel and generator",
        "Repairs and maintenance",
        "Stationery and printing",
        "Cleaning and sanitation",
        "Sports and events",
        "School bus running costs",
    ]
    category = {name: expenses.save_category(session, bursar, None, name=name).id for name in names}
    yesterday = lagos_today() - timedelta(days=1)
    # (category, naira, payee, what, paid from, days after 7 Sep)
    spent = [
        ("Diesel and generator", 185_000, "Total Energies, Allen Avenue", "Diesel, 200 litres", PaidFrom.BANK, 3),
        ("Stationery and printing", 42_500, "Olumide Prints", "Exam papers and report cards", PaidFrom.CASH, 6),
        ("Cleaning and sanitation", 18_000, "Mama Bisi Cleaners", "Weekly cleaning, September", PaidFrom.CASH, 10),
        ("Repairs and maintenance", 65_000, "Chidi Plumbing Works", "Toilet block pipes", PaidFrom.CASH, 14),
        ("School bus running costs", 96_000, "Mobil, Opebi", "Bus fuel, two weeks", PaidFrom.CARD, 17),
        ("Diesel and generator", 192_000, "Total Energies, Allen Avenue", "Diesel, 200 litres", PaidFrom.BANK, 24),
        ("Sports and events", 75_000, "Ikeja Sports Ltd", "Footballs, nets and cones", PaidFrom.BANK, 28),
        ("Cleaning and sanitation", 18_000, "Mama Bisi Cleaners", "Weekly cleaning, October", PaidFrom.CASH, 31),
        ("Repairs and maintenance", 34_000, "Sunday Electricals", "Classroom fans, JSS block", PaidFrom.CASH, 36),
        ("School bus running costs", 104_000, "Mobil, Opebi", "Bus fuel, two weeks", PaidFrom.CARD, 38),
        ("Stationery and printing", 27_800, "Olumide Prints", "Mid-term test papers", PaidFrom.CASH, 40),
        ("Diesel and generator", 201_000, "Total Energies, Allen Avenue", "Diesel, 200 litres", PaidFrom.BANK, 45),
    ]
    start = date(2026, 9, 7)
    recorded = []
    for cat, naira, payee, what, paid_from, days in spent:
        on = min(start + timedelta(days=days), yesterday)
        who = admin if rng.random() < 0.4 else bursar
        recorded.append(
            expenses.record_expense(
                session,
                who,
                category_id=category[cat],
                amount_kobo=naira * 100,
                spent_on=on,
                payee=payee,
                description=what,
                paid_from=paid_from,
                reference=f"RCPT-{rng.randint(1000, 9999)}",
            )
        )
    for expense in recorded[:-1]:
        expenses.decide(session, proprietor, expense.id, approve=True)
    # The latest one waits for the proprietor
    turned_down = expenses.record_expense(
        session,
        bursar,
        category_id=category["Sports and events"],
        amount_kobo=250_000 * 100,
        spent_on=yesterday,
        payee="Eko Events",
        description="Inter-house sports canopies and chairs",
        paid_from=PaidFrom.BANK,
        reference="INV-0912",
    )
    expenses.decide(session, proprietor, turned_down.id, approve=False, reason="Too dear: get two more quotes.")
    expenses.void(session, proprietor, recorded[4].id, "Entered twice: the same fuel receipt as the next entry.")


# --- Cash ---------------------------------------------------------------------------------------------------


def _cash(session: Session, bursar: StaffContext, proprietor: StaffContext) -> None:
    """Cash banked every other Friday and the drawer counted at the end of the last three working days."""
    today = lagos_today()
    fridays = [d for d in _working_days(date(2026, 9, 11), today - timedelta(days=4)) if d.weekday() == 4][::2]
    deposits = []
    for friday in fridays:
        held = cashbook.held(session, friday)
        if held < 50_000 * 100:
            continue
        amount = held // 100_000 * 100_000  # round thousands, the change stays in the drawer
        deposits.append(
            cashbook.record_deposit(
                session,
                bursar,
                amount_kobo=amount,
                banked_on=friday,
                bank_reference=f"GTB-DEP-{friday:%d%m}",
                note="Cash banked at GTBank, Allen Avenue",
            )
        )
    for deposit in deposits[:-1]:
        cashbook.decide_deposit(session, proprietor, deposit.id, confirm=True)
    days = _working_days(today - timedelta(days=6), today - timedelta(days=1))[-3:]
    expected = {day: cashbook.held(session, day) for day in days}
    busiest = max(days, key=lambda d: expected[d]) if days else None  # the short count falls on the busiest day
    for day in days:
        short = 2_000 * 100 if day == busiest and expected[day] >= 2_000 * 100 else 0
        note = "₦2,000 short: change given to a parent, not written down" if short else "Counted with the bursar"
        cashbook.close_day(session, bursar, day=day, counted_kobo=expected[day] - short, note=note)


# --- Extra fees for classes and pupils -----------------------------------------------------------------------


def _extra_fees(session: Session, bursar: StaffContext, term_id: uuid.UUID) -> None:
    levels = dict(session.execute(select(ClassLevel.name, ClassLevel.id)).all())
    primary = {lid for name, lid in levels.items() if name.startswith("Primary")}
    charges.charge_classes(
        session,
        bursar,
        term_id=term_id,
        fee_item_id=None,
        new_item_name="Inter-house sports",
        amount_kobo=3_000 * 100,
        class_level_ids=primary,
        excluded_student_ids=set(),
        dry_run=False,
    )
    if "SSS 3" in levels:
        charges.charge_classes(
            session,
            bursar,
            term_id=term_id,
            fee_item_id=None,
            new_item_name="WAEC registration",
            amount_kobo=27_500 * 100,
            class_level_ids={levels["SSS 3"]},
            excluded_student_ids=set(),
            dry_run=False,
        )
    # A pupil exempted from the sports levy (on a sports scholarship), and two one-off items
    sports = session.scalar(select(FeeItem.id).where(FeeItem.name == "Inter-house sports"))
    unpaid = _unpaid_pupils(session, term_id, primary)
    if sports and unpaid:
        charges.exempt(session, bursar, unpaid[0], term_id, sports, "Sports scholarship: the school pays")
    pupils = _unpaid_pupils(session, term_id, None)
    if len(pupils) >= 3:
        charges.charge_pupil(
            session,
            bursar,
            pupils[1],
            fee_item_id=None,
            description="Replacement ID card",
            unit_kobo=2_500 * 100,
            quantity=1,
            reason="Lost ID card",
        )
        charges.charge_pupil(
            session,
            bursar,
            pupils[2],
            fee_item_id=None,
            description="Mathematics textbook (New General Mathematics)",
            unit_kobo=4_800 * 100,
            quantity=2,
            reason="Second copy for home",
        )


def _unpaid_pupils(session: Session, term_id: uuid.UUID, level_ids: set[uuid.UUID] | None) -> list[uuid.UUID]:
    """Pupils (after the first demo family) whose term bill has nothing paid on it yet."""
    stmt = select(Invoice.id, Invoice.student_id).where(Invoice.term_id == term_id, Invoice.kind == InvoiceKind.TERM)
    if level_ids is not None:
        stmt = stmt.where(Invoice.class_level_id.in_(level_ids))
    rows = session.execute(stmt.order_by(Invoice.number)).all()
    amounts = amounts_for(session, [r[0] for r in rows])
    return [sid for iid, sid in rows[4:] if amounts[iid].paid_kobo == 0]


# --- Instalment plans ------------------------------------------------------------------------------------------


def _instalment_plans(session: Session, bursar: StaffContext, proprietor: StaffContext, term_id: uuid.UUID) -> None:
    pay_in_3 = plan_offers.save_template(
        session,
        proprietor,
        None,
        name="Pay in 3",
        is_active=True,
        eligibility=PlanEligibility.ANYONE,
        charge_kind=PlanChargeKind.NONE,
        charge_value=0,
        parts=[(4000, 0), (3000, 42), (3000, 77)],
    )
    plan_offers.save_template(
        session,
        proprietor,
        None,
        name="Pay monthly (no old debts)",
        is_active=True,
        eligibility=PlanEligibility.NO_OLD_DEBTS,
        charge_kind=PlanChargeKind.FIXED,
        charge_value=2_000 * 100,
        parts=[(3000, 0), (2500, 28), (2500, 56), (2000, 84)],
    )
    plan_offers.save_template(
        session,
        proprietor,
        None,
        name="Hardship plan (staff only)",
        is_active=True,
        eligibility=PlanEligibility.STAFF_ONLY,
        charge_kind=PlanChargeKind.PERCENT,
        charge_value=200,
        parts=[(2500, 0), (2500, 30), (2500, 60), (2500, 90)],
    )
    owing = session.scalars(
        select(Invoice).where(Invoice.term_id == term_id, Invoice.kind == InvoiceKind.TERM).order_by(Invoice.number)
    ).all()[4:]
    amounts = amounts_for(session, [i.id for i in owing])
    candidates = [i for i in owing if amounts[i.id].paid_kobo == 0 and amounts[i.id].balance_kobo > 0]
    plan_offers.choose_many(
        session,
        candidates[3:11],
        pay_in_3.id,
        school_id=bursar.school_id,
        actor=bursar.actor,
        meta=META,
        created_by=bursar.user.id,
        staff=True,
    )
    session.commit()
    if len(candidates) > 12:  # one plan agreed by hand: half now-ish, half at the end of November
        invoice = candidates[12]
        owed = amounts[invoice.id].balance_kobo
        today = lagos_today()
        plans.create_plan(
            session,
            bursar,
            invoice.id,
            [(today + timedelta(days=3), owed // 2), (date(2026, 11, 27), owed - owed // 2)],
            note="Father between jobs; agreed at the PTA meeting",
        )


# --- Write-offs ------------------------------------------------------------------------------------------------


def _write_offs(session: Session, bursar: StaffContext, proprietor: StaffContext) -> None:
    old = session.scalars(
        select(Invoice).where(Invoice.kind == InvoiceKind.OPENING_BALANCE).order_by(Invoice.number)
    ).all()
    on_plan = set(session.scalars(select(PaymentPlan.invoice_id).where(PaymentPlan.cancelled_at.is_(None))))
    amounts = amounts_for(session, [i.id for i in old])
    owed = [i for i in old if amounts[i.id].balance_kobo > 0 and i.id not in on_plan]
    if owed:
        first = writeoffs.request(
            session,
            bursar,
            owed[0].id,
            amount_kobo=amounts[owed[0].id].balance_kobo,
            reason="Family relocated to Canada in July; no reply to calls or letters",
        )
        writeoffs.approve(session, proprietor, first.id)
    if len(owed) > 1:
        writeoffs.request(
            session,
            bursar,
            owed[1].id,
            amount_kobo=amounts[owed[1].id].balance_kobo // 2,
            reason="Half of last session's debt, agreed with the family after the father's illness",
        )


# --- Payments marked for a fee after they arrived ------------------------------------------------------------


def _marked_payments(session: Session, bursar: StaffContext) -> None:
    recent = session.scalars(
        select(Payment)
        .where(
            Payment.kind == PaymentKind.RECEIPT,
            Payment.method.in_([PaymentMethod.BANK_TRANSFER, PaymentMethod.VIRTUAL_ACCOUNT]),
        )
        .order_by(Payment.received_at.desc())
        .limit(25)
    ).all()
    wanted = [["PTA levy"], ["Development levy", "PTA levy"], ["PTA levy"]]
    for payment in recent:  # a payment whose bill has nothing open on those fees is refused: try the next
        if not wanted:
            break
        try:
            earmarks.mark_payment(session, bursar, payment.id, wanted[0])
            wanted.pop(0)
        except (DomainError, Conflict):
            session.rollback()


# --- Closed books -----------------------------------------------------------------------------------------------


def _close_last_session(session: Session, proprietor: StaffContext) -> None:
    third = session.scalar(
        select(Term.id).join(AcademicSession).where(AcademicSession.name == "2025/2026", Term.number == 3)
    )
    if third is not None:
        periods.close_term(session, proprietor, third)


# --- Paid into the bank (fake gateway) ------------------------------------------------------------------------


def paystack_fee(amount_kobo: int, method: PaymentMethod) -> int:
    """
    What Paystack keeps (its Nigerian pricing): a transfer into a pupil's dedicated account is 1% capped at ₦300;
    a card (or any checkout) payment is 1.5%, plus ₦100 from ₦2,500, capped at ₦2,000.
    """
    if method == PaymentMethod.VIRTUAL_ACCOUNT:
        return min(amount_kobo // 100, 300 * 100)
    fee = amount_kobo * 150 // 10_000 + (100 * 100 if amount_kobo >= 2_500 * 100 else 0)
    return min(fee, 2_000 * 100)


def _payouts(session: Session, school_id: uuid.UUID, subaccount: str) -> None:
    """The gateway's payouts for the last ten days, matched against payments by the real sync."""
    today = lagos_today()
    since = datetime.combine(today - timedelta(days=12), time(0), tzinfo=LAGOS)
    rows = session.execute(
        select(Payment.gateway_reference, Payment.amount_kobo, Payment.received_at, Payment.method)
        .where(Payment.gateway == GatewayCode.FAKE, Payment.received_at >= since, Payment.kind == PaymentKind.RECEIPT)
        .order_by(Payment.received_at)
    ).all()
    by_day: dict[date, list[SettledTransaction]] = {}
    for reference, amount, at, method in rows:
        payout_day = at.astimezone(LAGOS).date() + timedelta(days=1)
        if payout_day >= today or reference is None:
            continue  # still to be paid out
        by_day.setdefault(payout_day, []).append(SettledTransaction(reference, amount, paystack_fee(amount, method)))
    fake = FakeGateway.shared()
    days = sorted(by_day)
    for n, day in enumerate(days):
        unknown = 25_000 * 100  # a transfer WardPay never heard of
        extra = (
            [SettledTransaction("FAKE_UNKNOWN_7731", unknown, paystack_fee(unknown, PaymentMethod.VIRTUAL_ACCOUNT))]
            if n == len(days) // 2
            else []
        )
        fake.settle(subaccount, [], on=day, extra=by_day[day] + extra)
    if days:
        settlements.sync(session, school_id)


# --- The Lekki branch ---------------------------------------------------------------------------------------------


def _branch(mfa_secret: str, first_parent_phone: str, fake: bool) -> uuid.UUID:
    """A smaller second school with its own staff, pupils, bills and payments."""
    from app.demo import _calendar, _classes, _fees

    rng = random.Random(2027)  # noqa: S311  (demo data, not security)
    with SessionLocal(expire_on_commit=False) as session:
        scope_platform_wide(session)
        school = School(
            id=uuid.uuid4(),
            name="Demo Model College, Lekki",
            slug=BRANCH_SLUG,
            address="5 Admiralty Way, Lekki Phase 1, Lagos",
            phone="+234 801 000 0001",
            email="lekki@demomodelcollege.example.com",
        )
        session.add(school)
        session.flush()
        password = hash_password("demo-password-123")
        users = [
            User(
                id=uuid.uuid4(),
                school_id=school.id,
                email=email,
                full_name=name,
                role=role,
                password_hash=password,
                mfa_secret=mfa_secret,
                mfa_enabled=True,
            )
            for email, name, role in BRANCH_STAFF
        ]
        session.add_all(users)
        term = _calendar(session, school)
        arms = _classes(session, school)
        session.flush()
        level_ids = list(dict.fromkeys(a.class_level_id for a in arms))
        _fees(session, school, term, level_ids)
        students: list[Student] = []
        guardians: list[Guardian] = []
        links: list[GuardianStudent] = []
        enrollments: list[Enrollment] = []
        surnames = [
            "Okonkwo",
            "Balogun",
            "Ibrahim",
            "Etim",
            "Ogundipe",
            "Nwankwo",
            "Lawal",
            "Akande",
            "Uche",
            "Danjuma",
        ]
        firsts = [
            "Ada",
            "Tobi",
            "Musa",
            "Chioma",
            "Seyi",
            "Ifeanyi",
            "Aisha",
            "Kunle",
            "Ebere",
            "Femi",
            "Halima",
            "Dayo",
        ]
        family = 0
        while len(students) < 45:
            size = 1 if family == 0 else rng.choice([1, 1, 2, 2, 3])
            surname = "Adeyemi" if family == 0 else rng.choice(surnames)
            phone = first_parent_phone if family == 0 else f"+234809{7000000 + family:07d}"
            guardian = Guardian(
                id=uuid.uuid4(),
                school_id=school.id,
                full_name=f"Mrs {rng.choice(firsts)} {surname}" if family else "Mrs Adeyemi",
                phone=phone,
                phone_index=blind_index(phone),
                consent_at=datetime.now(LAGOS),
                consent_method=ConsentMethod.SCHOOL_ATTESTED,
            )
            guardians.append(guardian)
            for _ in range(size):
                arm = rng.choice(arms)
                student = Student(
                    id=uuid.uuid4(),
                    school_id=school.id,
                    admission_number=f"DML/2026/{len(students) + 1:04d}",
                    first_name="Kemi" if family == 0 else rng.choice(firsts),
                    last_name=surname,
                    gender=Gender.FEMALE if rng.random() < 0.5 else Gender.MALE,
                )
                students.append(student)
                enrollments.append(
                    Enrollment(
                        school_id=school.id,
                        student_id=student.id,
                        term_id=term.id,
                        arm_id=arm.id,
                        class_level_id=arm.class_level_id,
                    )
                )
                links.append(
                    GuardianStudent(
                        school_id=school.id,
                        guardian_id=guardian.id,
                        student_id=student.id,
                        relationship_to_student=Relationship.MOTHER,
                        is_primary=True,
                    )
                )
            family += 1
        session.add_all([*students, *guardians])
        session.flush()
        session.add_all([*enrollments, *links])
        if fake:  # the group shares one bank account: same account number, its own subaccount
            session.add(
                SchoolPaymentAccount(
                    school_id=school.id,
                    gateway=GatewayCode.FAKE,
                    subaccount_code="FAKE_SUB_DEMO_LEKKI",
                    bank_code="058",
                    bank_name="GTBank",
                    account_number="0123456789",
                    account_number_last4="6789",
                    account_name="DEMO MODEL COLLEGE",
                )
            )
            highest = session.scalar(
                select(VirtualAccount.account_number)
                .where(VirtualAccount.gateway == GatewayCode.FAKE)
                .order_by(VirtualAccount.account_number.desc())
                .limit(1)
            )
            first = int(highest) + 1 if highest and highest.isdigit() else 9_100_000_001
            session.add_all(
                VirtualAccount(
                    school_id=school.id,
                    student_id=s.id,
                    gateway=GatewayCode.FAKE,
                    account_number=str(first + n),
                    account_name=f"DML/{s.last_name.upper()} {s.first_name.upper()}",
                    bank_name="Fake Bank",
                    provider_reference=f"FAKE_CUS_{first + n}",
                )
                for n, s in enumerate(students)
            )
        session.commit()
        school_id, term_id, bursar_user = school.id, term.id, users[1]

    with _school_session(school_id) as session:
        bursar = _ctx(bursar_user, school_id)
        generate_term_invoices(
            session, bursar, term_id=term_id, due_date=date(2026, 9, 30), class_level_ids=None, dry_run=False
        )
        session.execute(
            update(Invoice)
            .where(Invoice.kind == InvoiceKind.TERM)
            .values(issued_at=datetime(2026, 9, 7, 8, tzinfo=LAGOS))
        )
        session.commit()
        bills = dict(
            session.execute(select(Invoice.student_id, Invoice.id).where(Invoice.kind == InvoiceKind.TERM)).all()
        )
        owed = amounts_for(session, list(bills.values()))
        yesterday = lagos_today() - timedelta(days=1)
        new = []
        for n, student in enumerate(students[1:], start=1):  # Kemi's bill stays open, for the portal demo
            roll = rng.random()
            if roll < 0.25:
                continue
            total = owed[bills[student.id]].balance_kobo
            amount = total if roll < 0.7 else total // 2 // 100_000 * 100_000
            method = PaymentMethod.VIRTUAL_ACCOUNT if fake and n % 4 else PaymentMethod.CASH
            day = date(2026, 9, 8) + timedelta(days=rng.randint(0, max((yesterday - date(2026, 9, 8)).days, 0)))
            new.append(
                NewPayment(
                    student_id=student.id,
                    amount_kobo=amount,
                    method=method,
                    received_at=datetime.combine(day, time(rng.randint(8, 16), rng.randint(0, 59)), tzinfo=LAGOS),
                    gateway=GatewayCode.FAKE if method == PaymentMethod.VIRTUAL_ACCOUNT else None,
                    gateway_reference=f"FAKE_DML_{uuid.uuid4().hex[:12].upper()}"
                    if method == PaymentMethod.VIRTUAL_ACCOUNT
                    else None,
                    recorded_by=bursar_user.id if method == PaymentMethod.CASH else None,
                    approved_by=users[0].id if method == PaymentMethod.CASH else None,
                )
            )
        record_payments(session, school_id=school_id, payments=new, actor=bursar.actor, meta=META)
        session.commit()
    return school_id


# --- The group and its head office ----------------------------------------------------------------------------------


def _group(session: Session, school_ids: list[uuid.UUID], mfa_secret: str) -> None:
    group = session.scalar(select(SchoolGroup).where(SchoolGroup.name == GROUP_NAME))
    if group is None:
        group = SchoolGroup(id=uuid.uuid4(), name=GROUP_NAME)
        session.add(group)
        session.flush()
    session.execute(update(School).where(School.id.in_(school_ids)).values(group_id=group.id))
    password = hash_password("demo-password-123")
    taken = set(session.scalars(select(User.email).where(User.email.in_([e for e, _, _ in HEAD_OFFICE]))))
    session.add_all(
        User(
            id=uuid.uuid4(),
            group_id=group.id,
            email=email,
            full_name=name,
            role=role,
            password_hash=password,
            mfa_secret=mfa_secret,
            mfa_enabled=True,
        )
        for email, name, role in HEAD_OFFICE
        if email not in taken
    )
    session.commit()


# --- WardPay's own bills to the schools ------------------------------------------------------------------------------


def _wardpay_bills(session: Session, platform_admin: User, branch_id: uuid.UUID) -> None:
    admin = Principal(user=platform_admin, session_id=uuid.uuid4(), meta=META)
    run = billing.generate(session, admin, dry_run=False)
    session.commit()
    branch_bill = next((b for b in run.bills if b.school_id == branch_id), None) if run.bills else None
    if branch_bill is None:
        return
    found = session.scalar(select(SubscriptionInvoice.id).where(SubscriptionInvoice.school_id == branch_id))
    if found is None:
        return
    row = billing.invoice_detail(session, found)[0]
    billing.record_payment(
        session,
        admin,
        found,
        amount_kobo=row.balance_kobo,
        received_on=lagos_today() - timedelta(days=1),
        reference="NIP/DEMO-LEKKI-WARDPAY",
    )
