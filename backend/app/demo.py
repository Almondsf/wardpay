"""
Demo data for local development and browser tests: `python -m app.cli seed-demo`.

Builds "Demo Model College" (Lagos) with staff, the 2026/2027 calendar,
classes from Primary 1 to SSS 3, students in families (brothers and sisters
share a parent, ages match their class), and first-term fees (tuition by
class, optional bus, levies, a sibling discount).

By default the school is also "lived in" (`app/demo_activity.py`): last
session's debts carried over, scholarships, bus sign-ups, the term's bills
generated, two months of payments (transfers, card, cash, tellers) with
receipts, messages to parents, a reversal and entries awaiting approval. A few
more schools are added so the platform console has something to show.

`--bare` keeps the small version (60 students, no bills yet) that the browser
tests rely on. Refuses to run in production. Everything is inserted in bulk
statements.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import blind_index
from app.core.db import SessionLocal
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.models import load_all_models
from app.modules.accounts.models import Role, User
from app.modules.accounts.security import hash_password
from app.modules.fees.models import Discount, DiscountKind, DiscountRule, FeeCategory, FeeItem, FeeSchedule
from app.modules.payments.gateways import GatewayCode, enabled_gateways
from app.modules.payments.models import SchoolPaymentAccount, VirtualAccount
from app.modules.schools.models import AcademicSession, Arm, ClassLevel, School, SchoolStatus, Term
from app.modules.students.models import (
    ConsentMethod,
    Enrollment,
    Gender,
    Guardian,
    GuardianStudent,
    Relationship,
    Student,
)

DEMO_PASSWORD = "demo-password-123"  # noqa: S105  (demo data only; refuses to run in production)
DEMO_SLUG = "demo-school"
PLATFORM_EMAIL = "platform@demo.wardpay.ng"
FIRST_PARENT_PHONE = "+2348035000000"  # two children; used in the docs and the browser tests

CLASSES = [
    *((f"Primary {n}", ["A"]) for n in range(1, 7)),
    ("JSS 1", ["A", "B"]),
    ("JSS 2", ["A", "B"]),
    ("JSS 3", ["A", "B"]),
    ("SSS 1", ["Science", "Arts"]),
    ("SSS 2", ["Science", "Arts"]),
    ("SSS 3", ["Science", "Arts"]),
]
SURNAMES = [
    "Okafor", "Adeyemi", "Bello", "Eze", "Ibrahim", "Okonkwo", "Balogun", "Nwosu", "Abubakar", "Ogunleye",
    "Chukwu", "Adebayo", "Musa", "Obi", "Afolabi", "Danjuma", "Etim", "Ojo", "Uche", "Lawal", "Akinola",
    "Nnamdi", "Oyelaran", "Bassey", "Yakubu", "Olawale", "Ekwueme", "Garba", "Onyeka", "Adewale", "Umeh",
    "Sanni", "Okeke", "Fashola", "Ikpe", "Aminu", "Odukoya", "Nwachukwu", "Ajayi", "Effiong",
]  # fmt: skip
GIRLS = [
    "Chiamaka", "Aisha", "Funmilayo", "Ngozi", "Zainab", "Adaeze", "Temitope", "Amina", "Ifeoma", "Kemi",
    "Oluwaseun", "Hauwa", "Chidinma", "Yetunde", "Blessing", "Fatima", "Nneka", "Bukola", "Ebere", "Halima",
    "Simisola", "Ijeoma", "Damilola", "Rukayat", "Ozioma",
]  # fmt: skip
BOYS = [
    "Emeka", "Tunde", "Ibrahim", "Chinedu", "Segun", "Musa", "Obinna", "Femi", "Yusuf", "Kelechi",
    "Ayodele", "Uchenna", "Abdullahi", "Tobi", "Ikenna", "Sadiq", "Olumide", "Chukwuemeka", "Babatunde",
    "Nnamdi", "Kayode", "Aliyu", "Ebuka", "Gbenga", "Somto",
]  # fmt: skip
MOTHER_TITLES = ["Mrs", "Mrs", "Mrs", "Dr (Mrs)", "Alhaja", "Barr. (Mrs)"]
FATHER_TITLES = ["Mr", "Mr", "Mr", "Dr", "Chief", "Alhaji", "Engr.", "Pastor"]
PHONE_PREFIXES = ["803", "806", "813", "816", "703", "706", "802", "808", "812", "902", "905", "810"]

# More schools for the platform console: (name, slug, address, proprietor name, email, status)
OTHER_SCHOOLS = [
    ("Kings and Queens Academy", "demo-kings-queens", "Plot 4 Gwarinpa Estate, Abuja", "Dr Ifeoma Nwachukwu",
     "proprietor@kingsqueens.demo.wardpay.ng", SchoolStatus.ACTIVE),
    ("Bright Futures Nursery and Primary School", "demo-bright-futures", "17 Ring Road, Ibadan", "Mrs Bukola Ajayi",
     "proprietor@brightfutures.demo.wardpay.ng", SchoolStatus.ACTIVE),
    ("Sunrise International School", "demo-sunrise", "9 Aba Road, Port Harcourt", "Engr. Ikenna Okeke",
     "proprietor@sunrise.demo.wardpay.ng", SchoolStatus.SUSPENDED),
]  # fmt: skip


@dataclass(frozen=True)
class DemoSchool:
    """What the activity seeder needs to know about the school just built."""

    school: School
    term: Term
    bursar: User
    admin: User
    level_ids: list[uuid.UUID]  # in rank order
    placements: dict[uuid.UUID, str]  # student id -> "JSS 1 A"
    primary_guardian: dict[uuid.UUID, Guardian]  # student id -> primary guardian
    students: list[Student]
    fee_items: dict[str, FeeItem]


def seed_demo(mfa_secret: str, reset: bool, *, bare: bool = False) -> None:
    if get_settings().is_production:
        raise SystemExit("seed-demo never runs in production.")
    load_all_models()
    # Objects stay usable after each commit (no reload query per student)
    with SessionLocal(expire_on_commit=False) as session:
        scope_platform_wide(session)
        if session.scalar(select(School.id).where(School.slug == DEMO_SLUG)) is not None:
            if not reset:
                raise SystemExit("The demo school already exists (use --reset on a throwaway database).")
            _retire_old_demo(session)
        school = School(
            id=uuid.uuid4(),
            name="Demo Model College",
            slug=DEMO_SLUG,
            address="12 Allen Avenue, Ikeja, Lagos",
            phone="+234 801 000 0000",
            email="bursary@demomodelcollege.example.com",
        )
        session.add(school)
        session.flush()
        bursar, admin = _staff(session, school, mfa_secret)
        term = _calendar(session, school)
        arms = _classes(session, school)
        built = _students(session, school, term, arms, count=60 if bare else 240)
        fee_items = _fees(session, school, term, built.level_ids)
        fake_gateway = GatewayCode.FAKE in enabled_gateways()
        if fake_gateway:
            _fake_payments_setup(session, school)
        session.commit()
    if bare:
        return
    from app import demo_activity  # builds on everything above

    built_school = DemoSchool(
        school=school,
        term=term,
        bursar=bursar,
        admin=admin,
        level_ids=built.level_ids,
        placements=built.placements,
        primary_guardian=built.primary_guardian,
        students=built.students,
        fee_items=fee_items,
    )
    with SessionLocal(expire_on_commit=False) as session:
        scope_to_school(session, school.id)  # the real invoicing and allocation code runs in a school's scope
        demo_activity.add_activity(session, built_school, transfers_by_account=fake_gateway)
    with SessionLocal() as session:
        scope_platform_wide(session)
        _other_schools(session)
        session.commit()
    from app import demo_extras  # every later feature: spending, cash book, plans, write-offs, branches…

    demo_extras.add_extras(school.id, term.id, mfa_secret, FIRST_PARENT_PHONE)


def _retire_old_demo(session: Session) -> None:
    """
    Retire the previous demo school: remove its staff, suspend it (so its parents
    and account numbers stop working) and rename it out of the way. Its rows stay,
    because money and audit records are never deleted.
    """
    from app import demo_extras

    demo_extras.retire(session)  # the Lekki branch and head office
    old_ids = select(School.id).where(School.slug == DEMO_SLUG).scalar_subquery()
    session.execute(delete(User).where(or_(User.school_id == old_ids, User.email == PLATFORM_EMAIL)))
    session.execute(
        update(School)
        .where(School.slug == DEMO_SLUG)
        .values(slug=f"old-demo-{uuid.uuid4().hex[:8]}", status=SchoolStatus.SUSPENDED)
    )
    # Demo schools retired before suspension was added
    session.execute(
        update(School)
        .where(School.slug.like("old-demo-%"), School.status == SchoolStatus.ACTIVE)
        .values(status=SchoolStatus.SUSPENDED)
    )
    session.flush()


def _staff(session: Session, school: School, mfa_secret: str) -> tuple[User, User]:
    people = [
        ("proprietor@demo.wardpay.ng", "Grace Adeyemi", Role.PROPRIETOR, school.id, True),
        ("bursar@demo.wardpay.ng", "Musa Bello", Role.BURSAR, school.id, True),
        # With ENFORCE_MFA off (local development only), Ada signs in with just the password
        ("admin@demo.wardpay.ng", "Ada Obi", Role.SCHOOL_ADMIN, school.id, get_settings().enforce_mfa),
        (PLATFORM_EMAIL, "Platform Ops", Role.PLATFORM_ADMIN, None, True),
    ]
    password_hash = hash_password(DEMO_PASSWORD)
    users = [
        User(
            id=uuid.uuid4(),
            school_id=school_id,
            email=email,
            full_name=name,
            role=role,
            password_hash=password_hash,
            mfa_secret=mfa_secret if mfa else None,
            mfa_enabled=mfa,
        )
        for email, name, role, school_id, mfa in people
    ]
    session.add_all(users)
    return users[1], users[2]


def _calendar(session: Session, school: School) -> Term:
    academic = AcademicSession(
        id=uuid.uuid4(), school_id=school.id, name="2026/2027", start_date=date(2026, 9, 1), end_date=date(2027, 7, 31)
    )
    terms = [
        Term(
            id=uuid.uuid4(),
            school_id=school.id,
            session_id=academic.id,
            number=n,
            start_date=s,
            end_date=e,
            is_current=n == 1,
        )
        for n, (s, e) in enumerate(
            [
                (date(2026, 9, 7), date(2026, 12, 11)),
                (date(2027, 1, 4), date(2027, 4, 1)),
                (date(2027, 4, 19), date(2027, 7, 23)),
            ],
            start=1,
        )
    ]
    session.add(academic)
    session.flush()
    session.add_all(terms)
    return terms[0]


def _classes(session: Session, school: School) -> list[Arm]:
    levels: list[ClassLevel] = []
    arms: list[Arm] = []
    for rank, (name, arm_names) in enumerate(CLASSES, start=1):
        level = ClassLevel(id=uuid.uuid4(), school_id=school.id, name=name, rank=rank)
        levels.append(level)
        arms.extend(Arm(id=uuid.uuid4(), school_id=school.id, class_level_id=level.id, name=a) for a in arm_names)
    session.add_all(levels)
    session.flush()
    session.add_all(arms)
    return arms


@dataclass(frozen=True)
class _Built:
    level_ids: list[uuid.UUID]
    placements: dict[uuid.UUID, str]
    primary_guardian: dict[uuid.UUID, Guardian]
    students: list[Student]


def _students(session: Session, school: School, term: Term, arms: list[Arm], count: int) -> _Built:
    """
    Families of one to three children with one parent each (the first family has two
    children on FIRST_PARENT_PHONE). Children's ages match their class.
    """
    rng = random.Random(2026)  # noqa: S311  (demo data, not security)
    now = datetime.now(UTC)
    level_ids = list(dict.fromkeys(a.class_level_id for a in arms))
    level_names = {lid: name for lid, (name, _) in zip(level_ids, CLASSES, strict=True)}
    arms_by_level: dict[uuid.UUID, list[Arm]] = {}
    for arm in arms:
        arms_by_level.setdefault(arm.class_level_id, []).append(arm)

    students: list[Student] = []
    guardians: list[Guardian] = []
    links: list[GuardianStudent] = []
    enrollments: list[Enrollment] = []
    placements: dict[uuid.UUID, str] = {}
    primary: dict[uuid.UUID, Guardian] = {}
    family = 0
    while len(students) < count:
        size = 2 if family == 0 else rng.choices([1, 2, 3], weights=[55, 30, 15])[0]
        size = min(size, count - len(students))
        surname = SURNAMES[family % len(SURNAMES)] if family < len(SURNAMES) else rng.choice(SURNAMES)
        mother = family == 0 or rng.random() < 0.7
        phone = (
            FIRST_PARENT_PHONE
            if family == 0
            else f"+234{PHONE_PREFIXES[family % len(PHONE_PREFIXES)]}{5000000 + family:07d}"
        )
        given = rng.choice(GIRLS if mother else BOYS)
        guardian = Guardian(
            id=uuid.uuid4(),
            school_id=school.id,
            full_name=f"{'Mrs' if family == 0 else rng.choice(MOTHER_TITLES if mother else FATHER_TITLES)} "
            f"{given} {surname}",
            phone=phone,
            phone_index=blind_index(phone),
            email=f"{given.lower()}.{surname.lower()}{family}@example.com" if rng.random() < 0.35 else None,
            consent_at=now,
            consent_method=ConsentMethod.SCHOOL_ATTESTED,
        )
        guardians.append(guardian)
        ranks = sorted(rng.sample(range(len(level_ids)), size))
        for rank in ranks:
            level_id = level_ids[rank]
            arm = rng.choice(arms_by_level[level_id])
            girl = rng.random() < 0.5
            entry_year = 2026 - rank if rank < 6 else 2026 - (rank - 6)  # primary, then secondary intake
            student = Student(
                id=uuid.uuid4(),
                school_id=school.id,
                admission_number=f"DMC/{entry_year}/{len(students) + 1:04d}",
                first_name=rng.choice(GIRLS if girl else BOYS),
                last_name=surname,
                gender=Gender.FEMALE if girl else Gender.MALE,
                date_of_birth=date(2020 - rank, rng.randint(1, 12), rng.randint(1, 28)),
            )
            students.append(student)
            placements[student.id] = f"{level_names[level_id]} {arm.name}"
            primary[student.id] = guardian
            enrollments.append(
                Enrollment(
                    id=uuid.uuid4(),
                    school_id=school.id,
                    student_id=student.id,
                    term_id=term.id,
                    arm_id=arm.id,
                    class_level_id=level_id,
                )
            )
            links.append(
                GuardianStudent(
                    school_id=school.id,
                    guardian_id=guardian.id,
                    student_id=student.id,
                    relationship_to_student=Relationship.MOTHER if mother else Relationship.FATHER,
                    is_primary=True,
                )
            )
        family += 1
    session.add_all([*students, *guardians])
    session.flush()
    session.add_all([*enrollments, *links])
    return _Built(level_ids, placements, primary, students)


def _fees(session: Session, school: School, term: Term, level_ids: list[uuid.UUID]) -> dict[str, FeeItem]:
    """Tuition rising by class, an optional bus fee, PTA and development levies, and a sibling discount."""
    items = {
        name: FeeItem(id=uuid.uuid4(), school_id=school.id, name=name, category=category)
        for name, category in (
            ("Tuition", FeeCategory.TUITION),
            ("School bus", FeeCategory.SERVICE),
            ("PTA levy", FeeCategory.LEVY),
            ("Development levy", FeeCategory.LEVY),
        )
    }
    session.add_all(items.values())
    session.flush()  # no ORM relationship tells the flush to insert items before the discount
    session.add(
        Discount(
            id=uuid.uuid4(),
            school_id=school.id,
            name="Sibling discount (10% of tuition)",
            kind=DiscountKind.PERCENT,
            value=1000,
            rule=DiscountRule.SIBLING,
            fee_item_id=items["Tuition"].id,
        )
    )
    session.flush()
    schedules = []
    for rank, level_id in enumerate(level_ids):
        for item, naira, optional in (
            ("Tuition", 120_000 + rank * 15_000, False),
            ("School bus", 45_000, True),
            ("PTA levy", 5_000, False),
            ("Development levy", 10_000, False),
        ):
            schedules.append(
                FeeSchedule(
                    school_id=school.id,
                    term_id=term.id,
                    class_level_id=level_id,
                    fee_item_id=items[item].id,
                    amount_kobo=naira * 100,
                    is_optional=optional,
                )
            )
    session.add_all(schedules)
    return items


def _fake_payments_setup(session: Session, school: School) -> None:
    """With the fake gateway: a connected bank account and a virtual account for every student."""
    session.add(
        SchoolPaymentAccount(
            school_id=school.id,
            gateway=GatewayCode.FAKE,
            subaccount_code="FAKE_SUB_DEMO",
            bank_code="058",
            bank_name="GTBank",
            account_number="0123456789",
            account_number_last4="6789",
            account_name="DEMO MODEL COLLEGE",
        )
    )
    students = session.execute(
        select(Student.id, Student.first_name, Student.last_name)
        .where(Student.school_id == school.id)
        .order_by(Student.admission_number)
    ).all()
    # Carry on after the highest fake number so far, so re-seeding never reuses one
    highest = session.scalar(
        select(func.max(VirtualAccount.account_number)).where(VirtualAccount.gateway == GatewayCode.FAKE)
    )
    first = int(highest) + 1 if highest and highest.isdigit() else 9_000_000_001
    session.add_all(
        VirtualAccount(
            school_id=school.id,
            student_id=sid,
            gateway=GatewayCode.FAKE,
            account_number=str(first + n),
            account_name=f"DMC/{last.upper()} {given.upper()}",
            bank_name="Fake Bank",
            provider_reference=f"FAKE_CUS_{first + n}",
        )
        for n, (sid, given, last) in enumerate(students)
    )


def _other_schools(session: Session) -> None:
    """A few more schools (each with a proprietor) for the platform console. Kept across re-seeds."""
    existing = set(session.scalars(select(School.slug).where(School.slug.in_([s[1] for s in OTHER_SCHOOLS]))))
    taken = set(session.scalars(select(User.email).where(User.email.in_([s[4] for s in OTHER_SCHOOLS]))))
    password_hash = hash_password(DEMO_PASSWORD)
    schools, users = [], []
    for name, slug, address, owner, email, status in OTHER_SCHOOLS:
        if slug in existing:
            continue
        school = School(id=uuid.uuid4(), name=name, slug=slug, address=address, status=status)
        schools.append(school)
        if email not in taken:
            users.append(
                User(
                    id=uuid.uuid4(),
                    school_id=school.id,
                    email=email,
                    full_name=owner,
                    role=Role.PROPRIETOR,
                    password_hash=password_hash,
                )
            )
    session.add_all(schools)
    session.flush()
    session.add_all(users)
