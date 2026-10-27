"""
Authentication and authorization dependencies, and the one place an HTTP
request's database session gets its tenant scope.

    @router.get("/students")
    def list_students(ctx: StaffContext = Depends(require(Capability.STUDENTS_VIEW)),
                      db: Session = Depends(get_db)): ...

`require()` authenticates the bearer token against a live session, checks
the capability (and MFA enrolment for roles that need it), refuses
suspended schools, then scopes `db` to the caller's school. Head-office staff
of a school group act in the branch their session has chosen (`/auth/branch`),
as a proprietor or bursar of that branch. FastAPI caches
`get_db` per request, so the router's session is the scoped one.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotAuthenticated, PermissionDenied
from app.core.http import RequestMeta, request_meta
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.audit.service import Actor
from app.modules.schools.models import School, SchoolStatus

from .auth_service import actor_for, mfa_enrolment_required, scope_for_user
from .models import AuthSession, Role, User
from .permissions import Capability, role_has
from .security import decode_token

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


@dataclass(frozen=True)
class Principal:
    """A signed-in staff member or platform admin, before any capability check."""

    user: User
    session_id: uuid.UUID
    meta: RequestMeta
    # The school this request acts in: the user's own, or for head-office staff the branch they chose
    # (None while they have none, or the one they chose left the group or was suspended)
    acting_school_id: uuid.UUID | None = field(default=None, kw_only=True)

    @property
    def actor(self) -> Actor:
        return actor_for(self.user)


@dataclass(frozen=True)
class StaffContext(Principal):
    """A principal acting inside their school; the request's DB session is scoped to it."""

    school_id: uuid.UUID


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None, db: Session) -> Principal:
    """Who is calling, and the school they act in. Leaves the session's tenant scope to the caller."""
    claims = decode_token(credentials.credentials, "access") if credentials else None
    if claims is None or "sid" not in claims:
        raise NotAuthenticated("Please sign in.", code="not_authenticated")
    try:
        session_id = uuid.UUID(claims["sid"])
    except ValueError as e:
        raise NotAuthenticated("Please sign in.") from e
    # One query per request: session, user and the school they act in. For head-office staff that's the
    # session's branch, and only while it's still in their group.
    row = db.execute(
        select(AuthSession, User, School.status)
        .join(User, User.id == AuthSession.user_id)
        .outerjoin(
            School,
            or_(
                School.id == User.school_id,
                and_(User.school_id.is_(None), School.id == AuthSession.branch_id, School.group_id == User.group_id),
            ),
        )
        .where(AuthSession.id == session_id, AuthSession.user_id == uuid.UUID(claims["sub"]))
    ).first()
    if row is None:
        raise NotAuthenticated("Please sign in.", code="session_expired")
    auth_session, user, school_status = row
    if auth_session.revoked_at is not None or auth_session.expires_at <= datetime.now(UTC) or not user.is_active:
        raise NotAuthenticated("Your session has ended. Please sign in again.", code="session_expired")
    acting: uuid.UUID | None = None
    if user.school_id is not None:
        if school_status != SchoolStatus.ACTIVE:
            raise PermissionDenied(
                "This school's account is suspended. Please contact support.", code="school_suspended"
            )
        acting = user.school_id
    elif user.group_id is not None and school_status == SchoolStatus.ACTIVE:
        acting = auth_session.branch_id
    return Principal(user=user, session_id=session_id, meta=request_meta(request), acting_school_id=acting)


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    principal = _authenticate(request, credentials, db)
    scope_for_user(db, principal.user, principal.acting_school_id)
    return principal


@dataclass(frozen=True)
class HeadOffice(Principal):
    """Head-office staff reading across their group: the session is platform-wide, so services must filter
    to `branch_ids` themselves (the group's active branches, worked out here)."""

    group_id: uuid.UUID
    branch_ids: tuple[uuid.UUID, ...]


def require_head_office(capability: Capability) -> Callable[..., HeadOffice]:
    """
    Dependency for the few head-office endpoints that read every branch at once (owner, 2026-10-27: head
    office may see each branch's data). Read-only figures only: anything that changes a school goes through
    `require()` in the branch they've chosen.
    """

    def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        db: Session = Depends(get_db),
    ) -> HeadOffice:
        principal = _authenticate(request, credentials, db)
        user = principal.user
        if mfa_enrolment_required(user):
            raise PermissionDenied("Turn on two-factor sign-in to continue.", code="mfa_enrolment_required")
        if user.group_id is None or not role_has(user.role, capability):
            raise PermissionDenied("You don't have permission to do that.")
        branches = tuple(
            db.scalars(select(School.id).where(School.group_id == user.group_id, School.status == SchoolStatus.ACTIVE))
        )
        scope_platform_wide(db)
        return HeadOffice(
            user=user,
            session_id=principal.session_id,
            meta=principal.meta,
            acting_school_id=principal.acting_school_id,
            group_id=user.group_id,
            branch_ids=branches,
        )

    dependency.__name__ = f"require_head_office_{capability.value.replace('.', '_')}"
    return dependency


def require(capability: Capability) -> Callable[..., StaffContext]:
    """Dependency for school endpoints: the caller must hold `capability` in their own school."""

    def dependency(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> StaffContext:
        user = principal.user
        if mfa_enrolment_required(user):
            raise PermissionDenied("Turn on two-factor sign-in to continue.", code="mfa_enrolment_required")
        school_id = principal.acting_school_id
        if school_id is None and user.group_id is not None:
            raise PermissionDenied("Choose a branch to work in.", code="branch_required")
        if school_id is None or not role_has(user.role, capability):
            raise PermissionDenied("You don't have permission to do that.")
        scope_to_school(db, school_id)
        return StaffContext(
            user=user,
            session_id=principal.session_id,
            meta=principal.meta,
            acting_school_id=school_id,
            school_id=school_id,
        )

    dependency.__name__ = f"require_{capability.value.replace('.', '_')}"
    return dependency


def require_platform_admin(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> Principal:
    user = principal.user
    if user.role is not Role.PLATFORM_ADMIN:
        raise PermissionDenied("You don't have permission to do that.")
    if mfa_enrolment_required(user):
        raise PermissionDenied("Turn on two-factor sign-in to continue.", code="mfa_enrolment_required")
    scope_platform_wide(db)
    return principal
