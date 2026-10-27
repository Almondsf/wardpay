"""Background tasks for the platform side. Registered with Celery in app.workers.tasks."""

import uuid

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.email import OutgoingEmail, deliver
from app.core.money import format_naira
from app.core.tenancy import scope_platform_wide
from app.modules.accounts.models import Role, User
from app.modules.schools.models import School

from . import billing


def send_subscription_invoice(invoice_id: str) -> int:
    """Email a subscription bill to the school's active proprietors. Returns how many were emailed."""
    settings = get_settings()
    with SessionLocal() as session:
        scope_platform_wide(session)
        try:
            row, lines, _ = billing.invoice_detail(session, uuid.UUID(invoice_id))
        except Exception:  # voided or gone since it was queued: nothing to send
            return 0
        if row.voided:
            return 0
        recipients = list(
            session.scalars(
                select(User.email).where(
                    # the branch's proprietors, and its group's owner (who usually pays)
                    or_(
                        User.school_id == row.school_id,
                        User.group_id == select(School.group_id).where(School.id == row.school_id).scalar_subquery(),
                    ),
                    User.role == Role.PROPRIETOR,
                    User.is_active.is_(True),
                    User.password_hash.is_not(None),
                )
            )
        )
    charges = "\n".join(
        f"  {ln.description}: {ln.quantity} x {format_naira(ln.unit_kobo)} = {format_naira(ln.amount_kobo)}"
        for ln in lines
    )
    pay = (
        f"Please pay by bank transfer to {settings.platform_account_name}, {settings.platform_account_number} "
        f"({settings.platform_bank_name}), quoting invoice {row.number}."
        if settings.platform_account_number
        else "Reply to this email for our bank details."
    )
    body = (
        f"Hello,\n\nHere is {row.school_name}'s {settings.app_name} invoice {row.number} for {row.period}.\n\n"
        f"{charges}\n\n  Total: {format_naira(row.total_kobo)}\n  Due by: {row.due_date:%d %B %Y}\n\n{pay}\n\n"
        f"You can see and download it any time under Subscription in your console: "
        f"{settings.frontend_url}/console/subscription\n\nThank you for using {settings.app_name}.\n"
    )
    for email in recipients:
        deliver(OutgoingEmail(to=email, subject=f"{settings.app_name} invoice {row.number} ({row.period})", body=body))
    return len(recipients)


def signup_existing_account(user_id: str) -> None:
    """Someone signed up with an email that already has an account: tell its owner, don't create another."""
    settings = get_settings()
    with SessionLocal() as session:
        scope_platform_wide(session)
        user = session.get(User, uuid.UUID(user_id))
        if user is None or not user.is_active:
            return
        to, name = user.email, user.full_name
    deliver(
        OutgoingEmail(
            to=to,
            subject=f"You already have a {settings.app_name} account",
            body=(
                f"Hello {name},\n\nSomeone (hopefully you) tried to sign a school up to {settings.app_name} with "
                f"this email address, which already has an account. Sign in here instead:\n\n"
                f'{settings.frontend_url}/console/login\n\nForgot your password? Use "Forgot password" on that '
                "page. If this wasn't you, you can ignore this email.\n"
            ),
        )
    )
