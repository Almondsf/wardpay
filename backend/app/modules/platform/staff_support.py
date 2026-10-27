"""
Helping a school's staff when they're locked out and the school can't help itself (owner, 2026-10-22):
typically the proprietor, who is the only one who can manage staff.

- **Send a password link**: the invite link again if they never set a password, else a reset link.
  It goes to their own email, like "Forgot password", so the platform admin never sees a password.
- **Reset two-factor sign-in**: for a lost phone. Their sessions are signed out and they enrol a new
  authenticator at their next sign-in (proprietors and bursars must).

Both are recorded in the school's activity log and the platform's trail. The same help exists for a school
group's head-office staff (`group_id` instead of `school_id`), recorded in the platform's trail.
"""

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.modules.accounts import mfa
from app.modules.accounts.auth_service import revoke_sessions, send_password_link
from app.modules.accounts.deps import Principal
from app.modules.accounts.models import STAFF_ROLES, User
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record


def _staff_member(
    session: Session, user_id: uuid.UUID, *, school_id: uuid.UUID | None = None, group_id: uuid.UUID | None = None
) -> User:
    owner = User.school_id == school_id if school_id is not None else User.group_id == group_id
    user = session.scalar(select(User).where(User.id == user_id, owner, User.role.in_(STAFF_ROLES)).with_for_update())
    if user is None:
        raise NotFound("Staff member not found.")
    return user


def _audit(session: Session, admin: Principal, action: AuditAction, user: User, changes: dict[str, object]) -> None:
    for school in {None, user.school_id}:
        record(session, action, school_id=school, actor=admin.actor, meta=admin.meta, target=user, changes=changes)


def send_link(
    session: Session,
    admin: Principal,
    school_id: uuid.UUID | None,
    user_id: uuid.UUID,
    *,
    group_id: uuid.UUID | None = None,
) -> Literal["invite", "reset"]:
    """Email the invite (no password yet) or a reset link. Returns which one went."""
    user = _staff_member(session, user_id, school_id=school_id, group_id=group_id)
    if not user.is_active:
        who = "Reactivate it first" if user.group_id else "The school's proprietor can reactivate it"
        raise Conflict(f"This account is deactivated. {who}.")
    purpose: Literal["invite", "reset"] = "invite" if user.password_hash is None else "reset"
    send_password_link(session, user, purpose)
    _audit(session, admin, AuditAction.PLATFORM_STAFF_LINK_SENT, user, {"purpose": purpose})
    session.commit()
    return purpose


def reset_two_factor(
    session: Session,
    admin: Principal,
    school_id: uuid.UUID | None,
    user_id: uuid.UUID,
    *,
    group_id: uuid.UUID | None = None,
) -> None:
    user = _staff_member(session, user_id, school_id=school_id, group_id=group_id)
    if not user.mfa_enabled:
        raise Conflict("Two-factor sign-in isn't on for this account.")
    mfa.disable(session, user)
    revoke_sessions(session, user.id, "mfa_reset")
    _audit(session, admin, AuditAction.STAFF_MFA_RESET, user, {"by": "platform"})
    session.commit()
