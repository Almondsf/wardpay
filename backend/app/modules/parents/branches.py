"""
Parents with children in several branches of one school group (owner, 2026-10-27, school groups step 3).

Each branch is its own school, so a parent has one guardian record per branch (same phone number). Here they
are tied together: sign in once, see what's owed at every branch on the home page, and switch between
branches without a new code. Paying stays per branch, so money always reaches that branch's own bank account
(the group chooses whether branches share one account or each have their own; either works).

Who counts as "the same parent": a guardian record with the same phone (blind index) at an active school of
the **same group**. A number on file at an unrelated school is never shown or reachable from here. These reads
use a platform-wide session and name the branches and guardians explicitly.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.tenancy import scope_platform_wide
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.fees.invoicing import totals_subquery
from app.modules.fees.ledger import paid_subquery
from app.modules.fees.models import Invoice
from app.modules.schools.models import School, SchoolStatus
from app.modules.students.models import Guardian, GuardianStudent, Student, StudentStatus

from .auth_service import IssuedParentSession, access_token
from .deps import ParentContext
from .models import ParentSession


@dataclass(frozen=True)
class Branch:
    guardian_id: uuid.UUID
    school_id: uuid.UUID
    school_name: str
    children: tuple[str, ...]  # first names of current pupils
    owed_kobo: int
    current: bool  # the branch this device is looking at


def branches(session: Session, ctx: ParentContext) -> list[Branch]:
    """This parent's records at every active branch of the group (just this school if it isn't in a group)."""
    scope_platform_wide(session)
    me = select(Guardian.phone_index).where(Guardian.id == ctx.guardian_id).scalar_subquery()
    group = select(School.group_id).where(School.id == ctx.school_id).scalar_subquery()
    rows = session.execute(
        select(Guardian.id, School.id, School.name)
        .join(School, School.id == Guardian.school_id)
        .where(
            Guardian.phone_index == me,
            School.status == SchoolStatus.ACTIVE,
            School.group_id.is_not(None),
            School.group_id == group,
        )
        .order_by(School.name)
    ).all()
    if not rows:
        return []
    ids = [guardian_id for guardian_id, _, _ in rows]

    names: dict[uuid.UUID, list[str]] = {}
    for guardian_id, first_name in session.execute(
        select(GuardianStudent.guardian_id, Student.first_name)
        .join(Student, Student.id == GuardianStudent.student_id)
        .where(GuardianStudent.guardian_id.in_(ids), Student.status == StudentStatus.ACTIVE)
        .order_by(Student.first_name)
    ):
        names.setdefault(guardian_id, []).append(first_name)

    # What's owed for each guardian's children: the ledger's own totals, for just these bills
    children = select(GuardianStudent.student_id).where(GuardianStudent.guardian_id.in_(ids))
    bills = select(Invoice.id).where(Invoice.student_id.in_(children), Invoice.voided_at.is_(None))
    totals, paid = totals_subquery(bills), paid_subquery(bills)
    balance = func.coalesce(totals.c.total_kobo, 0) - func.coalesce(paid.c.paid_kobo, 0)
    owed = {
        guardian_id: int(kobo)
        for guardian_id, kobo in session.execute(
            select(GuardianStudent.guardian_id, func.sum(case((balance > 0, balance), else_=0)))
            .join(Invoice, Invoice.student_id == GuardianStudent.student_id)
            .outerjoin(totals, totals.c.invoice_id == Invoice.id)
            .outerjoin(paid, paid.c.invoice_id == Invoice.id)
            .where(GuardianStudent.guardian_id.in_(ids), Invoice.voided_at.is_(None))
            .group_by(GuardianStudent.guardian_id)
        )
    }
    return [
        Branch(gid, sid, name, tuple(names.get(gid, [])), owed.get(gid, 0), gid == ctx.guardian_id)
        for gid, sid, name in rows
    ]


def switch(session: Session, ctx: ParentContext, guardian_id: uuid.UUID) -> IssuedParentSession:
    """
    Look at another branch on this device. Only this parent's own record at a branch of the same group;
    anything else gets the same answer. The session keeps its refresh cookie; a new access token is issued.
    """
    target = next((b for b in branches(session, ctx) if b.guardian_id == guardian_id and not b.current), None)
    if target is None:
        raise DomainError("Choose one of your children's branches.", field="guardian_id", code="branch_invalid")
    session.execute(
        update(ParentSession)
        .where(ParentSession.id == ctx.session_id)
        .values(guardian_id=target.guardian_id, school_id=target.school_id)
    )
    record(
        session,
        AuditAction.PARENT_SWITCHED_BRANCH,
        school_id=target.school_id,
        actor=ctx.actor,
        meta=ctx.meta,
        changes={"from_school": ctx.school_id, "guardian": target.guardian_id},
    )
    session.commit()
    token, expires_in = access_token(target.guardian_id, target.school_id, ctx.session_id)
    return IssuedParentSession(token, expires_in, "", target.guardian_id, target.school_id)
