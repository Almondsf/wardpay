"""
Parent sign-in with a phone number and a one-time code.

1. `request_code(phone)`: if the number belongs to a guardian at any school, a
   6-digit code is texted to it. The response is the same either way, so the
   endpoint can't be used to find out which numbers are registered.
2. `verify_code(phone, code)`: at most OTP_MAX_ATTEMPTS tries, within
   OTP_MINUTES. A parent at one school is signed in straight away; a parent
   with children at several schools chooses one (`choose_school`).

Codes are stored only as a keyed hash. The code travels to the SMS task
encrypted, and is never logged.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import outbox
from app.core.config import get_settings
from app.core.crypto import blind_index, encrypt
from app.core.errors import DomainError, NotAuthenticated
from app.core.http import RequestMeta
from app.core.phone import InvalidPhone, normalize_phone
from app.core.tenancy import scope_platform_wide
from app.modules.accounts.security import decode_token, encode_token, hash_token, new_refresh_token
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record
from app.modules.schools.models import School, SchoolStatus
from app.modules.students.models import Guardian, GuardianStudent, Student

from .models import ParentOtp, ParentSession

CHOICE_LIFETIME = timedelta(minutes=10)
SEND_TASK = "parents.send_code"


def normalized(raw: str) -> tuple[str, str]:
    try:
        phone = normalize_phone(raw)
    except InvalidPhone as e:
        raise DomainError("Enter your phone number as it's registered with the school.", field="phone") from e
    return phone, blind_index(phone)


def _code_hash(phone_index: str, code: str) -> str:
    # A fixed label (the old codename): changing it would only void codes already sent
    key = hashlib.sha256(b"schoolpay.parent-otp:" + get_settings().secret_key.encode()).digest()
    return hmac.new(key, f"{phone_index}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(session: Session, raw_phone: str) -> None:
    scope_platform_wide(session)
    phone, index = normalized(raw_phone)
    known = session.scalar(
        select(Guardian.id)
        .join(School, School.id == Guardian.school_id)
        .where(Guardian.phone_index == index, School.status == SchoolStatus.ACTIVE)
        .limit(1)
    )
    if known is None:
        return  # same response as success: numbers can't be probed
    now = datetime.now(UTC)
    # A new code retires any earlier one
    session.execute(
        update(ParentOtp).where(ParentOtp.phone_index == index, ParentOtp.consumed_at.is_(None)).values(consumed_at=now)
    )
    code = f"{secrets.randbelow(10**6):06d}"
    otp = ParentOtp(
        id=uuid.uuid4(),
        phone_index=index,
        code_hash=_code_hash(index, code),
        expires_at=now + timedelta(minutes=get_settings().otp_minutes),
    )
    session.add(otp)
    outbox.enqueue(session, SEND_TASK, {"to": encrypt(phone), "code": encrypt(code)})
    session.commit()


@dataclass(frozen=True)
class SchoolChoice:
    guardian_id: uuid.UUID
    school_id: uuid.UUID
    school_name: str
    guardian_name: str
    group_id: uuid.UUID | None = None  # the school group it's a branch of
    children: tuple[str, ...] = ()  # first names, so similar school names can be told apart


@dataclass(frozen=True)
class IssuedParentSession:
    access_token: str
    expires_in: int
    refresh_token: str
    guardian_id: uuid.UUID
    school_id: uuid.UUID


@dataclass(frozen=True)
class ChooseSchool:
    ticket: str
    schools: list[SchoolChoice]


def _choices(session: Session, phone_index: str) -> list[SchoolChoice]:
    rows = session.execute(
        select(Guardian.id, School.id, School.name, Guardian.full_name, School.group_id)
        .join(School, School.id == Guardian.school_id)
        .where(Guardian.phone_index == phone_index, School.status == SchoolStatus.ACTIVE)
        .order_by(School.name)
    ).all()
    names: dict[uuid.UUID, list[str]] = {}
    for guardian_id, first_name in session.execute(
        select(GuardianStudent.guardian_id, Student.first_name)
        .join(Student, Student.id == GuardianStudent.student_id)
        .where(GuardianStudent.guardian_id.in_([row[0] for row in rows]))
        .order_by(Student.first_name)
    ).all():
        names.setdefault(guardian_id, []).append(first_name)
    return [SchoolChoice(*row, children=tuple(names.get(row[0], []))) for row in rows]


def verify_code(session: Session, raw_phone: str, code: str, meta: RequestMeta) -> IssuedParentSession | ChooseSchool:
    scope_platform_wide(session)
    _, index = normalized(raw_phone)
    otp = session.scalar(
        select(ParentOtp)
        .where(ParentOtp.phone_index == index, ParentOtp.consumed_at.is_(None))
        .order_by(ParentOtp.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if otp is None or otp.expires_at <= now:
        session.rollback()
        raise NotAuthenticated("That code has expired. Ask for a new one.", code="otp_expired")
    otp.attempts += 1
    settings = get_settings()
    if not secrets.compare_digest(otp.code_hash, _code_hash(index, (code or "").strip())):
        if otp.attempts >= settings.otp_max_attempts:
            otp.consumed_at = now
        session.commit()
        raise NotAuthenticated("That code isn't right. Check the latest text message.", code="otp_invalid")
    otp.consumed_at = now
    choices = _choices(session, index)
    if not choices:
        session.commit()
        raise NotAuthenticated("This number isn't registered with a school.", code="otp_invalid")
    # One school, or all branches of one group (switched between from the home page): no choice to make
    if len(choices) == 1 or (choices[0].group_id is not None and len({c.group_id for c in choices}) == 1):
        issued = _issue(session, choices[0], meta)
        session.commit()
        return issued
    session.commit()
    ticket = encode_token("parent_choice", otp.id, CHOICE_LIFETIME, pi=index)
    return ChooseSchool(ticket=ticket, schools=choices)


def choose_school(session: Session, ticket: str, guardian_id: uuid.UUID, meta: RequestMeta) -> IssuedParentSession:
    scope_platform_wide(session)
    claims = decode_token(ticket, "parent_choice")
    if claims is None:
        raise NotAuthenticated("Your sign-in has expired. Please start again.", code="otp_expired")
    choice = next((c for c in _choices(session, claims["pi"]) if c.guardian_id == guardian_id), None)
    if choice is None:
        raise DomainError("Choose one of your schools.", field="guardian_id")
    issued = _issue(session, choice, meta)
    session.commit()
    return issued


def access_token(guardian_id: uuid.UUID, school_id: uuid.UUID, session_id: uuid.UUID) -> tuple[str, int]:
    minutes = get_settings().access_token_minutes
    token = encode_token("parent", guardian_id, timedelta(minutes=minutes), sid=str(session_id), sch=str(school_id))
    return token, minutes * 60


def _issue(session: Session, choice: SchoolChoice, meta: RequestMeta) -> IssuedParentSession:
    refresh = new_refresh_token()
    parent_session = ParentSession(
        id=uuid.uuid4(),
        guardian_id=choice.guardian_id,
        school_id=choice.school_id,
        refresh_hash=hash_token(refresh),
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().refresh_token_days),
        ip_address=meta.ip,
        user_agent=meta.user_agent,
    )
    session.add(parent_session)
    record(
        session,
        AuditAction.PARENT_SIGNED_IN,
        school_id=choice.school_id,
        actor=Actor(user_id=None, label=f"guardian:{choice.guardian_id}"),
        meta=meta,
        changes={"guardian": choice.guardian_id},
    )
    access, expires_in = access_token(choice.guardian_id, choice.school_id, parent_session.id)
    return IssuedParentSession(access, expires_in, refresh, choice.guardian_id, choice.school_id)


def refresh(session: Session, token: str, meta: RequestMeta) -> IssuedParentSession:
    scope_platform_wide(session)
    if not token:
        raise NotAuthenticated("Please sign in.", code="session_expired")
    digest = hash_token(token)
    parent_session = session.scalar(select(ParentSession).where(ParentSession.refresh_hash == digest).with_for_update())
    if parent_session is None:
        reused = session.scalar(
            select(ParentSession)
            .where(ParentSession.previous_refresh_hash == digest, ParentSession.revoked_at.is_(None))
            .with_for_update()
        )
        if reused is not None:  # a copied cookie: end the session for everyone holding it
            reused.revoked_at, reused.revoke_reason = datetime.now(UTC), "refresh_reuse"
            session.commit()
        raise NotAuthenticated("Please sign in.", code="session_expired")
    school_active = (
        session.scalar(select(School.status).where(School.id == parent_session.school_id)) == SchoolStatus.ACTIVE
    )
    if parent_session.revoked_at is not None or parent_session.expires_at <= datetime.now(UTC) or not school_active:
        session.rollback()
        raise NotAuthenticated("Please sign in.", code="session_expired")
    new_token = new_refresh_token()
    parent_session.previous_refresh_hash, parent_session.refresh_hash = digest, hash_token(new_token)
    parent_session.last_used_at, parent_session.ip_address = datetime.now(UTC), meta.ip
    session.commit()
    access, expires_in = access_token(parent_session.guardian_id, parent_session.school_id, parent_session.id)
    return IssuedParentSession(access, expires_in, new_token, parent_session.guardian_id, parent_session.school_id)


def sign_out(session: Session, session_id: uuid.UUID) -> None:
    session.execute(
        update(ParentSession)
        .where(ParentSession.id == session_id, ParentSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoke_reason="logout")
    )
    session.commit()
