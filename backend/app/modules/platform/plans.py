"""Subscription plans: the prices schools pay. Edited by platform admins; every change is audited."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.modules.accounts.deps import Principal
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record
from app.modules.schools.models import School

from .models import Plan

PRICE_FIELDS = ("setup_fee_kobo", "per_pupil_year_kobo", "instalment_plan_fee_kobo", "plan_charge_per_payment_kobo")


@dataclass(frozen=True)
class PlanUsage:
    plan: Plan
    schools: int  # schools on this plan (the default plan also counts schools without one)


def list_plans(session: Session) -> list[PlanUsage]:
    plans = list(session.scalars(select(Plan).order_by(Plan.is_default.desc(), Plan.name)))
    counts = dict(session.execute(select(School.plan_id, func.count()).group_by(School.plan_id)).all())
    unassigned = counts.pop(None, 0)
    return [PlanUsage(p, int(counts.get(p.id, 0)) + (int(unassigned) if p.is_default else 0)) for p in plans]


def save_plan(
    session: Session,
    admin: Principal,
    plan_id: uuid.UUID | None,
    *,
    name: str,
    description: str,
    setup_fee_kobo: int,
    per_pupil_year_kobo: int,
    is_active: bool,
    instalment_plan_fee_kobo: int = 20_000,
    plan_charge_per_payment_kobo: int = 30_000,
    is_default: bool,
) -> Plan:
    """Create a plan (plan_id None) or change one. Price changes apply to bills generated afterwards."""
    if plan_id is None:
        plan = Plan(id=uuid.uuid4(), is_default=False)
        session.add(plan)
        before: dict[str, object] = {}
    else:
        found = session.get(Plan, plan_id, with_for_update=True)
        if found is None:
            raise NotFound("Plan not found.")
        plan = found
        before = {f: getattr(plan, f) for f in (*PRICE_FIELDS, "name", "is_active", "is_default")}
    if plan.is_default and (not is_active or not is_default):
        raise Conflict("Make another plan the default first.")
    plan.name, plan.description = name.strip(), description.strip()
    plan.setup_fee_kobo, plan.per_pupil_year_kobo = setup_fee_kobo, per_pupil_year_kobo
    plan.instalment_plan_fee_kobo = instalment_plan_fee_kobo
    plan.plan_charge_per_payment_kobo = plan_charge_per_payment_kobo
    plan.is_active = is_active
    if is_default and not plan.is_default and not is_active:
        raise Conflict("The default plan must be active.")
    try:  # first, so a name already taken is a clear answer (the default switch below would flush it too)
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise Conflict("There's already a plan with that name.", field="name") from e
    if is_default and not plan.is_default:
        session.execute(update(Plan).where(Plan.is_default.is_(True)).values(is_default=False))
        session.flush()
        plan.is_default = True
        session.flush()
    after = {f: getattr(plan, f) for f in (*PRICE_FIELDS, "name", "is_active", "is_default")}
    record(
        session,
        AuditAction.PLAN_SAVED,
        school_id=None,
        actor=admin.actor,
        meta=admin.meta,
        target=plan,
        changes={"before": before, "after": after},
    )
    session.commit()
    return plan


def set_school_plan(session: Session, admin: Principal, school_id: uuid.UUID, plan_id: uuid.UUID) -> School:
    school = session.get(School, school_id, with_for_update=True)
    plan = session.get(Plan, plan_id)
    if school is None:
        raise NotFound("School not found.")
    if plan is None or not plan.is_active:
        raise NotFound("Plan not found.", field="plan_id")
    before = school.plan_id
    school.plan_id = plan.id
    for target_school in (None, school.id):
        record(
            session,
            AuditAction.SCHOOL_PLAN_CHANGED,
            school_id=target_school,
            actor=admin.actor,
            meta=admin.meta,
            target=school,
            changes={"from": before, "to": plan.id, "plan": plan.name},
        )
    session.commit()
    return school
