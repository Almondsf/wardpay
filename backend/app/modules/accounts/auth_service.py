"""
Staff sign-in, sessions, passwords and two-factor sign-in.

Every function takes the session and does its own commit. Failures that
must be audited (bad password, refresh-token reuse) commit the audit entry
before raising.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import outbox
from app.core.config import get_settings
from app.core.errors import Conflict, DomainError, NotAuthenticated, PermissionDenied
from app.core.http import RequestMeta
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record
from app.modules.schools.models import School, SchoolStatus

from . import mfa
from .models import AuthSession, RotatedRefreshToken, User
from .permissions import MFA_REQUIRED_ROLES
from .security import (
    check_password_policy,
    decode_token,
    encode_token,
    hash_password,
    hash_token,
    new_refresh_token,
    password_fingerprint,
    verify_password,
)

INVALID_CREDENTIALS = "Incorrect email or password."


def normalize_email(email: str) -> str:
    return email.strip().lower()


def scope_for_user(session: Session, user: User, branch_id: uuid.UUID | None = None) -> None:
    """
    Once we know who's acting, the session acts for their school, platform-wide for platform admins, or for
    head-office staff the branch they're working in. Head-office staff with no branch chosen get no scope at
    all, so touching any school's data fails closed.
    """
    if user.school_id is not None:
        scope_to_school(session, user.school_id)
    elif user.group_id is not None:
        if branch_id is not None:
            scope_to_school(session, branch_id)
    else:
        scope_platform_wide(session)


def group_branches(session: Session, group_id: uuid.UUID) -> list[School]:
    """The branches a head-office user can work in: the group's active schools, by name."""
    return list(
        session.scalars(
            select(School)
            .where(School.group_id == group_id, School.status == SchoolStatus.ACTIVE)
            .order_by(School.name)
        )
    )


def actor_for(user: User) -> Actor:
    return Actor(user_id=user.id, label=user.email)


def mfa_enrolment_required(user: User) -> bool:
    return get_settings().enforce_mfa and user.role in MFA_REQUIRED_ROLES and not user.mfa_enabled


@dataclass(frozen=True)
class IssuedSession:
    user: User
    access_token: str
    expires_in: int
    refresh_token: str
    session_id: uuid.UUID
    # Head-office staff: the branch the session is working in
    branch_id: uuid.UUID | None = None


@dataclass(frozen=True)
class MfaChallenge:
    mfa_ticket: str


def _school_is_active(session: Session, user: User) -> bool:
    if user.school_id is None:
        return True
    status = session.scalar(select(School.status).where(School.id == user.school_id))
    return status == SchoolStatus.ACTIVE


def _issue(session: Session, user: User, meta: RequestMeta) -> IssuedSession:
    settings = get_settings()
    refresh = new_refresh_token()
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_hash=hash_token(refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        ip_address=meta.ip,
        user_agent=meta.user_agent,
    )
    if user.group_id is not None:  # head office starts in the first branch, by name
        auth_session.branch_id = session.scalar(
            select(School.id)
            .where(School.group_id == user.group_id, School.status == SchoolStatus.ACTIVE)
            .order_by(School.name)
            .limit(1)
        )
    session.add(auth_session)
    user.last_login_at = datetime.now(UTC)
    access, expires_in = _access_token(user, auth_session.id)
    return IssuedSession(
        user=user,
        access_token=access,
        expires_in=expires_in,
        refresh_token=refresh,
        session_id=auth_session.id,
        branch_id=auth_session.branch_id,
    )


def _access_token(user: User, session_id: uuid.UUID) -> tuple[str, int]:
    """(token, seconds until it expires)"""
    minutes = get_settings().access_token_minutes
    token = encode_token(
        "access",
        user.id,
        timedelta(minutes=minutes),
        sid=str(session_id),
        role=user.role.value,
        sch=str(user.school_id) if user.school_id else None,
    )
    return token, minutes * 60


def _audit_login(session: Session, user: User, meta: RequestMeta, **changes: object) -> None:
    record(
        session,
        AuditAction.LOGIN_SUCCEEDED,
        school_id=user.school_id,
        actor=actor_for(user),
        meta=meta,
        target=user,
        changes=dict(changes),
    )


def login(session: Session, email: str, password: str, meta: RequestMeta) -> IssuedSession | MfaChallenge:
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    if user is not None:
        scope_for_user(session, user)
    if user is None or not verify_password(password, user.password_hash) or not user.is_active:
        record(
            session,
            AuditAction.LOGIN_FAILED,
            school_id=user.school_id if user else None,
            actor=Actor(user_id=user.id if user else None, label=email),
            meta=meta,
            target=user,
            changes={"reason": "inactive" if user and user.is_active is False else "credentials"},
        )
        session.commit()
        raise NotAuthenticated(INVALID_CREDENTIALS, code="invalid_credentials")
    if not _school_is_active(session, user):
        session.rollback()
        raise PermissionDenied("This school's account is suspended. Please contact support.", code="school_suspended")
    if user.mfa_enabled:
        return MfaChallenge(mfa_ticket=mfa.make_ticket(user))
    issued = _issue(session, user, meta)
    _audit_login(session, user, meta)
    session.commit()
    return issued


def login_with_mfa(
    session: Session, ticket: str, meta: RequestMeta, *, totp_code: str = "", recovery_code: str = ""
) -> IssuedSession:
    user = mfa.user_from_ticket(session, ticket)
    if user is None:
        raise NotAuthenticated("Your sign-in has expired. Please start again.", code="mfa_ticket_invalid")
    scope_for_user(session, user)
    if totp_code and mfa.verify_code(session, user, totp_code):
        method = "totp"
    elif recovery_code and mfa.use_recovery_code(session, user, recovery_code):
        method = "recovery_code"
        record(
            session,
            AuditAction.MFA_RECOVERY_CODE_USED,
            school_id=user.school_id,
            actor=actor_for(user),
            meta=meta,
            target=user,
        )
    else:
        session.rollback()
        record(
            session,
            AuditAction.LOGIN_FAILED,
            school_id=user.school_id,
            actor=actor_for(user),
            meta=meta,
            target=user,
            changes={"reason": "mfa_code"},
        )
        session.commit()
        raise NotAuthenticated("That code isn't right. Try the latest code from your app.", code="mfa_code_invalid")
    if not _school_is_active(session, user):
        session.rollback()
        raise PermissionDenied("This school's account is suspended. Please contact support.", code="school_suspended")
    issued = _issue(session, user, meta)
    _audit_login(session, user, meta, mfa=method)
    session.commit()
    return issued


def refresh(session: Session, refresh_token: str, meta: RequestMeta) -> IssuedSession:
    """
    Rotate a refresh token. A token that was already rotated away means it was
    copied, so the session it belonged to is revoked.
    """
    if not refresh_token:
        raise NotAuthenticated("Please sign in.", code="session_expired")
    token_hash = hash_token(refresh_token)
    auth_session = session.scalar(select(AuthSession).where(AuthSession.refresh_hash == token_hash).with_for_update())
    if auth_session is None:
        _detect_reuse(session, token_hash, meta)
        raise NotAuthenticated("Please sign in.", code="session_expired")
    now = datetime.now(UTC)
    user = session.get(User, auth_session.user_id)
    if user is not None:
        scope_for_user(session, user)
    if (
        auth_session.revoked_at is not None
        or auth_session.expires_at <= now
        or user is None
        or not user.is_active
        or not _school_is_active(session, user)
    ):
        session.rollback()
        raise NotAuthenticated("Please sign in.", code="session_expired")
    new_token = new_refresh_token()
    _remember_rotated(session, auth_session, token_hash)
    auth_session.refresh_hash = hash_token(new_token)
    auth_session.last_used_at = now
    auth_session.ip_address = meta.ip
    session.commit()
    access, expires_in = _access_token(user, auth_session.id)
    return IssuedSession(
        user=user,
        access_token=access,
        expires_in=expires_in,
        refresh_token=new_token,
        session_id=auth_session.id,
        branch_id=auth_session.branch_id,
    )


def _remember_rotated(session: Session, auth_session: AuthSession, old_hash: str) -> None:
    session.add(RotatedRefreshToken(token_hash=old_hash, session_id=auth_session.id))


def _detect_reuse(session: Session, token_hash: str, meta: RequestMeta) -> None:
    rotated = session.get(RotatedRefreshToken, token_hash)
    if rotated is None:
        return
    auth_session = session.get(AuthSession, rotated.session_id, with_for_update=True)
    if auth_session is None or auth_session.revoked_at is not None:
        session.rollback()
        return
    auth_session.revoked_at = datetime.now(UTC)
    auth_session.revoke_reason = "refresh_reuse"
    user = session.get(User, auth_session.user_id)
    if user is not None:
        scope_for_user(session, user)
        record(
            session,
            AuditAction.SESSION_REUSE_DETECTED,
            school_id=user.school_id,
            actor=actor_for(user),
            meta=meta,
            target=user,
            changes={"session": str(auth_session.id)},
        )
    session.commit()


def logout(session: Session, user: User, session_id: uuid.UUID, meta: RequestMeta) -> None:
    session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoke_reason="logout")
    )
    record(session, AuditAction.LOGOUT, school_id=user.school_id, actor=actor_for(user), meta=meta, target=user)
    session.commit()


def switch_branch(
    session: Session, user: User, session_id: uuid.UUID, school_id: uuid.UUID, meta: RequestMeta
) -> uuid.UUID:
    """
    Head-office staff choose which branch this device works in. Only an active school of their own group;
    anything else gets the same answer, so other schools' ids can't be probed. Recorded in the platform's trail
    (the request is still scoped to the branch being left); what they then do is in each branch's own log.
    """
    if user.group_id is None:
        raise PermissionDenied("Only head-office staff of a school group can switch branch.")
    branch = session.scalar(
        select(School.id).where(
            School.id == school_id, School.group_id == user.group_id, School.status == SchoolStatus.ACTIVE
        )
    )
    if branch is None:
        raise DomainError("That branch isn't one you can work in.", field="school_id", code="branch_invalid")
    session.execute(
        update(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id).values(branch_id=branch)
    )
    record(
        session,
        AuditAction.BRANCH_SWITCHED,
        school_id=None,
        actor=actor_for(user),
        meta=meta,
        target=user,
        changes={"branch": str(branch)},
    )
    session.commit()
    return branch


def revoke_sessions(session: Session, user_id: uuid.UUID, reason: str, *, keep: uuid.UUID | None = None) -> None:
    """Sign a user out everywhere (except `keep`). Takes effect on their next request. Caller commits."""
    stmt = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if keep is not None:
        stmt = stmt.where(AuthSession.id != keep)
    session.execute(stmt.values(revoked_at=datetime.now(UTC), revoke_reason=reason[:50]))


def _set_password(user: User, new_password: str) -> None:
    check_password_policy(new_password, email=user.email, name=user.full_name)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = datetime.now(UTC)


def change_password(
    session: Session, user: User, session_id: uuid.UUID, current: str, new: str, meta: RequestMeta
) -> None:
    if not verify_password(current, user.password_hash):
        raise DomainError("Your current password isn't right.", field="current_password", code="invalid_password")
    _set_password(user, new)
    revoke_sessions(session, user.id, "password_changed", keep=session_id)
    record(
        session, AuditAction.PASSWORD_CHANGED, school_id=user.school_id, actor=actor_for(user), meta=meta, target=user
    )
    session.commit()


# --- Invite and reset links ----------------------------------------------------------------------


def password_link_token(user: User, purpose: str) -> str:
    hours = get_settings().password_link_hours
    return encode_token(
        "password", user.id, timedelta(hours=hours), pwd=password_fingerprint(user.password_hash), purpose=purpose
    )


def send_password_link(session: Session, user: User, purpose: str) -> None:
    """Queue an invite/reset email. The link is generated when sending, so no secret sits in the outbox."""
    outbox.enqueue(session, "accounts.send_password_link", {"user_id": str(user.id), "purpose": purpose})


def request_password_reset(session: Session, email: str, meta: RequestMeta) -> None:
    """Always succeeds from the caller's point of view, so it can't be used to discover accounts."""
    user = session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or not user.is_active:
        return
    scope_for_user(session, user)
    send_password_link(session, user, "reset")
    record(
        session,
        AuditAction.PASSWORD_RESET_REQUESTED,
        school_id=user.school_id,
        actor=actor_for(user),
        meta=meta,
        target=user,
    )
    session.commit()


def complete_password_link(session: Session, token: str, new_password: str, meta: RequestMeta) -> User:
    """Set a password from an invite or reset link. Using it changes the password, which retires the link."""
    claims = decode_token(token, "password")
    user = session.get(User, uuid.UUID(claims["sub"]), with_for_update=True) if claims else None
    if (
        claims is None
        or user is None
        or not user.is_active
        or claims.get("pwd") != password_fingerprint(user.password_hash)
    ):
        raise DomainError("This link has expired or was already used. Ask for a new one.", code="link_invalid")
    scope_for_user(session, user)
    first_password = user.password_hash is None
    _set_password(user, new_password)
    revoke_sessions(session, user.id, "password_reset")
    if first_password:
        from app.modules.platform.signup import activate_if_pending  # signup builds on accounts

        activate_if_pending(session, user)
    record(
        session,
        AuditAction.PASSWORD_RESET_COMPLETED,
        school_id=user.school_id,
        actor=actor_for(user),
        meta=meta,
        target=user,
        changes={"purpose": claims.get("purpose", "")},
    )
    session.commit()
    return user


# --- Two-factor sign-in management ----------------------------------------------------------------


def start_mfa_enrolment(session: Session, user: User, password: str) -> tuple[str, str]:
    if not verify_password(password, user.password_hash):
        raise DomainError("Your password isn't right.", field="password", code="invalid_password")
    if user.mfa_enabled:
        raise Conflict("Two-factor sign-in is already on. Turn it off first to move to a new phone.")
    secret, uri = mfa.start_enrolment(user)
    session.commit()
    return secret, uri


def confirm_mfa_enrolment(
    session: Session, user: User, session_id: uuid.UUID, totp_code: str, meta: RequestMeta
) -> list[str]:
    if user.mfa_enabled:
        raise Conflict("Two-factor sign-in is already on.")
    if not user.mfa_secret or not mfa.verify_code(session, user, totp_code):
        session.rollback()
        raise DomainError(
            "That code isn't right. Try the latest code from your app.", field="totp_code", code="mfa_code_invalid"
        )
    user.mfa_enabled = True
    codes = mfa.regenerate_recovery_codes(session, user)
    # Any other device signed in before MFA was on should prove the second factor too
    revoke_sessions(session, user.id, "mfa_enabled", keep=session_id)
    record(session, AuditAction.MFA_ENABLED, school_id=user.school_id, actor=actor_for(user), meta=meta, target=user)
    session.commit()
    return codes


def disable_mfa(session: Session, user: User, password: str, totp_code: str, meta: RequestMeta) -> None:
    if not verify_password(password, user.password_hash):
        raise DomainError("Your password isn't right.", field="password", code="invalid_password")
    if not user.mfa_enabled or not mfa.verify_code(session, user, totp_code):
        session.rollback()
        raise DomainError("That code isn't right.", field="totp_code", code="mfa_code_invalid")
    mfa.disable(session, user)
    record(
        session,
        AuditAction.MFA_DISABLED,
        school_id=user.school_id,
        actor=actor_for(user),
        meta=meta,
        target=user,
        changes={"by": "self"},
    )
    session.commit()


def regenerate_recovery_codes(session: Session, user: User, totp_code: str, meta: RequestMeta) -> list[str]:
    if not user.mfa_enabled or not mfa.verify_code(session, user, totp_code):
        session.rollback()
        raise DomainError("That code isn't right.", field="totp_code", code="mfa_code_invalid")
    codes = mfa.regenerate_recovery_codes(session, user)
    record(
        session,
        AuditAction.MFA_RECOVERY_CODES_REGENERATED,
        school_id=user.school_id,
        actor=actor_for(user),
        meta=meta,
        target=user,
    )
    session.commit()
    return codes
