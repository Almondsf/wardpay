"""
Bachs (https://docs.bachs.io). Amounts are decimal naira strings ("68000.00").

- A school is a Bachs **connected account** (`acct_...`), set up through Bachs' own hosted
  onboarding (business details, identity, payout bank). We never see its bank details.
- Card and bank-transfer payments are **checkout sessions** charged as destination charges:
  the money is the school's, and the platform fee (`platform_fee`) is split off by Bachs.
- Bachs gives a business one fixed account number, not one per customer, so it can't give
  each student their own account number (`supports_virtual_accounts = False`).
- Webhooks carry our checkout `reference` and Bachs' `charge_id`; the outcome is always
  re-read from `GET /v1/payments/{charge_id}`. Signatures: HMAC-SHA256 hex of
  "{timestamp}.{raw body}" with the endpoint's signing secret.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
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

LIVE_API = "https://api.bachs.io"
SANDBOX_API = "https://sandbox-api.bachs.io"
SIGNATURE_TOLERANCE_SECONDS = 300
_SUCCEEDED = {"SUCCEEDED", "ACCEPTED", "OVERPAID"}
_FAILED = {"FAILED", "CANCELED", "CANCELLED", "EXPIRED", "DECLINED", "REVERSED"}
NOT_THROUGH_BACHS = "With Bachs, the school's bank details are set up in Bachs' own onboarding."


def _naira(kobo: int) -> str:
    return f"{Decimal(kobo) / 100:.2f}"


def _kobo(amount: Any) -> int:
    try:
        return int((Decimal(str(amount or "0")) * 100).to_integral_value())
    except InvalidOperation:
        return 0


def _first(data: dict[str, Any], *paths: str) -> str | None:
    """The first non-empty value among dotted paths, e.g. 'payment.id'."""
    for path in paths:
        value: Any = data
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value:
            return str(value)
    return None


class BachsGateway:
    code = GatewayCode.BACHS
    supports_virtual_accounts = False

    def __init__(self, secret_key: str | None = None, webhook_secret: str | None = None) -> None:
        settings = get_settings()
        self.secret_key = secret_key if secret_key is not None else settings.bachs_secret_key
        self.webhook_secret = webhook_secret if webhook_secret is not None else settings.bachs_webhook_secret
        self.api = LIVE_API if self.secret_key.startswith("sk_live_") else SANDBOX_API

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.secret_key}", "Content-Type": "application/json"}
        try:
            with outbound_client("bachs", base_url=self.api, headers=headers) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GatewayError(f"Couldn't reach Bachs ({type(e).__name__}). Please try again.") from e
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 500:
            raise GatewayError("Bachs is having problems. Please try again shortly.")
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            message = error.get("message") or body.get("message") or f"HTTP {response.status_code}"
            raise GatewayRejected(f"Bachs: {message}")
        return body.get("data", body) if isinstance(body, dict) else body

    # Schools: a connected account, onboarded on Bachs' hosted pages

    def create_connected_account(self, *, business_name: str, email: str) -> Subaccount:
        data = self._request(
            "POST",
            "/v1/accounts",
            json={
                "display_name": business_name,
                "contact_email": email,
                "country": "NG",
                "configuration": {
                    "merchant": {
                        "capabilities": {
                            "card_collection": {"requested": True},
                            "ngn_card_collection": {"requested": True},
                        }
                    },
                    "recipient": {"capabilities": {"payouts": {"requested": True}, "transfers": {"requested": True}}},
                },
            },
        )
        account_id = _first(data, "id", "account_id")
        if not account_id:
            raise GatewayError("Bachs didn't return the school's account id.")
        return Subaccount(code=account_id)

    def onboarding_link(self, account_id: str, *, return_url: str, refresh_url: str) -> str:
        data = self._request(
            "POST",
            f"/v1/accounts/{account_id}/account-links",
            json={"type": "onboarding", "return_url": return_url, "refresh_url": refresh_url},
        )
        url = _first(data, "url")
        if not url:
            raise GatewayError("Bachs didn't return an onboarding link.")
        return url

    def list_banks(self) -> list[Bank]:
        raise GatewayRejected(NOT_THROUGH_BACHS)

    def resolve_account(self, *, bank_code: str, account_number: str) -> ResolvedAccount:
        raise GatewayRejected(NOT_THROUGH_BACHS)

    def create_subaccount(self, *, business_name: str, bank_code: str, account_number: str) -> Subaccount:
        raise GatewayRejected(NOT_THROUGH_BACHS)

    def clear_platform_share(self, subaccount_code: str) -> None:
        return None  # Bachs takes a platform fee only when one is asked for at checkout, and none is

    def create_virtual_account(self, *, customer: CustomerDetails, subaccount_code: str) -> IssuedVirtualAccount:
        raise GatewayRejected("Bachs can't give each pupil their own account number.")

    # Payments

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
        # Bachs appends its checkout_id; our reference lets the return page find the payment
        back = f"{callback_url}{'&' if '?' in callback_url else '?'}reference={reference}"
        data = self._request(
            "POST",
            "/v1/checkout-sessions",
            json={
                "pricing": {"currency": "NGN", "amount": _naira(amount_kobo), "price_type": "fixed"},
                "customer": {"email": email},
                "reference": reference,
                "success_url": back,
                "cancel_url": back,
                **({"transfer_data": {"destination": subaccount_code}} if subaccount_code else {}),
                "metadata": {"description": description[:200]},
            },
        )
        url, checkout_id = _first(data, "checkout_url", "url"), _first(data, "checkout_id", "id")
        if not url or not checkout_id:
            raise GatewayError("Bachs didn't return a checkout page.")
        return Checkout(authorization_url=url, provider_reference=checkout_id)

    def verify(
        self, reference: str, *, provider_reference: str | None = None, payment_id: str | None = None
    ) -> Verification:
        if payment_id is None and reference.startswith("ch_"):
            payment_id = reference  # a deposit or other payment known only by its charge id
        if payment_id is None and provider_reference:
            session = self._request("GET", f"/v1/checkout-sessions/{provider_reference}") or {}
            payment_id = _first(session, "charge_id", "payment_id", "payment.id", "charge.id", "latest_charge")
            if payment_id is None:
                state = (_first(session, "status") or "").upper()
                status = TransactionStatus.FAILED if state in _FAILED else TransactionStatus.PENDING
                return Verification(reference, status, 0, "NGN", "")
        if payment_id is None:
            return Verification(reference, TransactionStatus.PENDING, 0, "NGN", "")
        data = self._request("GET", f"/v1/payments/{payment_id}") or {}
        state = (_first(data, "status") or "").upper()
        status = (
            TransactionStatus.SUCCESS
            if state in _SUCCEEDED
            else TransactionStatus.FAILED
            if state in _FAILED
            else TransactionStatus.PENDING
        )
        return Verification(
            reference=_first(data, "reference") or reference,
            status=status,
            amount_kobo=_kobo(data.get("amount")),
            currency=(_first(data, "currency") or "").upper(),
            channel=_first(data, "payment_method_details.type", "payment_method.type", "payment_method_type") or "",
            receiver_account_number=_first(data, "payment_method_details.bank_transfer.virtual_account.account_number"),
            paid_at=_first(data, "paid_at", "created_at"),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        if not self._signature_ok(headers, body):
            raise InvalidSignature
        try:
            event = json.loads(body)
        except ValueError as e:
            raise InvalidSignature from e
        event_type = str(event.get("type", ""))
        data = event.get("data") or {}
        charge_id = _first(data, "charge_id")
        if event_type != "collection.succeeded" or not charge_id:
            return WebhookEvent(WebhookKind.IGNORED, event_type, f"{event_type}:{event.get('id', '')}")
        return WebhookEvent(
            kind=WebhookKind.CHARGE,
            event_type=event_type,
            event_key=f"charge:{charge_id}",
            # A checkout carries our reference; a deposit into the school's fixed account doesn't
            reference=_first(data, "reference") or charge_id,
            receiver_account_number=_first(data, "payment_method_details.bank_transfer.virtual_account.account_number"),
            amount_kobo=_kobo(data.get("amount")),
            provider_payment_id=charge_id,
            payload=event,
        )

    def _signature_ok(self, headers: dict[str, str], body: bytes) -> bool:
        if not self.webhook_secret:
            return False
        timestamp, signatures = headers.get("x-bachs-timestamp", ""), [headers.get("x-bachs-signature", "")]
        v2 = headers.get("x-bachs-signature-v2", "")
        if v2:  # "t=...,v1=...[,v1=...]" (several during a secret rotation)
            parts = [p.split("=", 1) for p in v2.split(",") if "=" in p]
            timestamp = next((v for k, v in parts if k.strip() == "t"), timestamp)
            signatures = [v for k, v in parts if k.strip() == "v1"]
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False
        expected = hmac.new(self.webhook_secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        return any(s and hmac.compare_digest(expected, s) for s in signatures)
