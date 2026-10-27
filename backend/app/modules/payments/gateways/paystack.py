"""
Paystack (https://paystack.com/docs/api/). Amounts are already in kobo.

- Subaccount: the school's bank account. WardPay takes no share of school payments (`percentage_charge` 0;
  owner, 2026-10-27: WardPay earns only from its bills to schools).
- Dedicated virtual accounts (DVA): one per student ("customer"), linked to the
  school's subaccount so transfers settle to the school. Paystack may require
  extra business approval and customer identification for DVAs (PLAN §3.2).
- Webhooks are signed: HMAC-SHA512 of the raw body with the secret key,
  in `x-paystack-signature`.
"""

import hashlib
import hmac
import json
from datetime import date
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
    SettledTransaction,
    Settlement,
    Subaccount,
    TransactionStatus,
    Verification,
    WebhookEvent,
    WebhookKind,
)

API = "https://api.paystack.co"
MAX_PAGES = 20
# Anything else ("abandoned", "ongoing", "pending", "processing", "queued") is still pending. Paystack calls a
# checkout "abandoned" until the customer pays, and they can still pay on the same page later: treating
# it as failed would close the checkout and the real payment would then be ignored.
_STATUSES = {
    "success": TransactionStatus.SUCCESS,
    "failed": TransactionStatus.FAILED,
    "reversed": TransactionStatus.FAILED,
}


class PaystackGateway:
    code = GatewayCode.PAYSTACK
    supports_virtual_accounts = True

    def __init__(self, secret_key: str | None = None) -> None:
        self.secret_key = secret_key if secret_key is not None else get_settings().paystack_secret_key

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._call(method, path, **kwargs).get("data")

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        try:
            with outbound_client("paystack", base_url=API, headers=headers) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GatewayError(f"Couldn't reach Paystack ({type(e).__name__}). Please try again.") from e
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 500:
            raise GatewayError("Paystack is having problems. Please try again shortly.")
        if response.status_code >= 400 or body.get("status") is False:
            raise GatewayRejected(f"Paystack: {body.get('message') or f'HTTP {response.status_code}'}")
        return body if isinstance(body, dict) else {}

    def _pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every row of a paged list (at most MAX_PAGES pages of 100)."""
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._call("GET", path, params={**params, "perPage": 100, "page": page})
            rows.extend(r for r in body.get("data") or [] if isinstance(r, dict))
            if page >= int((body.get("meta") or {}).get("pageCount") or 1):
                break
        return rows

    def list_settlements(self, *, subaccount_code: str, since: date) -> list[Settlement]:
        """
        Payouts to the school's subaccount. Paystack's fields: `total_processed` (what parents paid),
        `total_fees`, `effective_amount` (what reached the bank, after any deductions).
        """
        rows = self._pages("/settlement", {"subaccount": subaccount_code, "from": since.isoformat()})
        settlements = []
        for row in rows:
            paid_out = int(row.get("effective_amount") or row.get("total_amount") or 0)
            fees = int(row.get("total_fees") or 0)
            gross = int(row.get("total_processed") or 0) or paid_out + fees
            settled = str(row.get("settlement_date") or row.get("settled_at") or "")[:10]
            settlements.append(
                Settlement(
                    id=str(row.get("id")),
                    status=str(row.get("status") or ""),
                    settled_on=date.fromisoformat(settled) if settled else None,
                    gross_kobo=gross,
                    charges_kobo=gross - paid_out,
                    paid_out_kobo=paid_out,
                )
            )
        return settlements

    def settlement_transactions(self, settlement_id: str) -> list[SettledTransaction]:
        return [
            SettledTransaction(
                reference=str(row.get("reference") or ""),
                amount_kobo=int(row.get("amount") or 0),
                fees_kobo=int(row.get("fees") or 0),
            )
            for row in self._pages(f"/settlement/{settlement_id}/transactions", {})
            if row.get("reference")
        ]

    def list_banks(self) -> list[Bank]:
        data = self._request("GET", "/bank", params={"country": "nigeria", "perPage": 200})
        return [Bank(code=b["code"], name=b["name"]) for b in data or [] if b.get("active", True)]

    def resolve_account(self, *, bank_code: str, account_number: str) -> ResolvedAccount:
        data = self._request("GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code})
        return ResolvedAccount(account_number=data["account_number"], account_name=data["account_name"])

    def create_subaccount(self, *, business_name: str, bank_code: str, account_number: str) -> Subaccount:
        data = self._request(
            "POST",
            "/subaccount",
            json={
                "business_name": business_name,
                "settlement_bank": bank_code,
                "account_number": account_number,
                "percentage_charge": 0,
            },
        )
        return Subaccount(code=data["subaccount_code"])

    def clear_platform_share(self, subaccount_code: str) -> None:
        self._request("PUT", f"/subaccount/{subaccount_code}", json={"percentage_charge": 0})

    def create_virtual_account(self, *, customer: CustomerDetails, subaccount_code: str) -> IssuedVirtualAccount:
        created = self._request(
            "POST",
            "/customer",
            json={
                "email": customer.email,
                "first_name": customer.first_name,
                "last_name": customer.last_name,
                "phone": customer.phone,
                "metadata": {"student_id": customer.reference},
            },
        )
        data = self._request(
            "POST",
            "/dedicated_account",
            json={
                "customer": created["customer_code"],
                "preferred_bank": get_settings().paystack_preferred_bank,
                "subaccount": subaccount_code,
            },
        )
        return IssuedVirtualAccount(
            account_number=data["account_number"],
            account_name=data["account_name"],
            bank_name=data["bank"]["name"],
            provider_reference=created["customer_code"],
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
        split = {"subaccount": subaccount_code, "bearer": "subaccount"} if subaccount_code else {}
        data = self._request(
            "POST",
            "/transaction/initialize",
            json={
                "reference": reference,
                "amount": amount_kobo,
                "currency": "NGN",
                "email": email,
                "callback_url": callback_url,
                **split,
                "channels": ["card", "bank_transfer", "ussd"],
                "metadata": {"description": description},
            },
        )
        return Checkout(authorization_url=data["authorization_url"])

    def verify(
        self, reference: str, *, provider_reference: str | None = None, payment_id: str | None = None
    ) -> Verification:
        data = self._request("GET", f"/transaction/verify/{reference}") or {}
        authorization = data.get("authorization") or {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return Verification(
            reference=data.get("reference", reference),
            status=_STATUSES.get(data.get("status", ""), TransactionStatus.PENDING),
            amount_kobo=int(data.get("amount") or 0),
            currency=data.get("currency", ""),
            channel=data.get("channel", ""),
            receiver_account_number=authorization.get("receiver_bank_account_number")
            or (metadata or {}).get("receiver_account_number"),
            paid_at=data.get("paid_at"),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        signature = headers.get("x-paystack-signature", "")
        expected = hmac.new(self.secret_key.encode(), body, hashlib.sha512).hexdigest()
        if not self.secret_key or not hmac.compare_digest(signature, expected):
            raise InvalidSignature
        try:
            event = json.loads(body)
        except ValueError as e:
            raise InvalidSignature from e
        event_type = str(event.get("event", ""))
        data = event.get("data") or {}
        reference = str(data.get("reference", "")) or None
        if event_type != "charge.success" or not reference:
            return WebhookEvent(WebhookKind.IGNORED, event_type, f"{event_type}:{reference or data.get('id', '')}")
        authorization = data.get("authorization") or {}
        return WebhookEvent(
            kind=WebhookKind.CHARGE,
            event_type=event_type,
            event_key=f"charge:{reference}",
            reference=reference,
            receiver_account_number=authorization.get("receiver_bank_account_number"),
            amount_kobo=int(data.get("amount") or 0),
            payload=event,
        )
