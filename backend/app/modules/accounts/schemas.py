import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from .models import Role

Password = Annotated[str, StringConstraints(min_length=1, max_length=128)]
NewPassword = Annotated[str, StringConstraints(min_length=1, max_length=128)]
TotpCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=8)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=150)]
StaffRole = Literal[Role.PROPRIETOR, Role.BURSAR, Role.SCHOOL_ADMIN]


class LoginIn(BaseModel):
    email: EmailStr
    password: Password


class MfaLoginIn(BaseModel):
    mfa_ticket: Annotated[str, StringConstraints(max_length=2000)]
    totp_code: TotpCode | None = None
    recovery_code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None


class SchoolSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str


class GroupSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class BranchIn(BaseModel):
    school_id: uuid.UUID


class Me(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    # The school they're working in (for head-office staff: the branch chosen; None until they choose one)
    school: SchoolSummary | None
    # Head-office staff only: their school group and the branches they can switch between
    group: GroupSummary | None = None
    branches: list[SchoolSummary] = []
    capabilities: list[str]
    mfa_enabled: bool
    # True: the user must turn on two-factor sign-in before anything else works
    mfa_enrolment_required: bool


class SignedIn(BaseModel):
    status: Literal["signed_in"] = "signed_in"
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires")
    user: Me


class MfaRequired(BaseModel):
    status: Literal["mfa_required"] = "mfa_required"
    mfa_ticket: str = Field(description="Exchange with a code at /auth/login/mfa within 5 minutes")


LoginResult = Annotated[SignedIn | MfaRequired, Field(discriminator="status")]


class PasswordChangeIn(BaseModel):
    current_password: Password
    new_password: NewPassword


class PasswordForgotIn(BaseModel):
    email: EmailStr


class PasswordSetIn(BaseModel):
    token: Annotated[str, StringConstraints(max_length=2000)]
    new_password: NewPassword


class MfaSetupIn(BaseModel):
    password: Password


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class MfaCodeIn(BaseModel):
    totp_code: TotpCode


class MfaDisableIn(BaseModel):
    password: Password
    totp_code: TotpCode


class RecoveryCodes(BaseModel):
    recovery_codes: list[str] = Field(description="Shown once. Each works one time.")


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    mfa_enabled: bool
    invite_pending: bool
    last_login_at: datetime | None
    created_at: datetime


class StaffInviteIn(BaseModel):
    email: EmailStr
    full_name: Name
    role: StaffRole


class StaffUpdateIn(BaseModel):
    full_name: Name | None = None
    role: StaffRole | None = None
