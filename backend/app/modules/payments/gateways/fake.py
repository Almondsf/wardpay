"""
A stand-in gateway for development and tests. Never enabled in production
(settings refuse it).

It behaves like a real one: it issues virtual account numbers, checkout links
and signed webhooks, and `verify()` reports what "really" happened. Its state
lives in this process, which is fine for tests and for development with eager
background tasks.

    fake = FakeGateway.shared()
    event = fake.simulate_transfer(account_number, 5_000_000)   # a parent pays into a virtual account
    fake.complete_checkout(reference)                            # a parent pays by card
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from .base import (
    Bank,
    Checkout,
    CustomerDetails,
    GatewayCode,
    GatewayError,
    GatewayRejected,
    InvalidSignature,
    IssuedVirtualAccount,
    ResolvedAccount,
    SettledTransaction,
    Settlement,
    Subaccount,
    TransactionStatus,
    Verification,
    WebhookEvent,
    WebhookKind,
)

WEBHOOK_SECRET = b"fake-gateway-webhook-secret"
SIGNATURE_HEADER = "x-fake-signature"


@dataclass
class _Transaction:
    reference: str
    amount_kobo: int
    status: TransactionStatus
    channel: str
    receiver_account_number: str | None = None
    paid_at: str | None = None


@dataclass
class _State:
    transactions: dict[str, _Transaction] = field(default_factory=dict)
    accounts: dict[str, CustomerDetails] = field(default_factory=dict)
    # subaccount code -> payouts, each with the transactions it covered
    settlements: dict[str, list[tuple[Settlement, list[SettledTransaction]]]] = field(default_factory=dict)
    # Set to make the next calls fail like an outage
    unavailable: bool = False


class FakeGateway:
    code = GatewayCode.FAKE
    supports_virtual_accounts = True
    _shared: ClassVar["FakeGateway | None"] = None

    def __init__(self) -> None:
        self.state = _State()

    @classmethod
    def shared(cls) -> "FakeGateway":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reset(cls) -> None:
        cls._shared = None

    def _check(self) -> None:
        if self.state.unavailable:
            raise GatewayError("The fake gateway is pretending to be down.")

    # --- The interface ---------------------------------------------------------------------------

    def list_banks(self) -> list[Bank]:
        return [Bank("044", "Access Bank"), Bank("058", "GTBank"), Bank("033", "UBA"), Bank("057", "Zenith Bank")]

    def resolve_account(self, *, bank_code: str, account_number: str) -> ResolvedAccount:
        self._check()
        if not (account_number.isdigit() and len(account_number) == 10):
            raise GatewayRejected("Fake gateway: that isn't a 10-digit account number.")
        return ResolvedAccount(account_number=account_number, account_name="DEMO SCHOOL LTD")

    def create_subaccount(self, *, business_name: str, bank_code: str, account_number: str) -> Subaccount:
        self._check()
        return Subaccount(code=f"FAKE_SUB_{secrets.token_hex(4).upper()}")

    def clear_platform_share(self, subaccount_code: str) -> None:
        self._check()

    def create_virtual_account(self, *, customer: CustomerDetails, subaccount_code: str) -> IssuedVirtualAccount:
        self._check()
        number = "9" + "".join(secrets.choice("0123456789") for _ in range(9))
        self.state.accounts[number] = customer
        return IssuedVirtualAccount(
            account_number=number,
            account_name=f"WARDPAY/{customer.last_name.upper()} {customer.first_name.upper()}",
            bank_name="Fake Bank",
            provider_reference=f"FAKE_CUS_{customer.reference[:8]}",
        )

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
        self._check()
        self.state.transactions[reference] = _Transaction(reference, amount_kobo, TransactionStatus.PENDING, "card")
        return Checkout(authorization_url=f"https://checkout.fake/{reference}")

    def verify(
        self, reference: str, *, provider_reference: str | None = None, payment_id: str | None = None
    ) -> Verification:
        self._check()
        txn = self.state.transactions.get(reference)
        if txn is None:
            return Verification(reference, TransactionStatus.FAILED, 0, "NGN", "")
        return Verification(
            reference=reference,
            status=txn.status,
            amount_kobo=txn.amount_kobo,
            currency="NGN",
            channel=txn.channel,
            receiver_account_number=txn.receiver_account_number,
            paid_at=txn.paid_at,
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        expected = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get(SIGNATURE_HEADER, ""), expected):
            raise InvalidSignature
        event = json.loads(body)
        data = event.get("data") or {}
        if event.get("event") != "charge.success":
            return WebhookEvent(WebhookKind.IGNORED, str(event.get("event")), f"other:{data.get('reference')}")
        return WebhookEvent(
            kind=WebhookKind.CHARGE,
            event_type="charge.success",
            event_key=f"charge:{data['reference']}",
            reference=data["reference"],
            receiver_account_number=data.get("receiver_account_number"),
            amount_kobo=int(data.get("amount", 0)),
            payload=event,
        )

    def list_settlements(self, *, subaccount_code: str, since: date) -> list[Settlement]:
        self._check()
        return [
            s
            for s, _ in self.state.settlements.get(subaccount_code, [])
            if s.settled_on is None or s.settled_on >= since
        ]

    def settlement_transactions(self, settlement_id: str) -> list[SettledTransaction]:
        self._check()
        for payouts in self.state.settlements.values():
            for s, items in payouts:
                if s.id == settlement_id:
                    return items
        return []

    # --- Simulating the outside world ------------------------------------------------------------

    def settle(
        self,
        subaccount_code: str,
        references: list[str],
        *,
        on: date,
        fee_bp: int = 150,
        extra: list[SettledTransaction] | None = None,
        status: str = "success",
    ) -> Settlement:
        """Pay out these transactions to the subaccount, less `fee_bp` (1.5%), plus any `extra` unknown ones."""
        items = [
            SettledTransaction(
                r, self.state.transactions[r].amount_kobo, self.state.transactions[r].amount_kobo * fee_bp // 10_000
            )
            for r in references
        ] + list(extra or [])
        gross = sum(i.amount_kobo for i in items)
        fees = sum(i.fees_kobo for i in items)
        settlement = Settlement(f"FAKE_SET_{secrets.token_hex(4).upper()}", status, on, gross, fees, gross - fees)
        self.state.settlements.setdefault(subaccount_code, []).append((settlement, items))
        return settlement

    def _signed(self, event: dict[str, Any]) -> tuple[dict[str, str], bytes]:
        body = json.dumps(event).encode()
        return {SIGNATURE_HEADER: hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()}, body

    def simulate_transfer(self, account_number: str, amount_kobo: int) -> tuple[dict[str, str], bytes]:
        """A parent transfers money into a virtual account. Returns the signed webhook (headers, body)."""
        reference = f"FAKE_TRF_{secrets.token_hex(6).upper()}"
        self.state.transactions[reference] = _Transaction(
            reference,
            amount_kobo,
            TransactionStatus.SUCCESS,
            "dedicated_nuban",
            account_number,
            datetime.now(UTC).isoformat(),
        )
        return self._signed(
            {
                "event": "charge.success",
                "data": {"reference": reference, "amount": amount_kobo, "receiver_account_number": account_number},
            }
        )

    def complete_checkout(
        self, reference: str, *, amount_kobo: int | None = None, succeed: bool = True, channel: str = "card"
    ) -> tuple[dict[str, str], bytes]:
        """The parent finishes (or fails) a checkout, paying by `channel`. `amount_kobo` lets tests fake a mismatch."""
        txn = self.state.transactions[reference]
        txn.channel = channel
        txn.status = TransactionStatus.SUCCESS if succeed else TransactionStatus.FAILED
        txn.amount_kobo = amount_kobo if amount_kobo is not None else txn.amount_kobo
        txn.paid_at = datetime.now(UTC).isoformat()
        return self._signed(
            {
                "event": "charge.success" if succeed else "charge.failed",
                "data": {"reference": reference, "amount": txn.amount_kobo},
            }
        )
