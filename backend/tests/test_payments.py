"""Payments: allocation, virtual-account transfers, card checkout, manual payments, reversals, isolation."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.tenancy import scope_platform_wide, scope_to_school
from app.modules.accounts.models import Role
from app.modules.payments import webhooks
from app.modules.payments.gateways import FakeGateway, GatewayCode
from app.modules.payments.models import (
    Allocation,
    Payment,
    SchoolPaymentAccount,
    VirtualAccount,
    WebhookEvent,
    WebhookStatus,
)
from tests.factories import (
    QueryCounter,
    headers_for,
    make_calendar,
    make_levels,
    make_school,
    make_student,
    make_user,
    tenant_writer,
)
from tests.test_fees import add_fees

NAIRA = 100


def connect_gateway(school):
    with tenant_writer() as w:
        w.add(
            SchoolPaymentAccount(
                school_id=school.id,
                gateway=GatewayCode.FAKE,
                subaccount_code="FAKE_SUB_1",
                bank_code="058",
                bank_name="GTBank",
                account_number="0123456789",
                account_number_last4="6789",
                account_name="DEMO SCHOOL LTD",
            )
        )


@pytest.fixture
def world(db):
    school = make_school(db)
    people = {role: make_user(db, school, role) for role in (Role.PROPRIETOR, Role.BURSAR, Role.SCHOOL_ADMIN)}
    second_bursar = make_user(db, school, Role.BURSAR)
    _, terms = make_calendar(school)
    levels = make_levels(school)
    add_fees(school, terms[0], levels)  # JSS 1: 150,000 tuition + 5,000 PTA
    student = make_student(school, terms[0], levels[0].arms[0], first="Ada")
    connect_gateway(school)
    db.commit()
    return {
        "school": school,
        "terms": terms,
        "levels": levels,
        "student": student,
        "proprietor": headers_for(db, people[Role.PROPRIETOR]),
        "bursar": headers_for(db, people[Role.BURSAR]),
        "bursar2": headers_for(db, second_bursar),
        "admin": headers_for(db, people[Role.SCHOOL_ADMIN]),
    }


async def generate(client, world):
    term = world["terms"][0]
    response = await client.post(
        f"/api/v1/terms/{term.id}/invoices/generate", headers=world["bursar"], json={"dry_run": False}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def account(client, world, student=None):
    student = student or world["student"]
    return (await client.get(f"/api/v1/students/{student.id}/account", headers=world["bursar"])).json()


async def pay_cash(client, world, amount_naira, *, student=None, reference=""):
    student = student or world["student"]
    recorded = await client.post(
        "/api/v1/manual-payments",
        headers=world["admin"],
        json={
            "student_id": str(student.id),
            "amount_kobo": amount_naira * NAIRA,
            "method": "cash",
            "received_on": "2026-10-01",
            "reference": reference,
        },
    )
    assert recorded.status_code == 201, recorded.text
    approved = await client.post(f"/api/v1/manual-payments/{recorded.json()['id']}/approve", headers=world["bursar"])
    assert approved.status_code == 200, approved.text
    return approved.json()


async def provision(client, world):
    response = await client.post("/api/v1/virtual-accounts/provision", headers=world["bursar"], json={})
    assert response.status_code == 202, response.text
    va = await client.get(f"/api/v1/students/{world['student'].id}/virtual-account", headers=world["bursar"])
    return va.json()


# --- Allocation ----------------------------------------------------------------------------------


async def test_payments_clear_oldest_debt_first_and_overpayment_becomes_credit(client, world):
    await generate(client, world)  # term invoice: 155,000
    opening = await client.post(
        "/api/v1/invoices",
        headers=world["bursar"],
        json={
            "student_id": str(world["student"].id),
            "kind": "opening_balance",
            "lines": [{"description": "Last year's balance", "amount_kobo": 20_000 * NAIRA}],
        },
    )
    assert opening.status_code == 201

    await pay_cash(client, world, 50_000)
    invoices = {i["kind"]: i for i in (await account(client, world))["invoices"]}
    assert invoices["opening_balance"]["status"] == "paid"  # the oldest debt goes first
    assert invoices["term"]["paid_kobo"] == 30_000 * NAIRA

    await pay_cash(client, world, 140_000)  # 15,000 more than is owed
    summary = await account(client, world)
    assert summary["outstanding_kobo"] == 0
    credit = await client.get(f"/api/v1/students/{world['student'].id}/credit", headers=world["bursar"])
    assert credit.json() == 15_000 * NAIRA

    # The credit pays the next invoice automatically
    added = await client.post(
        "/api/v1/invoices",
        headers=world["bursar"],
        json={
            "student_id": str(world["student"].id),
            "kind": "adhoc",
            "lines": [{"description": "Excursion", "amount_kobo": 10_000 * NAIRA}],
        },
    )
    assert added.json()["status"] == "paid"
    credit = await client.get(f"/api/v1/students/{world['student'].id}/credit", headers=world["bursar"])
    assert credit.json() == 5_000 * NAIRA


async def test_payment_before_invoicing_is_applied_when_invoices_are_generated(client, world):
    await pay_cash(client, world, 100_000)
    await generate(client, world)
    [invoice] = (await account(client, world))["invoices"]
    assert (invoice["paid_kobo"], invoice["status"]) == (100_000 * NAIRA, "part_paid")


async def test_allocation_is_a_fixed_number_of_queries(db, world, connection):
    from app.modules.payments.allocation import allocate

    school, term, arm = world["school"], world["terms"][0], world["levels"][0].arms[0]
    few = [make_student(school, term, arm, first=f"F{i}").id for i in range(3)]
    many = [make_student(school, term, arm, first=f"M{i}").id for i in range(30)]
    scope_to_school(db, school.id)
    with QueryCounter(connection) as small:
        allocate(db, few)
    with QueryCounter(connection) as big:
        allocate(db, many)
    assert big.count == small.count


# --- Virtual accounts and transfers ---------------------------------------------------------------


async def test_transfer_into_virtual_account_is_recorded_once(client, world, db):
    await generate(client, world)
    va = await provision(client, world)
    assert va["account_number"].startswith("9") and va["bank_name"] == "Fake Bank"

    headers, body = FakeGateway.shared().simulate_transfer(va["account_number"], 60_000 * NAIRA)
    first = await client.post("/api/v1/webhooks/fake", headers=headers, content=body)
    repeat = await client.post("/api/v1/webhooks/fake", headers=headers, content=body)
    assert first.status_code == repeat.status_code == 200

    payments = (await client.get("/api/v1/payments", headers=world["bursar"])).json()
    assert payments["total"] == 1
    [payment] = payments["items"]
    assert (payment["method"], payment["amount_kobo"]) == ("virtual_account", 60_000 * NAIRA)
    assert payment["applied"][0]["amount_kobo"] == 60_000 * NAIRA
    assert (await account(client, world))["outstanding_kobo"] == 95_000 * NAIRA
    scope_platform_wide(db)
    [event] = db.scalars(select(WebhookEvent)).all()
    assert event.status == WebhookStatus.PROCESSED and event.school_id == world["school"].id


async def test_forged_webhook_is_rejected_and_not_stored(client, world, db):
    headers, body = FakeGateway.shared().simulate_transfer("9000000001", 1_000)
    forged = await client.post("/api/v1/webhooks/fake", headers={"x-fake-signature": "0" * 64}, content=body)
    assert forged.status_code == 401
    tampered = await client.post("/api/v1/webhooks/fake", headers=headers, content=body.replace(b"1000", b"9999"))
    assert tampered.status_code == 401
    scope_platform_wide(db)
    assert db.scalars(select(WebhookEvent)).all() == []


async def test_transfer_to_unknown_account_is_parked_for_review(client, world, db):
    headers, body = FakeGateway.shared().simulate_transfer("9999999999", 5_000 * NAIRA)
    assert (await client.post("/api/v1/webhooks/fake", headers=headers, content=body)).status_code == 200
    scope_platform_wide(db)
    [event] = db.scalars(select(WebhookEvent)).all()
    assert event.status == WebhookStatus.NEEDS_REVIEW
    assert db.scalars(select(Payment)).all() == []


async def test_gateway_outage_delays_processing_without_losing_the_payment(client, world, db):
    await generate(client, world)
    va = await provision(client, world)
    fake = FakeGateway.shared()
    headers, body = fake.simulate_transfer(va["account_number"], 10_000 * NAIRA)
    fake.state.unavailable = True
    assert (await client.post("/api/v1/webhooks/fake", headers=headers, content=body)).status_code == 200
    scope_platform_wide(db)
    event = db.scalars(select(WebhookEvent)).one()
    assert (event.status, event.attempts) == (WebhookStatus.RECEIVED, 1)

    fake.state.unavailable = False
    db.execute(update(WebhookEvent).values(received_at=datetime.now(UTC) - timedelta(minutes=10)))
    db.commit()
    assert webhooks.requeue_stuck() == 1
    db.expire_all()
    assert db.get(WebhookEvent, event.id).status == WebhookStatus.PROCESSED
    assert (await account(client, world))["paid_kobo"] == 10_000 * NAIRA


async def test_transfer_is_credited_to_the_right_school(client, world, db):
    """Two schools, one gateway: a transfer reaches only the school whose student owns the account."""
    other = make_school(db)
    other_bursar = make_user(db, other, Role.BURSAR)
    _, other_terms = make_calendar(other)
    other_levels = make_levels(other)
    other_student = make_student(other, other_terms[0], other_levels[0].arms[0], first="Other")
    connect_gateway(other)
    db.commit()
    other_h = headers_for(db, other_bursar)
    await client.post("/api/v1/virtual-accounts/provision", headers=other_h, json={})
    other_va = (await client.get(f"/api/v1/students/{other_student.id}/virtual-account", headers=other_h)).json()
    headers, body = FakeGateway.shared().simulate_transfer(other_va["account_number"], 7_000 * NAIRA)
    await client.post("/api/v1/webhooks/fake", headers=headers, content=body)
    assert (await client.get("/api/v1/payments", headers=other_h)).json()["total"] == 1
    assert (await client.get("/api/v1/payments", headers=world["bursar"])).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/students/{other_student.id}/virtual-account", headers=world["bursar"])
    ).status_code == 404


# --- Card checkout ---------------------------------------------------------------------------------


async def test_card_checkout_redirect_and_webhook_record_one_payment(client, world):
    await generate(client, world)
    link = await client.post(
        f"/api/v1/students/{world['student'].id}/payment-link",
        headers=world["admin"],
        json={"email": "parent@example.com"},
    )
    assert link.status_code == 201, link.text
    reference = link.json()["reference"]
    assert link.json()["amount_kobo"] == 155_000 * NAIRA  # defaults to everything owed
    assert link.json()["authorization_url"].endswith(reference)

    pending = await client.post("/api/v1/payments/verify", json={"reference": reference})
    assert pending.json()["status"] == "pending"
    headers, body = FakeGateway.shared().complete_checkout(reference)
    verified = await client.post("/api/v1/payments/verify", json={"reference": reference})
    await client.post("/api/v1/webhooks/fake", headers=headers, content=body)
    assert verified.json()["status"] == "succeeded"
    payments = (await client.get("/api/v1/payments", headers=world["bursar"])).json()
    assert payments["total"] == 1 and payments["items"][0]["method"] == "card"
    assert (await account(client, world))["outstanding_kobo"] == 0


async def test_failed_checkout_records_nothing(client, world):
    link = (
        await client.post(
            f"/api/v1/students/{world['student'].id}/payment-link",
            headers=world["admin"],
            json={"email": "parent@example.com", "amount_kobo": 1_000 * NAIRA},
        )
    ).json()
    FakeGateway.shared().complete_checkout(link["reference"], succeed=False)
    result = await client.post("/api/v1/payments/verify", json={"reference": link["reference"]})
    assert result.json()["status"] == "failed"
    assert (await client.get("/api/v1/payments", headers=world["bursar"])).json()["total"] == 0
    unknown = await client.post("/api/v1/payments/verify", json={"reference": "SP" + "0" * 20})
    assert unknown.status_code == 404


async def test_nothing_owed_needs_an_amount(client, world):
    response = await client.post(
        f"/api/v1/students/{world['student'].id}/payment-link",
        headers=world["admin"],
        json={"email": "parent@example.com"},
    )
    assert response.status_code == 400 and response.json()["error"]["field"] == "amount_kobo"


# --- Manual payments -------------------------------------------------------------------------------


async def test_manual_payment_needs_a_second_person(client, world):
    await generate(client, world)
    recorded = (
        await client.post(
            "/api/v1/manual-payments",
            headers=world["bursar"],
            json={
                "student_id": str(world["student"].id),
                "amount_kobo": 20_000 * NAIRA,
                "method": "teller",
                "received_on": "2026-10-02",
                "reference": "TLR-1001",
            },
        )
    ).json()
    assert recorded["status"] == "pending"
    assert (await account(client, world))["paid_kobo"] == 0  # not counted until approved
    self_approval = await client.post(f"/api/v1/manual-payments/{recorded['id']}/approve", headers=world["bursar"])
    assert self_approval.status_code == 403 and self_approval.json()["error"]["code"] == "maker_checker"
    approved = await client.post(f"/api/v1/manual-payments/{recorded['id']}/approve", headers=world["bursar2"])
    assert approved.json()["status"] == "approved"
    assert (await account(client, world))["paid_kobo"] == 20_000 * NAIRA
    again = await client.post(f"/api/v1/manual-payments/{recorded['id']}/approve", headers=world["proprietor"])
    assert again.status_code == 409

    duplicate = await client.post(
        "/api/v1/manual-payments",
        headers=world["admin"],
        json={
            "student_id": str(world["student"].id),
            "amount_kobo": 20_000 * NAIRA,
            "method": "teller",
            "received_on": "2026-10-02",
            "reference": "TLR-1001",
        },
    )
    assert duplicate.status_code == 409
    no_teller = await client.post(
        "/api/v1/manual-payments",
        headers=world["admin"],
        json={
            "student_id": str(world["student"].id),
            "amount_kobo": 1_000,
            "method": "teller",
            "received_on": "2026-10-02",
        },
    )
    assert no_teller.status_code == 400


async def test_rejected_manual_payment_changes_nothing(client, world):
    await generate(client, world)
    recorded = (
        await client.post(
            "/api/v1/manual-payments",
            headers=world["admin"],
            json={
                "student_id": str(world["student"].id),
                "amount_kobo": 5_000 * NAIRA,
                "method": "cash",
                "received_on": "2026-10-02",
            },
        )
    ).json()
    assert (
        await client.post(f"/api/v1/manual-payments/{recorded['id']}/approve", headers=world["admin"])
    ).status_code == 403  # school admins record, not approve
    rejected = await client.post(
        f"/api/v1/manual-payments/{recorded['id']}/reject",
        headers=world["bursar"],
        json={"reason": "Cash not in the safe"},
    )
    assert rejected.json()["status"] == "rejected"
    assert (await account(client, world))["paid_kobo"] == 0
    pending = (await client.get("/api/v1/manual-payments", headers=world["bursar"])).json()
    assert pending["total"] == 0


# --- Reversals -------------------------------------------------------------------------------------


async def test_reversal_reopens_the_debt(client, world):
    await generate(client, world)
    approved = await pay_cash(client, world, 155_000)
    assert (await account(client, world))["outstanding_kobo"] == 0
    reversal = await client.post(
        f"/api/v1/payments/{approved['payment_id']}/reverse", headers=world["bursar"], json={"reason": "Teller bounced"}
    )
    assert reversal.status_code == 200, reversal.text
    assert reversal.json()["amount_kobo"] == -155_000 * NAIRA
    assert (await account(client, world))["outstanding_kobo"] == 155_000 * NAIRA
    original = (await client.get(f"/api/v1/payments/{approved['payment_id']}", headers=world["bursar"])).json()
    assert original["reversed"] is True
    again = await client.post(
        f"/api/v1/payments/{approved['payment_id']}/reverse", headers=world["bursar"], json={"reason": "Twice"}
    )
    assert again.status_code == 409
    not_reversible = await client.post(
        f"/api/v1/payments/{reversal.json()['id']}/reverse", headers=world["bursar"], json={"reason": "Undo undo"}
    )
    assert not_reversible.status_code == 400


async def test_paid_invoice_cannot_be_voided_or_cut_below_what_was_paid(client, world):
    await generate(client, world)
    await pay_cash(client, world, 100_000)
    invoice = (await account(client, world))["invoices"][0]
    void = await client.post(
        f"/api/v1/invoices/{invoice['id']}/void", headers=world["bursar"], json={"reason": "Mistake"}
    )
    assert void.status_code == 409
    cut = await client.post(
        f"/api/v1/invoices/{invoice['id']}/lines",
        headers=world["bursar"],
        json={"kind": "adjustment", "description": "Too much", "amount_kobo": -60_000 * NAIRA, "reason": "Testing"},
    )
    assert cut.status_code == 400


# --- Database guarantees ---------------------------------------------------------------------------


async def test_payment_ledger_is_append_only_and_bounded(client, world, db):
    await generate(client, world)
    approved = await pay_cash(client, world, 10_000)
    for statement in ("UPDATE payments SET amount_kobo = 1", "DELETE FROM allocations"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(statement))
        db.rollback()
    scope_to_school(db, world["school"].id)
    invoice_id = db.scalar(select(Allocation.invoice_id))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db.add(Allocation(payment_id=uuid.UUID(approved["payment_id"]), invoice_id=invoice_id, amount_kobo=1))
    with pytest.raises(DBAPIError, match="over-allocated"):
        db.flush()
    db.rollback()


# --- Setup, permissions and isolation --------------------------------------------------------------


async def test_only_proprietor_connects_the_bank_account(client, db):
    school = make_school(db)
    owner, bursar = make_user(db, school, Role.PROPRIETOR), make_user(db, school, Role.BURSAR)
    db.commit()
    body = {"gateway": "fake", "bank_code": "058", "account_number": "0123456789"}
    assert (await client.put("/api/v1/payment-settings", headers=headers_for(db, bursar), json=body)).status_code == 403
    set_up = await client.put("/api/v1/payment-settings", headers=headers_for(db, owner), json=body)
    assert set_up.status_code == 200, set_up.text
    assert set_up.json() == {
        "gateway": "fake",
        "bank_name": "GTBank",
        "account_number_last4": "6789",
        "account_name": "DEMO SCHOOL LTD",
    }
    disabled = await client.put(
        "/api/v1/payment-settings", headers=headers_for(db, owner), json={**body, "gateway": "paystack"}
    )
    assert disabled.status_code == 400


async def test_cannot_touch_another_schools_payments(client, world, db):
    other = make_school(db)
    other_bursar, other_admin = make_user(db, other, Role.BURSAR), make_user(db, other, Role.SCHOOL_ADMIN)
    _, other_terms = make_calendar(other)
    other_student = make_student(other, other_terms[0], make_levels(other)[0].arms[0])
    connect_gateway(other)
    db.commit()
    other_admin_h = headers_for(db, other_admin)
    pending = (
        await client.post(
            "/api/v1/manual-payments",
            headers=other_admin_h,
            json={
                "student_id": str(other_student.id),
                "amount_kobo": 100,
                "method": "cash",
                "received_on": "2026-10-01",
            },
        )
    ).json()
    approved = (
        await client.post(f"/api/v1/manual-payments/{pending['id']}/approve", headers=headers_for(db, other_bursar))
    ).json()
    other_pending = (
        await client.post(
            "/api/v1/manual-payments",
            headers=other_admin_h,
            json={
                "student_id": str(other_student.id),
                "amount_kobo": 100,
                "method": "cash",
                "received_on": "2026-10-01",
            },
        )
    ).json()

    h = world["bursar"]
    assert (await client.get("/api/v1/payments", headers=h)).json()["total"] == 0
    assert (await client.get("/api/v1/manual-payments", headers=h)).json()["total"] == 0
    for method, url, body in [
        ("get", f"/api/v1/payments/{approved['payment_id']}", None),
        ("post", f"/api/v1/payments/{approved['payment_id']}/reverse", {"reason": "Not mine"}),
        ("post", f"/api/v1/manual-payments/{other_pending['id']}/approve", None),
        ("post", f"/api/v1/manual-payments/{other_pending['id']}/reject", {"reason": "Not mine"}),
        ("post", f"/api/v1/students/{other_student.id}/payment-link", {"email": "x@example.com", "amount_kobo": 100}),
        ("get", f"/api/v1/students/{other_student.id}/credit", None),
        (
            "post",
            "/api/v1/manual-payments",
            {"student_id": str(other_student.id), "amount_kobo": 100, "method": "cash", "received_on": "2026-10-01"},
        ),
    ]:
        response = await client.request(method, url, headers=h, json=body)
        assert response.status_code == 404, (url, response.text)
    scope_to_school(db, world["school"].id)
    assert db.scalars(select(VirtualAccount)).all() == []


def test_clearing_the_platform_fee_touches_every_school_once(db, monkeypatch):
    """Owner, 2026-10-27: WardPay takes no share of school payments. Old subaccounts are set to 0% by a command."""
    from app import cli
    from app.modules.payments.gateways import FakeGateway

    cleared: list[str] = []
    monkeypatch.setattr(FakeGateway, "clear_platform_share", lambda self, code: cleared.append(code))
    first, second = make_school(db, "One"), make_school(db, "Two")
    db.commit()
    connect_gateway(first)
    connect_gateway(second)
    count, failed = cli.clear_platform_fee()
    assert failed == [] and count >= 2 and cleared.count("FAKE_SUB_1") >= 2
