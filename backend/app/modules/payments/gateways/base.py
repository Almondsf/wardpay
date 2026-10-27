"""
The payment gateway interface. Paystack, Bachs, Flutterwave and the fake gateway
all implement it, so the rest of the system never knows which one a school uses.

The platform never holds school money. Each school has a gateway
**subaccount** pointing at its own bank account. Card payments and transfers
into students' virtual accounts are split by the gateway: the school's share
settles to the school, and the platform fee settles to us.

Rules for callers:
- Never trust a webhook body for the outcome: `verify()` asks the gateway.
- Amounts are integer kobo here; adapters convert to each gateway's units.
"""

import enum
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from app.core.errors import DomainError, ServiceUnavailable


class GatewayCode(enum.StrEnum):
    PAYSTACK = "paystack"
    FLUTTERWAVE = "flutterwave"
    BACHS = "bachs"
    FAKE = "fake"


class GatewayError(ServiceUnavailable):
    """The gateway couldn't be reached or refused the request. Retry later or check the details."""

    code = "gateway_error"


class GatewayRejected(DomainError):
    """The gateway refused the request because of what was sent (e.g. an invalid account number)."""

    code = "gateway_rejected"


class InvalidSignature(Exception):
    """A webhook whose signature doesn't verify. Never processed."""


@dataclass(frozen=True)
class Bank:
    code: str
    name: str


@dataclass(frozen=True)
class ResolvedAccount:
    account_number: str
    account_name: str


@dataclass(frozen=True)
class Subaccount:
    code: str


@dataclass(frozen=True)
class CustomerDetails:
    """Who a virtual account is for: the student, contactable through their guardian."""

    reference: str  # our stable id for the customer (the student id)
    first_name: str
    last_name: str
    email: str
    phone: str


@dataclass(frozen=True)
class IssuedVirtualAccount:
    account_number: str
    account_name: str
    bank_name: str
    provider_reference: str


@dataclass(frozen=True)
class Checkout:
    authorization_url: str
    provider_reference: str = ""  # the gateway's own id for the checkout, when it differs from ours


class TransactionStatus(enum.StrEnum):
    SUCCESS = "success"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class Verification:
    """The gateway's own account of a transaction, normalised."""

    reference: str
    status: TransactionStatus
    amount_kobo: int
    currency: str
    channel: str  # "dedicated_nuban" / "bank_transfer" / "card" ...
    # For transfers into a virtual account: which account received it
    receiver_account_number: str | None = None
    paid_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settlement:
    """One payout from the gateway into the school's bank account."""

    id: str
    status: str  # "success" once paid out; "pending" / "processing" before that
    settled_on: date | None
    gross_kobo: int  # what parents paid in the transactions it covers
    charges_kobo: int  # what came off before it reached the bank (gateway fees and any split share)
    paid_out_kobo: int  # what the bank received


@dataclass(frozen=True)
class SettledTransaction:
    reference: str  # the gateway's transaction reference (Payment.gateway_reference)
    amount_kobo: int
    fees_kobo: int


class SettlementSource(Protocol):
    """Gateways that can say what they paid out, and which transactions each payout covered."""

    def list_settlements(self, *, subaccount_code: str, since: date) -> list[Settlement]: ...

    def settlement_transactions(self, settlement_id: str) -> list[SettledTransaction]: ...


def parse_paid_at(value: str | None) -> datetime:
    """A gateway's timestamp ("2026-10-18T09:15:00.000Z"), or now if it's missing or unreadable."""
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


class WebhookKind(enum.StrEnum):
    CHARGE = "charge"  # money arrived (card or transfer): verify it, then record it
    IGNORED = "ignored"  # an event we don't act on


@dataclass(frozen=True)
class WebhookEvent:
    kind: WebhookKind
    event_type: str
    # Unique per real-world event; duplicate deliveries share it
    event_key: str
    reference: str | None = None
    receiver_account_number: str | None = None
    amount_kobo: int | None = None
    provider_payment_id: str | None = None  # the gateway's id for the payment, to verify it by
    payload: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(Protocol):
    code: GatewayCode
    supports_virtual_accounts: bool  # can give each student their own permanent account number

    def list_banks(self) -> list[Bank]: ...

    def resolve_account(self, *, bank_code: str, account_number: str) -> ResolvedAccount: ...

    def create_subaccount(self, *, business_name: str, bank_code: str, account_number: str) -> Subaccount: ...

    def clear_platform_share(self, subaccount_code: str) -> None:
        """Make sure the provider keeps nothing of the school's payments for the platform (owner, 2026-10-27:
        WardPay earns only from its bills to schools). For subaccounts made before that."""
        ...

    def create_virtual_account(self, *, customer: CustomerDetails, subaccount_code: str) -> IssuedVirtualAccount: ...

    def initialize_payment(
        self,
        *,
        reference: str,
        amount_kobo: int,
        email: str,
        callback_url: str,
        subaccount_code: str | None,
        description: str,
    ) -> Checkout:
        """A hosted checkout. With a subaccount the money settles to that school; with None (the
        school paying its own subscription) it settles to the platform's own balance."""
        ...

    def verify(
        self, reference: str, *, provider_reference: str | None = None, payment_id: str | None = None
    ) -> Verification:
        """Ask the gateway what happened. Gateways that track checkouts by their own ids use the hints."""
        ...

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        """Check the signature (raise InvalidSignature) and normalise the event."""
        ...
