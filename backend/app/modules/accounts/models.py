import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, Timestamped, UUIDPrimaryKey, enum_type
from app.core.crypto import EncryptedString
from app.modules.schools.models import School


class Role(enum.StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    PROPRIETOR = "proprietor"
    BURSAR = "bursar"
    SCHOOL_ADMIN = "school_admin"
    PARENT = "parent"


STAFF_ROLES = (Role.PROPRIETOR, Role.BURSAR, Role.SCHOOL_ADMIN)


class User(UUIDPrimaryKey, Timestamped, Base):
    """
    A person who signs in with email + password: school staff, head-office
    staff of a school group (`group_id`, a proprietor or bursar working in any
    of the group's branches), or a platform admin (who belongs to neither). Parents sign in by phone and are
    modelled as guardians (see the parent portal), not here.

    Not a TenantMixin model: sign-in has to find the user before the school is
    known. Staff queries in services go through `school_id` explicitly, and the
    check constraint ties role and school together.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(role = 'platform_admin' AND school_id IS NULL AND group_id IS NULL)"
            " OR (role <> 'platform_admin' AND school_id IS NOT NULL AND group_id IS NULL)"
            " OR (role IN ('proprietor', 'bursar') AND school_id IS NULL AND group_id IS NOT NULL)",
            name="role_matches_school",
        ),
        CheckConstraint("role <> 'parent'", name="staff_roles_only"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)
    # Head-office staff: the school group they work for, instead of one school
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("school_groups.id", ondelete="RESTRICT"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(150))
    role: Mapped[Role] = mapped_column(enum_type(Role, "user_role"))
    # Null until the invited person sets a password
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Two-factor sign-in (TOTP). The secret is encrypted at rest.
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedString)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last accepted TOTP time step, so a code can never be used twice
    mfa_last_step: Mapped[int] = mapped_column(BigInteger, default=0)

    school: Mapped[School | None] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<User {self.id} {self.role}>"


class RecoveryCode(UUIDPrimaryKey, Base):
    """Single-use MFA backup codes, stored hashed."""

    __tablename__ = "recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(UUIDPrimaryKey, Base):
    """
    One signed-in device. Access tokens carry the session id and are rejected
    once the session is revoked, so sign-out, deactivation and password changes
    take effect immediately rather than when the token expires.

    The refresh token is stored hashed and rotates on every use. Presenting an
    already-rotated token means it was copied: the session is revoked.
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_active", "user_id", postgresql_where="revoked_at IS NULL"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str] = mapped_column(String(50), default="")
    ip_address: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    # Head-office staff: the branch this signed-in device is working in (checked against the group on every request)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id", ondelete="SET NULL"))

    user: Mapped[User] = relationship(lazy="raise")


class RotatedRefreshToken(Base):
    """
    Hashes of refresh tokens that have already been rotated away. If one is
    presented again, someone copied it: the session it belonged to is revoked.
    Pruned once the session itself has expired.
    """

    __tablename__ = "rotated_refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
