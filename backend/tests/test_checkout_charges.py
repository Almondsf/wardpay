"""Who pays the provider's charge on online checkouts: the school by default, or the parent on top (each school
chooses). Either way the bills are credited with the fees only."""

import pytest
from sqlalchemy import select

from app.core.tenancy import scope_to_school
from app.modules.accounts.models import Role
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.payments.checkout_charges import PAYSTACK_LOCAL, ChargePayer, parent_charge
from app.modules.payments.gateways import FakeGateway, GatewayCode
from app.modules.payments.models import Payment
from tests.factories import headers_for, make_user
from tests.test_family_link import NAIRA, checkout, owed, staff_link
from tests.test_family_link import world as world  # the fixture: Ada owes 155k, Bayo 185k
from tests.test_fees import generate
from tests.test_parents import bearer, sign_in

SETTING = "/api/v1/payment-settings/checkout-charges"


@pytest.mark.parametrize(
    ("naira", "charge"),
    [
        (1_000, 15.23),  # 1.5%, no ₦100 under ₦2,500
        (10_000, 253.81),  # 1.5% + ₦100, on the grossed-up amount
        (100_000, 1_624.37),
        (125_000, 2_000),  # the cap
        (500_000, 2_000),
    ],
)
def test_the_school_still_gets_the_fees_in_full(naira, charge):
    net = naira * NAIRA
    gross = PAYSTACK_LOCAL.gross_up(net)
    assert gross - net == round(charge * NAIRA)
    assert gross - PAYSTACK_LOCAL.fee(gross) >= net  # the school receives at least the fees
    assert (gross - 1) - PAYSTACK_LOCAL.fee(gross - 1) < net  # and the parent pays not a kobo more than needed


def test_every_amount_grosses_up_exactly():
    for net in range(100, 2_000_000, 997):  # ₦1 to ₦20,000, across the ₦2,500 threshold
        gross = PAYSTACK_LOCAL.gross_up(net)
        assert gross - PAYSTACK_LOCAL.fee(gross) >= net > (gross - 1) - PAYSTACK_LOCAL.fee(gross - 1)


def test_school_pays_unless_it_chooses_otherwise_and_unknown_providers_add_nothing():
    assert parent_charge(GatewayCode.PAYSTACK, ChargePayer.SCHOOL, 100_000 * NAIRA) == 0
    assert parent_charge(GatewayCode.FLUTTERWAVE, ChargePayer.PARENT, 100_000 * NAIRA) == 0  # rates unknown here
    assert parent_charge(GatewayCode.PAYSTACK, ChargePayer.PARENT, 100_000 * NAIRA) == 162_437


async def choose(client, db, world, payer: str):
    proprietor = headers_for(db, make_user(db, world["school"], Role.PROPRIETOR))
    response = await client.put(SETTING, headers=proprietor, json={"payer": payer})
    assert response.status_code == 200, response.text
    return response


async def test_by_default_the_parent_pays_just_the_fees(client, world):
    await generate(client, world)
    assert (await client.get(SETTING, headers=world["bursar"])).json() == {"payer": "school"}
    token = await staff_link(client, world)
    assert (await client.get(f"/api/v1/public/pay/{token}")).json()["checkout_charge"] is None
    started = (await checkout(client, token, world, ada=100_000)).json()
    assert started["amount_kobo"] == 100_000 * NAIRA and started["charge_kobo"] == 0
    assert FakeGateway.shared().state.transactions[started["reference"]].amount_kobo == 100_000 * NAIRA


async def test_parent_pays_the_charge_on_top_and_the_bill_gets_the_fees(client, world, db):
    await generate(client, world)
    await choose(client, db, world, "parent")
    token = await staff_link(client, world)
    page = (await client.get(f"/api/v1/public/pay/{token}")).json()
    assert page["checkout_charge"] == {
        "percent_bp": 150,
        "flat_kobo": 10_000,
        "flat_from_kobo": 250_000,
        "cap_kobo": 200_000,
    }

    started = (await checkout(client, token, world, ada=100_000, bayo=50_000)).json()
    charge = PAYSTACK_LOCAL.gross_up(150_000 * NAIRA) - 150_000 * NAIRA
    assert started["amount_kobo"] == 150_000 * NAIRA and started["charge_kobo"] == charge
    reference = started["reference"]
    assert FakeGateway.shared().state.transactions[reference].amount_kobo == 150_000 * NAIRA + charge

    FakeGateway.shared().complete_checkout(reference)
    assert (await client.post("/api/v1/payments/verify", json={"reference": reference})).json()["status"] == "succeeded"
    assert await owed(client, world, "ada") == 55_000 * NAIRA  # credited with the fees, not the charge
    assert await owed(client, world, "bayo") == 135_000 * NAIRA
    scope_to_school(db, world["school"].id)
    assert sum(db.scalars(select(Payment.amount_kobo))) == 150_000 * NAIRA


async def test_one_child_from_the_portal_and_staff_links_carry_the_charge_too(client, world, db):
    await generate(client, world)
    await choose(client, db, world, "parent")
    portal = await client.post(
        f"/api/v1/parent/children/{world['ada'].id}/pay",
        headers=bearer(await sign_in(client)),
        json={"amount_kobo": 10_000 * NAIRA},
    )
    assert portal.status_code == 201, portal.text
    assert portal.json()["charge_kobo"] == 25_381
    reference = portal.json()["reference"]
    FakeGateway.shared().complete_checkout(reference)
    await client.post("/api/v1/payments/verify", json={"reference": reference})
    assert await owed(client, world, "ada") == 145_000 * NAIRA

    link = await client.post(
        f"/api/v1/students/{world['bayo'].id}/payment-link",
        headers=world["bursar"],
        json={"amount_kobo": 100_000 * NAIRA, "email": "parent@example.com"},
    )
    assert link.status_code == 201 and link.json()["charge_kobo"] == 162_437


async def test_a_wrong_amount_with_a_charge_is_parked_not_guessed(client, world, db):
    await generate(client, world)
    await choose(client, db, world, "parent")
    portal = await client.post(
        f"/api/v1/parent/children/{world['ada'].id}/pay",
        headers=bearer(await sign_in(client)),
        json={"amount_kobo": 10_000 * NAIRA},
    )
    reference = portal.json()["reference"]
    FakeGateway.shared().complete_checkout(reference, amount_kobo=10_000 * NAIRA)  # the charge didn't come
    result = await client.post("/api/v1/payments/verify", json={"reference": reference})
    assert result.json()["status"] == "needs_review"
    assert await owed(client, world, "ada") == 155_000 * NAIRA


async def test_only_the_proprietor_chooses_and_it_is_recorded(client, world, db):
    refused = await client.put(SETTING, headers=world["bursar"], json={"payer": "parent"})
    assert refused.status_code == 403
    await choose(client, db, world, "parent")
    scope_to_school(db, world["school"].id)
    entry = db.scalar(select(AuditLog).where(AuditLog.action == AuditAction.CHECKOUT_CHARGE_PAYER_CHANGED.value))
    assert entry is not None and entry.changes == {"before": "school", "after": "parent"}
    assert (await client.put(SETTING, headers=world["bursar"], json={"payer": "nobody"})).status_code in (403, 422)
