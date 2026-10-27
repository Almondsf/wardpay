"""The Paystack and Flutterwave adapters, against canned API responses (no network)."""

import hashlib
import hmac
import json

import httpx
import pytest

from app.core.errors import ServiceUnavailable
from app.modules.payments.gateways import flutterwave, paystack
from app.modules.payments.gateways.base import (
    CustomerDetails,
    GatewayRejected,
    InvalidSignature,
    TransactionStatus,
    WebhookKind,
)


def mock_api(monkeypatch, module, handler):
    """Route the adapter's HTTP calls to `handler(request) -> httpx.Response`, recording requests."""
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    def client(provider, *, base_url, headers=None, redact_keys=frozenset()):
        return httpx.Client(base_url=base_url, headers=headers or {}, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(module, "outbound_client", client)
    return calls


# --- Paystack --------------------------------------------------------------------------------------

PAYSTACK_KEY = "sk_test_secret"


def paystack_signed(event: dict) -> tuple[dict[str, str], bytes]:
    body = json.dumps(event).encode()
    return {"x-paystack-signature": hmac.new(PAYSTACK_KEY.encode(), body, hashlib.sha512).hexdigest()}, body


def test_paystack_webhook_signature_and_parsing():
    gateway = paystack.PaystackGateway(secret_key=PAYSTACK_KEY)
    headers, body = paystack_signed(
        {
            "event": "charge.success",
            "data": {
                "reference": "T123",
                "amount": 5_000_000,
                "channel": "dedicated_nuban",
                "authorization": {"receiver_bank_account_number": "9930000001"},
            },
        }
    )
    event = gateway.parse_webhook(headers, body)
    assert (event.kind, event.reference, event.amount_kobo) == (WebhookKind.CHARGE, "T123", 5_000_000)
    assert event.receiver_account_number == "9930000001"
    assert event.event_key == "charge:T123"
    with pytest.raises(InvalidSignature):
        gateway.parse_webhook({"x-paystack-signature": "bad"}, body)
    with pytest.raises(InvalidSignature):
        paystack.PaystackGateway(secret_key="").parse_webhook(headers, body)
    other_headers, other_body = paystack_signed({"event": "transfer.success", "data": {"reference": "X"}})
    assert gateway.parse_webhook(other_headers, other_body).kind == WebhookKind.IGNORED


def test_paystack_verify_maps_the_transaction(monkeypatch):
    calls = mock_api(
        monkeypatch,
        paystack,
        lambda r: httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "reference": "T123",
                    "status": "success",
                    "amount": 5_000_000,
                    "currency": "NGN",
                    "channel": "dedicated_nuban",
                    "paid_at": "2026-10-18T09:00:00.000Z",
                    "authorization": {"receiver_bank_account_number": "9930000001"},
                },
            },
        ),
    )
    result = paystack.PaystackGateway(secret_key=PAYSTACK_KEY).verify("T123")
    assert (result.status, result.amount_kobo, result.currency) == (TransactionStatus.SUCCESS, 5_000_000, "NGN")
    assert result.receiver_account_number == "9930000001"
    assert calls[0].url.path == "/transaction/verify/T123"
    assert calls[0].headers["authorization"] == f"Bearer {PAYSTACK_KEY}"


@pytest.mark.parametrize(
    ("paystack_status", "ours"),
    [
        ("abandoned", TransactionStatus.PENDING),  # not paid yet: the customer can still pay on the same page
        ("ongoing", TransactionStatus.PENDING),
        ("failed", TransactionStatus.FAILED),
        ("reversed", TransactionStatus.FAILED),
    ],
)
def test_paystack_unfinished_checkouts_stay_pending(monkeypatch, paystack_status, ours):
    mock_api(
        monkeypatch,
        paystack,
        lambda r: httpx.Response(200, json={"status": True, "data": {"reference": "T1", "status": paystack_status}}),
    )
    assert paystack.PaystackGateway(secret_key=PAYSTACK_KEY).verify("T1").status == ours


@pytest.mark.parametrize("subaccount", ["ACCT_school", None])
def test_paystack_checkout_splits_to_the_school_or_settles_to_the_platform(monkeypatch, subaccount):
    calls = mock_api(
        monkeypatch,
        paystack,
        lambda r: httpx.Response(200, json={"status": True, "data": {"authorization_url": "https://pay/x"}}),
    )
    paystack.PaystackGateway(secret_key=PAYSTACK_KEY).initialize_payment(
        reference="WS1",
        amount_kobo=100,
        email="a@b.ng",
        callback_url="https://x",
        subaccount_code=subaccount,
        description="Bill",
    )
    body = json.loads(calls[0].content)
    if subaccount:
        assert (body["subaccount"], body["bearer"]) == ("ACCT_school", "subaccount")
    else:  # a school's own subscription: the platform's balance, no split
        assert "subaccount" not in body and "bearer" not in body


def test_paystack_subaccount_and_virtual_account(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/subaccount":
            assert json.loads(request.content)["percentage_charge"] == 0  # WardPay takes no share
            return httpx.Response(200, json={"status": True, "data": {"subaccount_code": "ACCT_x"}})
        if request.url.path == "/customer":
            return httpx.Response(200, json={"status": True, "data": {"customer_code": "CUS_1"}})
        body = json.loads(request.content)
        assert (body["customer"], body["subaccount"]) == ("CUS_1", "ACCT_x")
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "account_number": "9930000001",
                    "account_name": "SCHOOL/ADA OBI",
                    "bank": {"name": "Wema Bank"},
                },
            },
        )

    mock_api(monkeypatch, paystack, handler)
    gateway = paystack.PaystackGateway(secret_key=PAYSTACK_KEY)
    sub = gateway.create_subaccount(business_name="X", bank_code="058", account_number="0123456789")
    account = gateway.create_virtual_account(
        customer=CustomerDetails("s1", "Ada", "Obi", "p@example.com", "+2348031234567"), subaccount_code=sub.code
    )
    assert (account.account_number, account.bank_name) == ("9930000001", "Wema Bank")


def test_paystack_clears_an_old_subaccounts_platform_share(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": True, "data": {"subaccount_code": "ACCT_old"}})

    mock_api(monkeypatch, paystack, handler)
    paystack.PaystackGateway(secret_key=PAYSTACK_KEY).clear_platform_share("ACCT_old")
    [request] = calls
    assert (request.method, request.url.path) == ("PUT", "/subaccount/ACCT_old")
    assert json.loads(request.content) == {"percentage_charge": 0}


@pytest.mark.parametrize(("status_code", "error"), [(400, GatewayRejected), (503, ServiceUnavailable)])
def test_paystack_errors(monkeypatch, status_code, error):
    mock_api(
        monkeypatch,
        paystack,
        lambda r: httpx.Response(status_code, json={"status": False, "message": "Invalid account"}),
    )
    with pytest.raises(error):
        paystack.PaystackGateway(secret_key=PAYSTACK_KEY).resolve_account(bank_code="058", account_number="1")


def test_paystack_unreachable(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route")

    mock_api(monkeypatch, paystack, boom)
    with pytest.raises(ServiceUnavailable):
        paystack.PaystackGateway(secret_key=PAYSTACK_KEY).verify("T1")


# --- Flutterwave -----------------------------------------------------------------------------------


def test_flutterwave_converts_naira_to_kobo_and_checks_the_hash(monkeypatch):
    gateway = flutterwave.FlutterwaveGateway(secret_key="FLWSECK", webhook_hash="hash123")
    body = json.dumps({"event": "charge.completed", "data": {"tx_ref": "SPABC", "amount": 1500.5}}).encode()
    event = gateway.parse_webhook({"verif-hash": "hash123"}, body)
    assert (event.reference, event.amount_kobo) == ("SPABC", 150_050)
    with pytest.raises(InvalidSignature):
        gateway.parse_webhook({"verif-hash": "nope"}, body)

    calls = mock_api(
        monkeypatch,
        flutterwave,
        lambda r: httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "tx_ref": "SPABC",
                    "status": "successful",
                    "amount": 1500.5,
                    "currency": "NGN",
                    "payment_type": "card",
                },
            },
        ),
    )
    result = gateway.verify("SPABC")
    assert (result.status, result.amount_kobo) == (TransactionStatus.SUCCESS, 150_050)
    assert calls[0].url.params["tx_ref"] == "SPABC"


def test_flutterwave_checkout_sends_naira(monkeypatch):
    calls = mock_api(
        monkeypatch,
        flutterwave,
        lambda r: httpx.Response(
            200, json={"status": "success", "data": {"link": "https://checkout.flutterwave.com/x"}}
        ),
    )
    gateway = flutterwave.FlutterwaveGateway(secret_key="FLWSECK", webhook_hash="h")
    checkout = gateway.initialize_payment(
        reference="SP1",
        amount_kobo=150_050,
        email="p@example.com",
        callback_url="https://app/pay",
        subaccount_code="RS_1",
        description="Fees",
    )
    assert checkout.authorization_url == "https://checkout.flutterwave.com/x"
    body = json.loads(calls[0].content)
    assert (body["amount"], body["subaccounts"]) == ("1500.5", [{"id": "RS_1"}])


def test_flutterwave_virtual_accounts_are_refused_without_bvn():
    gateway = flutterwave.FlutterwaveGateway(secret_key="k", webhook_hash="h")
    with pytest.raises(GatewayRejected, match="BVN"):
        gateway.create_virtual_account(customer=CustomerDetails("s", "A", "B", "e@x.com", ""), subaccount_code="x")


def test_outbound_logs_never_contain_the_secret_key(monkeypatch, caplog):
    """The logging client used for real gateway calls redacts keys and card authorisations."""
    from app.core import outbound

    def logged_client(provider, *, base_url, headers=None):
        reply = {"status": True, "data": {"authorization": {"authorization_code": "AUTH_secret"}, "status": "success"}}
        return httpx.Client(
            base_url=base_url,
            headers=headers or {},
            event_hooks=outbound._hooks(provider),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply)),
        )

    monkeypatch.setattr(paystack, "outbound_client", logged_client)
    with caplog.at_level("INFO", logger="outbound.paystack"):
        paystack.PaystackGateway(secret_key=PAYSTACK_KEY).verify("T1")
    assert "verify/T1" in caplog.text
    assert PAYSTACK_KEY not in caplog.text and "AUTH_secret" not in caplog.text
