"""
Flutterwave v3 (https://developer.flutterwave.com/docs). Amounts are in naira,
so they're converted to and from kobo here.

- Subaccount: the school's bank account. WardPay takes no share of school payments (`split_value` 0).
- Permanent virtual accounts need the customer's BVN on Flutterwave, which we
  don't collect (NDPA minimisation). Until that's settled (PLAN §3.2),
  `create_virtual_account` refuses and schools on Flutterwave use card checkout.
- Webhooks carry the dashboard's "secret hash" in the `verif-hash` header.
"""

import hmac
import json
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.outbound import outbound_client

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
    Subaccount,
    TransactionStatus,
    Verification,
    WebhookEvent,
    WebhookKind,
)

API = "https://api.flutterwave.com/v3"
_STATUSES = {
    "successful": TransactionStatus.SUCCESS,
    "failed": TransactionStatus.FAILED,
    "cancelled": TransactionStatus.FAILED,
}


def _to_kobo(amount: Any) -> int:
    return int((Decimal(str(amount or 0)) * 100).to_integral_value())


def _to_naira(kobo: int) -> str:
    return str(Decimal(kobo) / 100)


class FlutterwaveGateway:
    code = GatewayCode.FLUTTERWAVE
    supports_virtual_accounts = True

    def __init__(self, secret_key: str | None = None, webhook_hash: str | None = None) -> None:
        settings = get_settings()
        self.secret_key = secret_key if secret_key is not None else settings.flutterwave_secret_key
        self.webhook_hash = webhook_hash if webhook_hash is not None else settings.flutterwave_webhook_hash

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        try:
            with outbound_client("flutterwave", base_url=API, headers=headers) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GatewayError(f"Couldn't reach Flutterwave ({type(e).__name__}). Please try again.") from e
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 500:
            raise GatewayError("Flutterwave is having problems. Please try again shortly.")
        if response.status_code >= 400 or body.get("status") != "success":
            raise GatewayRejected(f"Flutterwave: {body.get('message') or f'HTTP {response.status_code}'}")
        return body.get("data")

    def list_banks(self) -> list[Bank]:
        return [Bank(code=b["code"], name=b["name"]) for b in self._request("GET", "/banks/NG") or []]

    def resolve_account(self, *, bank_code: str, account_number: str) -> ResolvedAccount:
        data = self._request(
            "POST", "/accounts/resolve", json={"account_number": account_number, "account_bank": bank_code}
        )
        return ResolvedAccount(account_number=data["account_number"], account_name=data["account_name"])

    def create_subaccount(self, *, business_name: str, bank_code: str, account_number: str) -> Subaccount:
        data = self._request(
            "POST",
            "/subaccounts",
            json={
                "account_bank": bank_code,
                "account_number": account_number,
                "business_name": business_name,
                "country": "NG",
                "split_type": "percentage",
                "split_value": 0,
            },
        )
        return Subaccount(code=str(data["subaccount_id"]))

    def clear_platform_share(self, subaccount_code: str) -> None:
        self._request("PUT", f"/subaccounts/{subaccount_code}", json={"split_type": "percentage", "split_value": 0})

    def create_virtual_account(self, *, customer: CustomerDetails, subaccount_code: str) -> IssuedVirtualAccount:
        raise GatewayRejected(
            "Flutterwave needs each customer's BVN for permanent virtual accounts. Use Paystack "
            "for per-student accounts, or card payments."
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
        data = self._request(
            "POST",
            "/payments",
            json={
                "tx_ref": reference,
                "amount": _to_naira(amount_kobo),
                "currency": "NGN",
                "redirect_url": callback_url,
                "customer": {"email": email},
                **({"subaccounts": [{"id": subaccount_code}]} if subaccount_code else {}),
                "payment_options": "card,banktransfer,ussd",
                "customizations": {"title": get_settings().app_name, "description": description},
            },
        )
        return Checkout(authorization_url=data["link"])

    def verify(
        self, reference: str, *, provider_reference: str | None = None, payment_id: str | None = None
    ) -> Verification:
        data = self._request("GET", "/transactions/verify_by_reference", params={"tx_ref": reference}) or {}
        return Verification(
            reference=data.get("tx_ref", reference),
            status=_STATUSES.get(data.get("status", ""), TransactionStatus.PENDING),
            amount_kobo=_to_kobo(data.get("amount")),
            currency=data.get("currency", ""),
            channel=data.get("payment_type", ""),
            paid_at=data.get("created_at"),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        signature = headers.get("verif-hash", "")
        if not self.webhook_hash or not hmac.compare_digest(signature, self.webhook_hash):
            raise InvalidSignature
        try:
            event = json.loads(body)
        except ValueError as e:
            raise InvalidSignature from e
        event_type = str(event.get("event", ""))
        data = event.get("data") or {}
        reference = str(data.get("tx_ref", "")) or None
        if event_type != "charge.completed" or not reference:
            return WebhookEvent(WebhookKind.IGNORED, event_type, f"{event_type}:{reference or data.get('id', '')}")
        return WebhookEvent(
            kind=WebhookKind.CHARGE,
            event_type=event_type,
            event_key=f"charge:{reference}",
            reference=reference,
            amount_kobo=_to_kobo(data.get("amount")),
            payload=event,
        )
