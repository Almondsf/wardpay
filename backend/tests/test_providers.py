"""Switching the payment provider at runtime, and the Bachs adapter (against canned API responses)."""

import hashlib
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.accounts.models import Role
from app.modules.audit.models import PlatformAuditLog
from app.modules.payments.gateways import bachs
from app.modules.payments.gateways.base import (
    Checkout,
    GatewayCode,
    GatewayRejected,
    InvalidSignature,
    TransactionStatus,
    Verification,
    WebhookKind,
)
from app.modules.payments.models import PaymentIntent, SchoolPaymentAccount
from tests.factories import headers_for, make_user, tenant_writer
from tests.test_gateways import mock_api
from tests.test_payments import NAIRA, generate
from tests.test_payments import world as world  # the fixture

SECRET = "whsec_test"


def signed(event: dict, *, secret: str = SECRET, at: int | None = None, v2: bool = True) -> tuple[dict, bytes]:
    body = json.dumps(event).encode()
    ts = str(at if at is not None else int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = (
        {"x-bachs-signature-v2": f"t={ts},v1={sig}"} if v2 else {"x-bachs-timestamp": ts, "x-bachs-signature": sig}
    )
    return headers, body


def succeeded(reference: str, charge_id: str = "ch_1", amount: str = "155000.00") -> dict:
    return {
        "id": "evt_1",
        "type": "collection.succeeded",
        "data": {"charge_id": charge_id, "checkout_id": "chk_1", "reference": reference, "amount": amount},
    }


# --- The Bachs adapter -----------------------------------------------------------------------------


def test_checkout_is_a_destination_charge_with_the_platform_fee(monkeypatch):
    calls = mock_api(
        monkeypatch,
        bachs,
        lambda r: httpx.Response(200, json={"checkout_id": "chk_9", "checkout_url": "https://checkout.bachs.io/c/x"}),
    )
    gateway = bachs.BachsGateway(secret_key="sk_sandbox_abc", webhook_secret=SECRET)
    checkout = gateway.initialize_payment(
        reference="SPABC",
        amount_kobo=6_800_000,
        email="p@example.com",
        callback_url="https://app.test/pay/complete",
        subaccount_code="acct_school",
        description="School fees",
    )
    assert checkout == Checkout("https://checkout.bachs.io/c/x", "chk_9")
    [request] = calls
    assert str(request.url) == "https://sandbox-api.bachs.io/v1/checkout-sessions"
    assert request.headers["authorization"] == "Bearer sk_sandbox_abc"
    body = json.loads(request.content)
    assert body["pricing"] == {"currency": "NGN", "amount": "68000.00", "price_type": "fixed"}
    assert body["transfer_data"] == {"destination": "acct_school"} and "platform_fee" not in body  # WardPay takes none
    assert body["reference"] == "SPABC" and body["success_url"] == "https://app.test/pay/complete?reference=SPABC"
    assert bachs.BachsGateway(secret_key="sk_live_x").api == "https://api.bachs.io"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("SUCCEEDED", TransactionStatus.SUCCESS), ("OVERPAID", TransactionStatus.SUCCESS),
     ("FAILED", TransactionStatus.FAILED), ("PROCESSING", TransactionStatus.PENDING)],
)  # fmt: skip
def test_verify_reads_the_payment(monkeypatch, state, expected):
    payment = {"id": "ch_1", "status": state, "amount": "155000.50", "currency": "NGN", "reference": "SPABC",
               "payment_method_details": {"type": "bank_transfer"}}  # fmt: skip
    mock_api(monkeypatch, bachs, lambda r: httpx.Response(200, json=payment))
    result = bachs.BachsGateway(secret_key="sk_sandbox_x").verify("SPABC", payment_id="ch_1")
    assert result.status == expected and result.amount_kobo == 15_500_050
    assert result.reference == "SPABC" and result.channel == "bank_transfer"


def test_verify_finds_the_payment_through_the_checkout(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/checkout-sessions/chk_1":
            return httpx.Response(200, json={"checkout_id": "chk_1", "status": "completed", "charge_id": "ch_7"})
        return httpx.Response(200, json={"status": "SUCCEEDED", "amount": "10.00", "currency": "NGN"})

    calls = mock_api(monkeypatch, bachs, handler)
    result = bachs.BachsGateway(secret_key="sk_sandbox_x").verify("SPABC", provider_reference="chk_1")
    assert result.status == TransactionStatus.SUCCESS
    assert [c.url.path for c in calls] == ["/v1/checkout-sessions/chk_1", "/v1/payments/ch_7"]


def test_unpaid_checkout_is_pending_and_errors_are_clear(monkeypatch):
    mock_api(monkeypatch, bachs, lambda r: httpx.Response(200, json={"checkout_id": "chk_1", "status": "open"}))
    gateway = bachs.BachsGateway(secret_key="sk_sandbox_x")
    assert gateway.verify("SPABC", provider_reference="chk_1").status == TransactionStatus.PENDING
    mock_api(monkeypatch, bachs, lambda r: httpx.Response(422, json={"error": {"message": "amount too small"}}))
    with pytest.raises(GatewayRejected, match="amount too small"):
        gateway.initialize_payment(
            reference="R", amount_kobo=100, email="e@x.ng", callback_url="https://a", subaccount_code="acct",
            description="d",
        )  # fmt: skip
    with pytest.raises(GatewayRejected, match="own account number"):
        gateway.create_virtual_account(customer=None, subaccount_code="acct")  # type: ignore[arg-type]
    assert gateway.supports_virtual_accounts is False


def test_webhook_signatures():
    gateway = bachs.BachsGateway(secret_key="sk_sandbox_x", webhook_secret=SECRET)
    for v2 in (True, False):
        event = gateway.parse_webhook(*signed(succeeded("SPABC"), v2=v2))
        assert event.kind == WebhookKind.CHARGE and event.reference == "SPABC"
        assert event.provider_payment_id == "ch_1" and event.event_key == "charge:ch_1"
        assert event.amount_kobo == 15_500_000
    for bad in (signed(succeeded("SPABC"), secret="wrong"), signed(succeeded("SPABC"), at=int(time.time()) - 600)):
        with pytest.raises(InvalidSignature):
            gateway.parse_webhook(*bad)
    ignored = gateway.parse_webhook(*signed({"id": "evt_2", "type": "checkout.expired", "data": {}}))
    assert ignored.kind == WebhookKind.IGNORED
    with pytest.raises(InvalidSignature):  # no secret configured: nothing is trusted
        bachs.BachsGateway(secret_key="sk_sandbox_x", webhook_secret="").parse_webhook(*signed(succeeded("R")))


# --- Switching at runtime --------------------------------------------------------------------------


@pytest.fixture
def both(monkeypatch):
    """Bachs configured beside the fake gateway, with its network calls replaced."""
    monkeypatch.setattr(get_settings(), "payment_gateways", ["fake", "bachs"])
    monkeypatch.setattr(get_settings(), "bachs_secret_key", "sk_sandbox_x")
    monkeypatch.setattr(get_settings(), "bachs_webhook_secret", SECRET)
    started: list[dict] = []

    def initialize(self, **kwargs):
        started.append(kwargs)
        return Checkout(f"https://checkout.bachs.io/c/{kwargs['reference']}", f"chk_{len(started)}")

    def verify(self, reference, *, provider_reference=None, payment_id=None):
        return Verification(reference, TransactionStatus.SUCCESS, 155_000 * NAIRA, "NGN", "card")

    monkeypatch.setattr(bachs.BachsGateway, "initialize_payment", initialize)
    monkeypatch.setattr(bachs.BachsGateway, "verify", verify)
    return started


def platform_admin(db):
    admin = make_user(db, None, Role.PLATFORM_ADMIN)
    db.commit()
    return headers_for(db, admin)


def on_bachs(world):
    with tenant_writer() as w:
        w.add(
            SchoolPaymentAccount(
                school_id=world["school"].id, gateway=GatewayCode.BACHS, subaccount_code="acct_school",
                bank_code="", bank_name="Set up with Bachs", account_number="", account_number_last4="",
                account_name="School",
            )
        )  # fmt: skip


async def pay_link(client, world):
    response = await client.post(
        f"/api/v1/students/{world['student'].id}/payment-link",
        headers=world["admin"],
        json={"email": "p@example.com", "amount_kobo": 10_000 * NAIRA},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_platform_admin_switches_the_provider(client, world, db, both):
    admin = platform_admin(db)
    providers = (await client.get("/api/v1/platform/providers", headers=admin)).json()
    assert providers[0]["active_provider"] == "bachs"  # no choice saved: the first real provider configured
    assert set(providers[0]["available"]) == {"fake", "bachs"}

    switched = await client.put("/api/v1/platform/providers/payment", headers=admin, json={"provider": "fake"})
    assert switched.status_code == 200 and switched.json()["active_provider"] == "fake"
    refused = await client.put("/api/v1/platform/providers/payment", headers=admin, json={"provider": "paystack"})
    assert refused.status_code == 400 and "isn't configured" in refused.text
    assert (await client.put("/api/v1/platform/providers/payment", headers=world["proprietor"],
                             json={"provider": "bachs"})).status_code == 403  # fmt: skip
    scope_platform_wide(db)
    assert db.scalar(select(func.count()).where(PlatformAuditLog.action == "platform.provider_changed")) == 1


async def test_checkouts_follow_the_switch_and_fall_back(client, world, db, both):
    await generate(client, world)
    admin = platform_admin(db)
    await client.put("/api/v1/platform/providers/payment", headers=admin, json={"provider": "bachs"})
    # Not set up on Bachs yet: the school's existing provider keeps taking payments
    assert "checkout.fake" in (await pay_link(client, world))["authorization_url"]

    on_bachs(world)
    link = await pay_link(client, world)
    assert link["authorization_url"].startswith("https://checkout.bachs.io/")
    assert both[-1]["subaccount_code"] == "acct_school" and "platform_fee_bp" not in both[-1]
    scope_to_school(db, world["school"].id)
    intent = db.scalar(select(PaymentIntent).where(PaymentIntent.reference == link["reference"]))
    assert intent.gateway == GatewayCode.BACHS and intent.provider_reference.startswith("chk_")

    # The signed webhook settles it; the payment is recorded once, on Bachs
    headers, body = signed(succeeded(link["reference"]))
    assert (await client.post("/api/v1/webhooks/bachs", headers=headers, content=body)).status_code == 200
    await client.post("/api/v1/webhooks/bachs", headers=headers, content=body)  # repeated delivery
    payments = (await client.get("/api/v1/payments", headers=world["bursar"])).json()
    assert payments["total"] == 1 and payments["items"][0]["method"] == "card"

    await client.put("/api/v1/platform/providers/payment", headers=admin, json={"provider": "fake"})
    assert "checkout.fake" in (await pay_link(client, world))["authorization_url"]  # switched back at once


async def test_forged_bachs_webhook_is_refused(client, world, both):
    headers, body = signed(succeeded("SPX"), secret="not-ours")
    assert (await client.post("/api/v1/webhooks/bachs", headers=headers, content=body)).status_code == 401


async def test_account_numbers_need_a_provider_that_issues_them(client, world, db, both):
    with tenant_writer() as w:
        w.query(SchoolPaymentAccount).delete()
    on_bachs(world)
    response = await client.post("/api/v1/virtual-accounts/provision", headers=world["bursar"], json={})
    assert response.status_code == 400 and "Paystack" in response.text
    settings = (await client.get("/api/v1/payment-settings", headers=world["bursar"])).json()
    assert [a["gateway"] for a in settings["accounts"]] == ["bachs"]
