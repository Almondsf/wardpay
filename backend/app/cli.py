"""
Operator commands.

    python -m app.cli create-platform-admin --email ops@example.com --name "Ops Person"
    python -m app.cli seed-demo [--reset] [--bare]   (development and browser tests only)
    python -m app.cli clear-platform-fee              (once: subaccounts made before WardPay stopped taking 1%)

Prints a one-time set-password link for the new admin (there's no one to
email an invite from yet). Treat the link like a password.
"""

import argparse
import sys
import uuid

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app import demo
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.tenancy import scope_platform_wide
from app.models import load_all_models
from app.modules.accounts.auth_service import normalize_email, password_link_token
from app.modules.accounts.models import Role, User
from app.modules.audit.models import AuditAction
from app.modules.audit.service import Actor, record


def create_platform_admin(email: str, name: str) -> str:
    load_all_models()
    with SessionLocal() as session:
        scope_platform_wide(session)
        try:  # the same rule the API applies at sign-in, or the admin could never log in
            email = normalize_email(TypeAdapter(EmailStr).validate_python(email))
        except ValidationError as e:
            raise SystemExit(f"Not a usable email address: {email}") from e
        if session.scalar(select(User.id).where(User.email == email)) is not None:
            raise SystemExit(f"An account with {email} already exists.")
        user = User(id=uuid.uuid4(), email=email, full_name=name.strip(), role=Role.PLATFORM_ADMIN)
        session.add(user)
        record(
            session,
            AuditAction.STAFF_INVITED,
            school_id=None,
            actor=Actor.system("cli"),
            target=user,
            changes={"email": email, "role": Role.PLATFORM_ADMIN},
        )
        session.commit()
        token = password_link_token(user, "invite")
    return f"{get_settings().frontend_url}/console/set-password#token={token}"


def clear_platform_fee() -> tuple[int, list[str]]:
    """
    Set every school's subaccount at the payment provider to keep nothing for WardPay (owner, 2026-10-27: WardPay
    earns only from its bills to schools). Safe to run again. One provider call per school, by necessity (each
    subaccount is its own record at the provider); the database is read once. Returns (cleared, failures).
    """
    from app.modules.payments.gateways import GatewayError, enabled_gateways, get_gateway
    from app.modules.payments.models import SchoolPaymentAccount

    load_all_models()
    with SessionLocal() as session:
        scope_platform_wide(session)
        accounts = session.execute(
            select(
                SchoolPaymentAccount.gateway, SchoolPaymentAccount.subaccount_code, SchoolPaymentAccount.school_id
            ).where(SchoolPaymentAccount.gateway.in_(enabled_gateways()))
        ).all()
    cleared, failed = 0, []
    for gateway, code, school_id in accounts:
        try:
            get_gateway(gateway).clear_platform_share(code)
            cleared += 1
        except GatewayError as e:
            failed.append(f"{gateway} {code} (school {school_id}): {e}")
    with SessionLocal() as session:
        scope_platform_wide(session)
        record(
            session,
            AuditAction.PLATFORM_FEE_CLEARED,
            school_id=None,
            actor=Actor.system("cli"),
            changes={"cleared": cleared, "failed": len(failed)},
        )
        session.commit()
    return cleared, failed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-platform-admin", help="Create a platform admin and print their invite link")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    seed = commands.add_parser("seed-demo", help="Create a demo school (development and browser tests only)")
    seed.add_argument("--mfa-secret", default="JBSWY3DPEHPK3PXP", help="Base32 TOTP secret for MFA users")
    seed.add_argument("--reset", action="store_true", help="Replace an existing demo school")
    seed.add_argument("--bare", action="store_true", help="Only the school and students, no bills or payments yet")
    commands.add_parser(
        "clear-platform-fee", help="Set every school's provider subaccount to give WardPay 0% (run once after deploy)"
    )
    args = parser.parse_args(argv)
    if args.command == "seed-demo":
        demo.seed_demo(args.mfa_secret, args.reset, bare=args.bare)
        sys.stdout.write(f"Demo school ready. Password for every account: {demo.DEMO_PASSWORD}\n")
    elif args.command == "create-platform-admin":
        link = create_platform_admin(args.email, args.name)
        sys.stdout.write(f"Platform admin created. Send them this one-time link to set a password:\n{link}\n")
    elif args.command == "clear-platform-fee":
        cleared, failed = clear_platform_fee()
        sys.stdout.write(f"Subaccounts set to 0% for WardPay: {cleared}\n")
        for line in failed:
            sys.stdout.write(f"  Not done, run it again: {line}\n")


if __name__ == "__main__":
    main()
