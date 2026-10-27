"""
Settings, read once from the environment (and `.env` in development).

Every variable is documented in `.env.example`. Defaults are the safe ones:
DEBUG is off, cookies are secure, CORS allows nothing. Development and test
loosen what they need explicitly.
"""

import os
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "production"]


def normalize_database_url(value: str) -> str:
    """Accept the common postgres:// spellings and pin the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value.removeprefix(prefix)
    if value.startswith("postgresql+psycopg://"):
        return value
    raise ValueError("Database URLs must be PostgreSQL (postgresql+psycopg://...)")


# Accepted address schemes. Checked at startup so a mistyped address (e.g. a whole `redis-cli -u ...`
# command pasted in) stops the deploy with a clear message instead of failing the first request.
REDIS_SCHEMES = ("redis://", "rediss://")
RATE_LIMIT_SCHEMES = ("memory://", "redis://", "rediss://", "redis+sentinel://", "redis+cluster://")


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Tests point WARDPAY_ENV_FILE at a missing file so a developer's .env never leaks in
        env_file=os.environ.get("WARDPAY_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Never echo a rejected value back: these hold passwords and keys, and startup errors end up in logs
        hide_input_in_errors=True,
    )

    environment: Environment = "production"
    debug: bool = False
    # The product name parents and staff see (texts, emails, receipts, the authenticator app)
    app_name: str = "WardPay"

    # Signing key for JWTs and other short-lived tokens. Rotate by deploying a new value
    # (all sessions end). Never reuse it as the field-encryption key.
    secret_key: str = Field(min_length=32)

    # Runtime role: DML only. Migrations use MIGRATION_DATABASE_URL (see app.core.migrate).
    database_url: str
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    redis_url: str = "redis://localhost:6379/0"
    # Run Celery tasks in-process (development without Redis, and tests)
    celery_task_always_eager: bool = False
    # Rate-limit counters; use the Redis URL in production so limits hold across workers
    rate_limit_storage_url: str = "memory://"
    rate_limits_enabled: bool = True

    # Fernet keys (url-safe base64, 32 bytes). The first encrypts; all decrypt (rotation).
    field_encryption_keys: Annotated[list[str], NoDecode] = Field(min_length=1)

    cors_origins: Annotated[list[str], NoDecode] = []
    frontend_url: str = "http://localhost:5175"
    # Number of reverse proxies in front of the API whose X-Forwarded-For we trust
    num_proxies: int = Field(default=0, ge=0, le=5)
    # Largest request body accepted (spreadsheet imports are the biggest legitimate uploads)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)

    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    mfa_issuer: str = "WardPay"
    # Staff who can move money (proprietors, bursars, school admins) must enrol in two-factor sign-in before
    # doing anything else. Can be turned off for local development only.
    enforce_mfa: bool = True

    # Rate limits on sign-in endpoints ("N/period"; see app.core.ratelimit)
    login_rate_per_ip: str = "20/minute"
    login_rate_per_account: str = "10/hour"
    password_reset_rate: str = "5/hour"

    # Outgoing email: console (log only), memory (tests) or smtp
    email_backend: Literal["console", "memory", "smtp"] = "console"
    email_from: str = "WardPay <no-reply@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # How long invite and password-reset links stay valid
    password_link_hours: int = Field(default=48, ge=1, le=168)

    # Payment gateways the platform can use (keys configured). Which one new checkouts go through is
    # switched at runtime by a platform admin (PUT /platform/providers/payment); until then, the first one.
    # "fake" simulates a gateway (development and tests only).
    payment_gateways: Annotated[list[Literal["paystack", "bachs", "flutterwave", "fake"]], NoDecode] = ["paystack"]
    paystack_secret_key: str = ""
    # Bank Paystack issues dedicated virtual accounts from ("wema-bank", "titan-paystack", ...)
    paystack_preferred_bank: str = "wema-bank"
    flutterwave_secret_key: str = ""
    # The "secret hash" set in the Flutterwave dashboard; sent back in the verif-hash header
    flutterwave_webhook_hash: str = ""
    # Bachs (https://docs.bachs.io): sk_sandbox_... uses the sandbox, sk_live_... the live API
    bachs_secret_key: str = ""
    # The signing secret of our webhook endpoint in the Bachs developer portal
    bachs_webhook_secret: str = ""
    # Where the gateway sends parents after a card payment (the web app verifies and shows the result)
    payment_callback_path: str = "/pay/complete"
    # A parent's family payment link (in bills and reminders) works this long, then a fresh one is made
    family_link_days: int = Field(default=120, ge=7, le=400)

    # The platform's own bank account, printed on subscription invoices so schools can pay by transfer
    platform_bank_name: str = ""
    platform_account_number: str = ""
    platform_account_name: str = ""
    # Days a school has to pay a subscription invoice
    subscription_due_days: int = Field(default=30, ge=1, le=120)
    # Shown on the public website and privacy notice for schools and parents to contact the platform
    support_email: str = ""
    # Schools can sign themselves up at /signup; limits per IP address
    signup_enabled: bool = True
    signup_rate_per_ip: str = "5/hour"
    # Signed-up schools whose proprietor never confirms are removed after this many days
    pending_school_days: int = Field(default=14, ge=1, le=90)

    # Retention (NDPA). Students who left this many years ago, and their parents if they have no other
    # children at the school, have their personal details erased; money records stay (anonymised).
    records_retention_years: int = Field(default=6, ge=1, le=20)
    # The text of messages to parents is cleared after this many days (who, when and how is kept)
    message_body_retention_days: int = Field(default=365, ge=30, le=3650)

    # Text messages: console (log only), memory (tests), or a provider: termii | africastalking. With keys for
    # both, a platform admin can switch between them at runtime (Platform → Providers); this is the default.
    sms_backend: Literal["console", "memory", "termii", "africastalking"] = "console"
    termii_api_key: str = ""
    # Registered Termii sender ID (up to 11 characters), e.g. "WardPay"
    termii_sender_id: str = "WardPay"
    # WhatsApp (Meta Cloud API): off | console (log only) | memory (tests) | cloud
    whatsapp_backend: Literal["off", "console", "memory", "cloud"] = "off"
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_language: str = "en"
    # Approved template names, as kind=name pairs (see docs/DEPLOYMENT.md for their parameters)
    whatsapp_templates: Annotated[dict[str, str], NoDecode] = {
        "receipt": "payment_receipt",
        "fee_notice": "fee_notice",
        "reminder": "fee_reminder",
    }
    # Queued messages older than this are dropped rather than sent late (e.g. after a long outage)
    message_max_age_hours: int = Field(default=24, ge=1, le=168)
    # Parents are never sent fee notices or reminders between these hours (Lagos time)
    quiet_hours_start: int = Field(default=20, ge=0, le=23)
    quiet_hours_end: int = Field(default=7, ge=0, le=23)
    # Phone calls to parents who prefer them: console (log only), memory (tests) or africastalking
    voice_backend: Literal["console", "memory", "africastalking"] = "console"
    africastalking_username: str = ""
    africastalking_api_key: str = ""
    # The Africa's Talking voice number calls come from, e.g. +2347000000000
    africastalking_voice_number: str = ""
    # Texts through Africa's Talking: its registered sender ID or short code (empty: theirs).
    # Username "sandbox" sends to Africa's Talking's sandbox instead.
    africastalking_sms_sender: str = ""
    # Calls only between these hours (Lagos time)
    call_hours_start: int = Field(default=9, ge=7, le=12)
    call_hours_end: int = Field(default=19, ge=13, le=21)
    # Parent sign-in codes
    otp_minutes: int = Field(default=5, ge=2, le=15)
    otp_max_attempts: int = Field(default=5, ge=3, le=10)
    otp_rate_per_phone: str = "3/10minutes"
    otp_rate_per_ip: str = "20/hour"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Error monitoring (Sentry). Off unless a DSN is set; events are scrubbed of personal data first.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    # Shown in error reports, e.g. the git commit being deployed
    release: str = ""

    @field_validator("whatsapp_templates", mode="before")
    @classmethod
    def _templates(cls, value: object) -> object:
        if isinstance(value, str):  # "receipt=payment_receipt,reminder=fee_reminder"
            pairs = [item.split("=", 1) for item in value.split(",") if item.strip()]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError("Use kind=template_name pairs, separated by commas")
            value = {k.strip(): v.strip() for k, v in pairs}
        if isinstance(value, dict) and not set(value) <= {"receipt", "fee_notice", "reminder"}:
            raise ValueError("WHATSAPP_TEMPLATES kinds are receipt, fee_notice and reminder")
        return value

    @field_validator("payment_gateways", mode="before")
    @classmethod
    def _gateways_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("cors_origins", "field_encryption_keys", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("database_url")
    @classmethod
    def _postgres_driver(cls, value: str) -> str:
        return normalize_database_url(value.strip())

    @field_validator("redis_url")
    @classmethod
    def _redis_address(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(REDIS_SCHEMES):
            raise ValueError("REDIS_URL must be just the address, starting with redis:// or rediss://")
        return value

    @field_validator("rate_limit_storage_url")
    @classmethod
    def _rate_limit_address(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(RATE_LIMIT_SCHEMES):
            raise ValueError(
                "RATE_LIMIT_STORAGE_URL must be just the address, starting with " + ", ".join(RATE_LIMIT_SCHEMES)
            )
        return value

    @model_validator(mode="after")
    def _production_is_strict(self) -> "Settings":
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be off in production")
            if self.celery_task_always_eager:
                raise ValueError("CELERY_TASK_ALWAYS_EAGER must be off in production")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS can't be '*' in production")
            if self.rate_limit_storage_url.startswith("memory"):
                raise ValueError("RATE_LIMIT_STORAGE_URL must be shared storage (Redis) in production")
            if not self.enforce_mfa:
                raise ValueError("ENFORCE_MFA can't be turned off in production")
            if "fake" in self.payment_gateways:
                raise ValueError("The fake payment gateway can't be enabled in production")
            if "paystack" in self.payment_gateways and not self.paystack_secret_key:
                raise ValueError("PAYSTACK_SECRET_KEY is required when paystack is enabled")
            if "flutterwave" in self.payment_gateways and not (
                self.flutterwave_secret_key and self.flutterwave_webhook_hash
            ):
                raise ValueError("FLUTTERWAVE_SECRET_KEY and FLUTTERWAVE_WEBHOOK_HASH are required for flutterwave")
            if "bachs" in self.payment_gateways and not (
                self.bachs_secret_key.startswith("sk_live_") and self.bachs_webhook_secret
            ):
                raise ValueError("A live BACHS_SECRET_KEY and BACHS_WEBHOOK_SECRET are required for bachs")
            if self.whatsapp_backend == "memory":
                raise ValueError("WHATSAPP_BACKEND=memory is for tests only")
            if self.whatsapp_backend == "cloud" and not (self.whatsapp_token and self.whatsapp_phone_number_id):
                raise ValueError("WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required for WhatsApp")
            if self.sms_backend == "memory":
                raise ValueError("SMS_BACKEND=memory is for tests only")
            if self.voice_backend == "memory":  # it keeps every call in a list, which only grows
                raise ValueError("VOICE_BACKEND=memory is for tests only")
            if self.sms_backend == "termii" and not self.termii_api_key:
                raise ValueError("TERMII_API_KEY is required when SMS_BACKEND=termii")
            if self.sms_backend == "africastalking" and not (
                self.africastalking_username and self.africastalking_api_key
            ):
                raise ValueError("AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY are required for its texts")
            if self.email_backend == "memory":
                raise ValueError("EMAIL_BACKEND=memory is for tests only")
            if self.email_backend == "smtp" and not self.smtp_host:
                raise ValueError("SMTP_HOST is required when EMAIL_BACKEND=smtp")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def secure_cookies(self) -> bool:
        # Browsers drop Secure cookies on plain-http localhost, so only development relaxes it
        return self.environment != "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
