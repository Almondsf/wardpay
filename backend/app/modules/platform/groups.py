"""
School groups: one owner's several branches or campuses (owner, 2026-10-27, "option A").

Each branch stays its own school (its own pupils, bills, receipt numbers, bank account and WardPay bill).
What a group adds is **head-office staff**: the owner and a central bursar sign in once and work in any
branch, choosing it from a menu (`/auth/branch`), with that branch's proprietor or bursar permissions.
Staff of one branch still see only their branch.

The platform admin sets groups up: name a group, attach schools, invite head-office staff. Every change is in
the platform's trail; adding or removing a branch is also in that branch's own log.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, DomainError, NotFound
from app.modules.accounts.auth_service import normalize_email, revoke_sessions, send_password_link
from app.modules.accounts.deps import Principal
from app.modules.accounts.models import AuthSession, Role, User
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.schools.models import School, SchoolGroup, SchoolStatus

# Head-office roles (owner, 2026-10-27: "Owner + central bursar")
GROUP_ROLES = (Role.PROPRIETOR, Role.BURSAR)


@dataclass(frozen=True)
class GroupRow:
    group: SchoolGroup
    branches: int
    staff: int


@dataclass(frozen=True)
class BranchRow:
    id: uuid.UUID
    name: str
    slug: str
    status: SchoolStatus


@dataclass(frozen=True)
class GroupDetail:
    group: SchoolGroup
    branches: list[BranchRow]
    staff: list[User]


def groups(session: Session) -> list[GroupRow]:
    """Every group with how many branches and head-office staff it has (one query)."""
    branches = select(func.count()).where(School.group_id == SchoolGroup.id).correlate(SchoolGroup).scalar_subquery()
    staff = (
        select(func.count())
        .where(User.group_id == SchoolGroup.id, User.is_active.is_(True))
        .correlate(SchoolGroup)
        .scalar_subquery()
    )
    rows = session.execute(select(SchoolGroup, branches, staff).order_by(SchoolGroup.name))
    return [GroupRow(group, int(b), int(s)) for group, b, s in rows]


def _group(session: Session, group_id: uuid.UUID, *, lock: bool = False) -> SchoolGroup:
    group = session.get(SchoolGroup, group_id, with_for_update=lock)
    if group is None:
        raise NotFound("Group not found.")
    return group


def detail(session: Session, group_id: uuid.UUID) -> GroupDetail:
    group = _group(session, group_id)
    branches = [
        BranchRow(r.id, r.name, r.slug, r.status)
        for r in session.execute(
            select(School.id, School.name, School.slug, School.status)
            .where(School.group_id == group_id)
            .order_by(School.name)
        )
    ]
    staff = list(session.scalars(select(User).where(User.group_id == group_id).order_by(User.role, User.full_name)))
    return GroupDetail(group, branches, staff)


def save(session: Session, admin: Principal, name: str, group_id: uuid.UUID | None = None) -> SchoolGroup:
    """Create a group, or rename one."""
    name = " ".join(name.split())
    if not name:
        raise DomainError("Give the group a name.", field="name")
    if group_id is None:
        group = SchoolGroup(id=uuid.uuid4(), name=name)
        session.add(group)
        before = None
    else:
        group = _group(session, group_id, lock=True)
        before, group.name = group.name, name
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise Conflict("There's already a group with that name.", field="name") from e
    record(
        session,
        AuditAction.GROUP_SAVED,
        school_id=None,
        actor=admin.actor,
        meta=admin.meta,
        target=group,
        changes={"name": [before, name]},
    )
    session.commit()
    return group


def add_branch(session: Session, admin: Principal, group_id: uuid.UUID, school_id: uuid.UUID) -> None:
    group = _group(session, group_id, lock=True)
    school = session.get(School, school_id, with_for_update=True)
    if school is None:
        raise NotFound("School not found.")
    if school.group_id == group.id:
        return
    if school.group_id is not None:
        raise Conflict("That school is already a branch of another group. Remove it there first.")
    school.group_id = group.id
    _audit_branch(session, admin, AuditAction.GROUP_BRANCH_ADDED, group, school)
    session.commit()


def remove_branch(session: Session, admin: Principal, group_id: uuid.UUID, school_id: uuid.UUID) -> None:
    """Head office loses access at once: their requests stop matching the branch, and devices working in it
    are moved off it."""
    group = _group(session, group_id, lock=True)
    school = session.scalar(select(School).where(School.id == school_id, School.group_id == group.id).with_for_update())
    if school is None:
        raise NotFound("That school isn't a branch of this group.")
    school.group_id = None
    session.execute(
        update(AuthSession)
        .where(
            AuthSession.branch_id == school.id,
            AuthSession.user_id.in_(select(User.id).where(User.group_id == group.id)),
        )
        .values(branch_id=None)
    )
    _audit_branch(session, admin, AuditAction.GROUP_BRANCH_REMOVED, group, school)
    session.commit()


def _audit_branch(session: Session, admin: Principal, action: AuditAction, group: SchoolGroup, school: School) -> None:
    for where in (None, school.id):
        record(
            session,
            action,
            school_id=where,
            actor=admin.actor,
            meta=admin.meta,
            target=group,
            changes={"group": group.name, "school": school.name},
        )


def invite(session: Session, admin: Principal, group_id: uuid.UUID, *, email: str, full_name: str, role: Role) -> User:
    """Add head-office staff; they set a password from the emailed link (and turn on two-factor sign-in)."""
    group = _group(session, group_id)
    if role not in GROUP_ROLES:
        raise DomainError("Head office is the owner (proprietor) or a central bursar.", field="role")
    email = normalize_email(email)
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        raise Conflict("Someone already has an account with that email.", field="email")
    user = User(id=uuid.uuid4(), group_id=group.id, email=email, full_name=full_name.strip(), role=role)
    session.add(user)
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise Conflict("Someone already has an account with that email.", field="email") from e
    send_password_link(session, user, "invite")
    record(
        session,
        AuditAction.GROUP_STAFF_INVITED,
        school_id=None,
        actor=admin.actor,
        meta=admin.meta,
        target=user,
        changes={"group": group.name, "role": role.value},
    )
    session.commit()
    return user


def set_active(session: Session, admin: Principal, group_id: uuid.UUID, user_id: uuid.UUID, active: bool) -> User:
    """Deactivating signs them out everywhere at once."""
    user = session.scalar(select(User).where(User.id == user_id, User.group_id == group_id).with_for_update())
    if user is None:
        raise NotFound("Staff member not found.")
    if user.is_active == active:
        return user
    user.is_active = active
    if not active:
        revoke_sessions(session, user.id, "deactivated")
    record(
        session,
        AuditAction.GROUP_STAFF_REACTIVATED if active else AuditAction.GROUP_STAFF_DEACTIVATED,
        school_id=None,
        actor=admin.actor,
        meta=admin.meta,
        target=user,
    )
    session.commit()
    return user
