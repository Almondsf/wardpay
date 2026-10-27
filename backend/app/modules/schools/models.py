import enum
import uuid
from datetime import date

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, SmallInteger, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, Timestamped, UUIDPrimaryKey, enum_type
from app.core.tenancy import TenantMixin


class SchoolStatus(enum.StrEnum):
    PENDING = "pending"  # signed up, proprietor hasn't confirmed their email (set a password) yet
    ACTIVE = "active"
    SUSPENDED = "suspended"  # staff and parents can't sign in; data is kept


class SchoolOrigin(enum.StrEnum):
    PLATFORM = "platform"  # created by a platform admin
    SIGNUP = "signup"  # the school signed itself up


class FamilySplitRule(enum.StrEnum):
    """How one amount a parent pays for several children is shared (see payments/family_split.py)."""

    CLEAR_MOST = "clear_most"  # earlier bills first, then the smallest balance this term first
    PROPORTIONAL = "proportional"
    ELDEST_FIRST = "eldest_first"
    YOUNGEST_FIRST = "youngest_first"


class SchoolGroup(UUIDPrimaryKey, Timestamped, Base):
    """
    Several schools (branches or campuses) with one owner. Each branch stays its own tenant: its own pupils,
    bills, numbering, bank account and WardPay bill. The group only lets head-office staff (users with
    `group_id`) sign in once and work in any branch (owner, 2026-10-27). Set up by the platform admin.
    """

    __tablename__ = "school_groups"

    name: Mapped[str] = mapped_column(String(200), unique=True)

    def __repr__(self) -> str:
        return f"<SchoolGroup {self.name}>"


class School(UUIDPrimaryKey, Timestamped, Base):
    """The tenant. Everything a school owns hangs off this row via TenantMixin."""

    __tablename__ = "schools"
    __table_args__ = (CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_format"),)

    name: Mapped[str] = mapped_column(String(200))
    # Short, URL-safe and unique: used in support lookups and receipt numbers
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    status: Mapped[SchoolStatus] = mapped_column(enum_type(SchoolStatus, "school_status"), default=SchoolStatus.ACTIVE)
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    origin: Mapped[SchoolOrigin] = mapped_column(
        enum_type(SchoolOrigin, "school_origin"), default=SchoolOrigin.PLATFORM
    )
    # Its subscription plan; None means the default plan
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"))
    # The proprietor's choice for sharing a family's one-amount payment between its children
    family_split_rule: Mapped[FamilySplitRule] = mapped_column(
        enum_type(FamilySplitRule, "family_split_rule"),
        default=FamilySplitRule.CLEAR_MOST,
        server_default=FamilySplitRule.CLEAR_MOST.value,
    )
    # Nothing dated on or before this day can be recorded (a closed term); see accounting/periods.py
    books_closed_through: Mapped[date | None] = mapped_column(Date)
    # The group it's a branch of, if any
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("school_groups.id", ondelete="RESTRICT"), index=True)

    def __repr__(self) -> str:
        return f"<School {self.slug}>"


# --- Academic calendar and structure -----------------------------------------------------------


class AcademicSession(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """A school year, e.g. "2026/2027" (September to July), split into three terms."""

    __tablename__ = "academic_sessions"
    __table_args__ = (
        UniqueConstraint("school_id", "name"),
        CheckConstraint("start_date < end_date", name="dates_in_order"),
    )

    name: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)

    terms: Mapped[list["Term"]] = relationship(back_populates="session", order_by="Term.number", lazy="raise")


class Term(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    One of a session's three terms. Fees are billed per term. Exactly one term
    per school is current: it's what the console shows by default and where new
    students are enrolled.
    """

    __tablename__ = "terms"
    __table_args__ = (
        UniqueConstraint("session_id", "number"),
        CheckConstraint("number BETWEEN 1 AND 3", name="number_range"),
        CheckConstraint("start_date < end_date", name="dates_in_order"),
        Index("uq_terms_one_current_per_school", "school_id", unique=True, postgresql_where=text("is_current")),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("academic_sessions.id", ondelete="RESTRICT"), index=True)
    number: Mapped[int] = mapped_column(SmallInteger)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    session: Mapped[AcademicSession] = relationship(back_populates="terms", lazy="raise")

    @property
    def label(self) -> str:
        return f"{('First', 'Second', 'Third')[self.number - 1]} term"


class ClassLevel(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """
    A year group, e.g. "JSS 1" or "Primary 4". `rank` orders levels for
    promotion: at the end of a session students move to the next active level
    by rank, and students in the highest level graduate.
    """

    __tablename__ = "class_levels"
    __table_args__ = (UniqueConstraint("school_id", "name"), UniqueConstraint("school_id", "rank"))

    name: Mapped[str] = mapped_column(String(50))
    rank: Mapped[int] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    arms: Mapped[list["Arm"]] = relationship(back_populates="class_level", order_by="Arm.name", lazy="raise")


class Arm(TenantMixin, UUIDPrimaryKey, Timestamped, Base):
    """A stream within a class level, e.g. JSS 1 "A" or "Gold". Every level has at least one."""

    __tablename__ = "arms"
    __table_args__ = (
        UniqueConstraint("class_level_id", "name"),
        # Target of enrollments' composite FK, which keeps an enrolment's level and arm consistent
        UniqueConstraint("id", "class_level_id", name="uq_arms_id_level"),
    )

    class_level_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("class_levels.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    class_level: Mapped[ClassLevel] = relationship(back_populates="arms", lazy="raise")
