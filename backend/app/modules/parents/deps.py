"""
The signed-in parent, and the scoping of their requests.

`get_parent` checks the parent access token against a live session, then
scopes the database session to the guardian's school (a parent with children
in several branches of a school group switches between them: `branches.py`). Within that school,
`portal.require_child` limits everything to the guardian's own children.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotAuthenticated, PermissionDenied
from app.core.http import RequestMeta, request_meta
from app.core.tenancy import scope_to_school
from app.modules.accounts.security import decode_token
from app.modules.audit.service import Actor
from app.modules.schools.models import School, SchoolStatus

from .models import ParentSession

_bearer = HTTPBearer(auto_error=False, description="Parent access token from /parent/auth")


@dataclass(frozen=True)
class ParentContext:
    guardian_id: uuid.UUID
    school_id: uuid.UUID
    session_id: uuid.UUID
    meta: RequestMeta

    @property
    def actor(self) -> Actor:
        return Actor(user_id=None, label=f"guardian:{self.guardian_id}")


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None, db: Session) -> ParentContext:
    """The signed-in parent and their school. Leaves the session's tenant scope to the caller."""
    claims = decode_token(credentials.credentials, "parent") if credentials else None
    if claims is None or "sid" not in claims:
        raise NotAuthenticated("Please sign in.", code="not_authenticated")
    row = db.execute(
        select(ParentSession, School.status)
        .join(School, School.id == ParentSession.school_id)
        .where(ParentSession.id == uuid.UUID(claims["sid"]), ParentSession.guardian_id == uuid.UUID(claims["sub"]))
    ).first()
    if row is None:
        raise NotAuthenticated("Please sign in.", code="session_expired")
    parent_session, school_status = row
    if parent_session.revoked_at is not None or parent_session.expires_at <= datetime.now(UTC):
        raise NotAuthenticated("Your session has ended. Please sign in again.", code="session_expired")
    if school_status != SchoolStatus.ACTIVE:
        raise PermissionDenied("This school's account is suspended.", code="school_suspended")
    return ParentContext(
        guardian_id=parent_session.guardian_id,
        school_id=parent_session.school_id,
        session_id=parent_session.id,
        meta=request_meta(request),
    )


def get_parent(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> ParentContext:
    ctx = _authenticate(request, credentials, db)
    scope_to_school(db, ctx.school_id)
    return ctx


def get_parent_across_branches(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> ParentContext:
    """For `branches.py` only: the session is left unscoped and that module reads platform-wide, naming this
    parent's own records at their school group's branches explicitly."""
    return _authenticate(request, credentials, db)
