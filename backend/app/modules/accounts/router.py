import uuid

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ERROR_RESPONSES
from app.core.http import RequestMeta, request_meta
from app.core.pagination import Page, PageParams, page_params, paginate
from app.core.ratelimit import get_limiter
from app.modules.schools.models import School, SchoolGroup

from . import auth_service, staff_service
from .deps import Principal, StaffContext, get_principal, require
from .models import User
from .permissions import ROLE_CAPABILITIES, Capability
from .schemas import (
    BranchIn,
    GroupSummary,
    LoginIn,
    LoginResult,
    Me,
    MfaCodeIn,
    MfaDisableIn,
    MfaLoginIn,
    MfaRequired,
    MfaSetupIn,
    MfaSetupOut,
    PasswordChangeIn,
    PasswordForgotIn,
    PasswordSetIn,
    RecoveryCodes,
    SchoolSummary,
    SignedIn,
    StaffInviteIn,
    StaffOut,
    StaffUpdateIn,
)
from .security import hash_token

REFRESH_COOKIE = "sp_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

auth_router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)
staff_router = APIRouter(prefix="/staff", tags=["staff"], responses=ERROR_RESPONSES)


def _me(db: Session, user: User, branch_id: uuid.UUID | None = None) -> Me:
    group, branches = None, []
    if user.group_id is not None:
        group = db.get(SchoolGroup, user.group_id)
        branches = auth_service.group_branches(db, user.group_id)
        school = next((b for b in branches if b.id == branch_id), None)
    else:
        school = db.get(School, user.school_id) if user.school_id else None
    return Me(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        school=SchoolSummary.model_validate(school) if school else None,
        group=GroupSummary.model_validate(group) if group else None,
        branches=[SchoolSummary.model_validate(b) for b in branches],
        capabilities=sorted(c.value for c in ROLE_CAPABILITIES[user.role]),
        mfa_enabled=user.mfa_enabled,
        mfa_enrolment_required=auth_service.mfa_enrolment_required(user),
    )


def _signed_in(db: Session, response: Response, issued: auth_service.IssuedSession) -> SignedIn:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=settings.refresh_token_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
    )
    return SignedIn(
        access_token=issued.access_token, expires_in=issued.expires_in, user=_me(db, issued.user, issued.branch_id)
    )


def _clear_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True, secure=settings.secure_cookies, samesite="strict"
    )


# --- Sign-in -------------------------------------------------------------------------------------


@auth_router.post("/login", response_model=LoginResult, summary="Sign in with email and password")
def login(
    body: LoginIn, response: Response, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> SignedIn | MfaRequired:
    settings, limiter = get_settings(), get_limiter()
    limiter.hit("login:ip", settings.login_rate_per_ip, meta.ip)
    limiter.hit("login:account", settings.login_rate_per_account, auth_service.normalize_email(body.email))
    result = auth_service.login(db, body.email, body.password, meta)
    if isinstance(result, auth_service.MfaChallenge):
        return MfaRequired(mfa_ticket=result.mfa_ticket)
    return _signed_in(db, response, result)


@auth_router.post("/login/mfa", response_model=SignedIn, summary="Finish sign-in with a two-factor code")
def login_mfa(
    body: MfaLoginIn, response: Response, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> SignedIn:
    settings, limiter = get_settings(), get_limiter()
    limiter.hit("login:ip", settings.login_rate_per_ip, meta.ip)
    limiter.hit("login:mfa-ticket", "5/5minutes", hash_token(body.mfa_ticket))
    issued = auth_service.login_with_mfa(
        db, body.mfa_ticket, meta, totp_code=body.totp_code or "", recovery_code=body.recovery_code or ""
    )
    return _signed_in(db, response, issued)


@auth_router.post(
    "/refresh", response_model=SignedIn, summary="New access token from the refresh cookie (rotates the cookie)"
)
def refresh(
    response: Response,
    meta: RequestMeta = Depends(request_meta),
    db: Session = Depends(get_db),
    sp_refresh: str | None = Cookie(default=None, include_in_schema=False),
) -> SignedIn:
    get_limiter().hit("refresh:ip", "60/minute", meta.ip)
    try:
        issued = auth_service.refresh(db, sp_refresh or "", meta)
    except Exception:
        _clear_cookie(response)
        raise
    return _signed_in(db, response, issued)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out this device")
def logout(response: Response, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> None:
    auth_service.logout(db, principal.user, principal.session_id, principal.meta)
    _clear_cookie(response)


@auth_router.get("/me", response_model=Me, summary="The signed-in user, their school and capabilities")
def me(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> Me:
    return _me(db, principal.user, principal.acting_school_id)


@auth_router.post("/branch", response_model=Me, summary="Head-office staff: choose the branch this device works in")
def switch_branch(body: BranchIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> Me:
    get_limiter().hit("branch:user", "60/minute", str(principal.user.id))
    branch = auth_service.switch_branch(db, principal.user, principal.session_id, body.school_id, principal.meta)
    return _me(db, principal.user, branch)


# --- Passwords -----------------------------------------------------------------------------------


@auth_router.post(
    "/password/change", status_code=status.HTTP_204_NO_CONTENT, summary="Change password (signs out other devices)"
)
def change_password(
    body: PasswordChangeIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> None:
    get_limiter().hit("password-change:user", "10/hour", str(principal.user.id))
    auth_service.change_password(
        db, principal.user, principal.session_id, body.current_password, body.new_password, principal.meta
    )


@auth_router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password-reset link (always accepted, so emails can't be probed)",
)
def forgot_password(
    body: PasswordForgotIn, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)
) -> dict[str, str]:
    settings, limiter = get_settings(), get_limiter()
    limiter.hit("password-reset:ip", settings.password_reset_rate, meta.ip)
    limiter.hit("password-reset:account", settings.password_reset_rate, auth_service.normalize_email(body.email))
    auth_service.request_password_reset(db, body.email, meta)
    return {"detail": "If that email has an account, a reset link is on its way."}


@auth_router.post(
    "/password/set", status_code=status.HTTP_204_NO_CONTENT, summary="Set a password from an invite or reset link"
)
def set_password(body: PasswordSetIn, meta: RequestMeta = Depends(request_meta), db: Session = Depends(get_db)) -> None:
    get_limiter().hit("password-set:ip", "10/hour", meta.ip)
    auth_service.complete_password_link(db, body.token, body.new_password, meta)


# --- Two-factor sign-in --------------------------------------------------------------------------


@auth_router.post("/mfa/setup", response_model=MfaSetupOut, summary="Start enrolling an authenticator app")
def mfa_setup(
    body: MfaSetupIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> MfaSetupOut:
    get_limiter().hit("mfa:user", "10/15minutes", str(principal.user.id))
    secret, uri = auth_service.start_mfa_enrolment(db, principal.user, body.password)
    return MfaSetupOut(secret=secret, otpauth_uri=uri)


@auth_router.post(
    "/mfa/confirm", response_model=RecoveryCodes, summary="Confirm enrolment with a code; returns recovery codes once"
)
def mfa_confirm(
    body: MfaCodeIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> RecoveryCodes:
    get_limiter().hit("mfa:user", "10/15minutes", str(principal.user.id))
    codes = auth_service.confirm_mfa_enrolment(db, principal.user, principal.session_id, body.totp_code, principal.meta)
    return RecoveryCodes(recovery_codes=codes)


@auth_router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT, summary="Turn off two-factor sign-in")
def mfa_disable(
    body: MfaDisableIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> None:
    get_limiter().hit("mfa:user", "10/15minutes", str(principal.user.id))
    auth_service.disable_mfa(db, principal.user, body.password, body.totp_code, principal.meta)


@auth_router.post("/mfa/recovery-codes", response_model=RecoveryCodes, summary="Replace all recovery codes")
def mfa_recovery_codes(
    body: MfaCodeIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> RecoveryCodes:
    get_limiter().hit("mfa:user", "10/15minutes", str(principal.user.id))
    return RecoveryCodes(
        recovery_codes=auth_service.regenerate_recovery_codes(db, principal.user, body.totp_code, principal.meta)
    )


# --- Staff ---------------------------------------------------------------------------------------


def _staff_out(user: User) -> StaffOut:
    return StaffOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
        invite_pending=user.password_hash is None,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@staff_router.get("", response_model=Page[StaffOut], summary="The school's staff accounts")
def list_staff(
    ctx: StaffContext = Depends(require(Capability.STAFF_VIEW)),
    db: Session = Depends(get_db),
    params: PageParams = Depends(page_params),
) -> Page[StaffOut]:
    rows, total = paginate(db, staff_service.list_staff(ctx.school_id), params)
    return Page(items=[_staff_out(u) for u in rows], total=total, limit=params.limit, offset=params.offset)


@staff_router.post(
    "", response_model=StaffOut, status_code=status.HTTP_201_CREATED, summary="Invite a staff member by email"
)
def invite_staff(
    body: StaffInviteIn, ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)), db: Session = Depends(get_db)
) -> StaffOut:
    get_limiter().hit("staff-invite:school", "50/hour", str(ctx.school_id))
    return _staff_out(staff_service.invite(db, ctx, email=body.email, full_name=body.full_name, role=body.role))


@staff_router.patch("/{user_id}", response_model=StaffOut, summary="Rename a staff member or change their role")
def update_staff(
    user_id: uuid.UUID,
    body: StaffUpdateIn,
    ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)),
    db: Session = Depends(get_db),
) -> StaffOut:
    return _staff_out(staff_service.update(db, ctx, user_id, full_name=body.full_name, role=body.role))


@staff_router.post("/{user_id}/deactivate", response_model=StaffOut, summary="Block sign-in and end their sessions")
def deactivate_staff(
    user_id: uuid.UUID, ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)), db: Session = Depends(get_db)
) -> StaffOut:
    return _staff_out(staff_service.deactivate(db, ctx, user_id))


@staff_router.post("/{user_id}/reactivate", response_model=StaffOut, summary="Allow sign-in again")
def reactivate_staff(
    user_id: uuid.UUID, ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)), db: Session = Depends(get_db)
) -> StaffOut:
    return _staff_out(staff_service.reactivate(db, ctx, user_id))


@staff_router.post(
    "/{user_id}/reset-mfa", response_model=StaffOut, summary="Turn off someone's two-factor sign-in (lost phone)"
)
def reset_staff_mfa(
    user_id: uuid.UUID, ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)), db: Session = Depends(get_db)
) -> StaffOut:
    return _staff_out(staff_service.reset_mfa(db, ctx, user_id))


@staff_router.post("/{user_id}/resend-invite", response_model=StaffOut, summary="Email the invite link again")
def resend_staff_invite(
    user_id: uuid.UUID, ctx: StaffContext = Depends(require(Capability.STAFF_MANAGE)), db: Session = Depends(get_db)
) -> StaffOut:
    get_limiter().hit("staff-invite:school", "50/hour", str(ctx.school_id))
    return _staff_out(staff_service.resend_invite(db, ctx, user_id))
