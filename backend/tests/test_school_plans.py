"""The school's own instalment plans: set up by staff, chosen by parents, and charged to the school by WardPay."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.clock import lagos_today
from app.core.tenancy import scope_platform_wide
from app.modules.accounts.models import Role
from tests.factories import QueryCounter, headers_for, make_school, make_user
from tests.test_family_link import page, staff_link
from tests.test_family_link import world as world  # the fixture
from tests.test_fees import generate
from tests.test_parents import bearer, sign_in

NAIRA = 100
ADA_OWES = 155_000 * NAIRA  # JSS 1 tuition + PTA in add_fees
PAY_IN_3 = {
    "name": "Pay in 3",
    "eligibility": "anyone",
    "charge_kind": "fixed",
    "charge_value": 1_500 * NAIRA,
    "parts": [{"percent_bp": 5000, "day": 0}, {"percent_bp": 2500, "day": 40}, {"percent_bp": 2500, "day": 70}],
}


@pytest.fixture
def term_running(world, db):
    """The current term started ten days ago and runs for another hundred."""
    today = lagos_today()
    scope_platform_wide(db)
    db.execute(
        text("UPDATE terms SET start_date = :s, end_date = :e WHERE id = :id"),
        {"s": today - timedelta(days=10), "e": today + timedelta(days=100), "id": world["terms"][0].id},
    )
    db.commit()
    return world


async def add_plan(client, world, **changes):
    response = await client.post("/api/v1/plan-templates", headers=world["bursar"], json={**PAY_IN_3, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def ada(page_json, world):
    return next(c for c in page_json["children"] if c["student_id"] == str(world["ada"].id))


async def test_a_school_sets_up_its_plans(client, world):
    plan = await add_plan(client, world)
    assert [p["percent_bp"] for p in plan["parts"]] == [5000, 2500, 2500]
    bad = {
        "shares": {"parts": [{"percent_bp": 5000, "day": 0}, {"percent_bp": 4000, "day": 30}]},
        "order": {"parts": [{"percent_bp": 5000, "day": 30}, {"percent_bp": 5000, "day": 10}]},
        "too much": {"charge_kind": "percent", "charge_value": 2500},
    }
    for why, change in bad.items():
        refused = await client.post(
            "/api/v1/plan-templates", headers=world["bursar"], json={**PAY_IN_3, **change, "name": why}
        )
        assert refused.status_code in (400, 422), why
    clash = await client.post("/api/v1/plan-templates", headers=world["bursar"], json={**PAY_IN_3, "name": "pay IN 3"})
    assert clash.status_code == 409
    listed = (await client.get("/api/v1/plan-templates", headers=world["bursar"])).json()
    assert [p["name"] for p in listed] == ["Pay in 3"]


async def test_a_parent_chooses_a_plan_from_the_link(client, world, term_running):
    await add_plan(client, world)
    await generate(client, world)
    token = await staff_link(client, world)
    child = ada((await page(client, token)).json(), world)
    [offer] = child["plan_offers"]
    assert offer["charge_kobo"] == 1_500 * NAIRA and offer["total_kobo"] == ADA_OWES + 1_500 * NAIRA
    dues = [i["due_date"] for i in offer["instalments"]]
    assert dues[0] == lagos_today().isoformat()  # day 0 has passed: due today
    assert len(dues) == 3 and sum(i["amount_kobo"] for i in offer["instalments"]) == offer["total_kobo"]

    chosen = await client.post(
        f"/api/v1/public/pay/{token}/plan",
        json={"invoice_id": offer["invoice_id"], "template_id": offer["template_id"]},
    )
    assert chosen.status_code == 200, chosen.text
    after = ada(chosen.json(), world)
    assert after["on_plan"] is True and after["plan_offers"] == []
    assert after["suggested_kobo"] == offer["instalments"][0]["amount_kobo"]  # pay the first instalment now
    bill = (await client.get(f"/api/v1/invoices/{offer['invoice_id']}", headers=world["bursar"])).json()
    assert any(line["description"] == "Instalment plan charge (Pay in 3)" for line in bill["lines"])
    assert bill["plan"]["note"] == "Pay in 3 (chosen by the parent)"
    again = await client.post(
        f"/api/v1/public/pay/{token}/plan",
        json={"invoice_id": offer["invoice_id"], "template_id": offer["template_id"]},
    )
    assert again.status_code == 409
    # Another family's bill can't be put on a plan through this link
    chidi_bill = (await client.get(f"/api/v1/students/{world['chidi'].id}/account", headers=world["bursar"])).json()
    stranger = await client.post(
        f"/api/v1/public/pay/{token}/plan",
        json={"invoice_id": chidi_bill["invoices"][0]["id"], "template_id": offer["template_id"]},
    )
    assert stranger.status_code == 404


async def test_who_may_choose_is_the_schools_call(client, world, term_running):
    await add_plan(client, world, name="Clean slate", eligibility="no_old_debts", charge_kind="none", charge_value=0)
    staff_only = await add_plan(
        client, world, name="By arrangement", eligibility="staff_only", charge_kind="none", charge_value=0
    )
    await generate(client, world)
    old = await client.post(
        "/api/v1/invoices",
        headers=world["bursar"],
        json={
            "student_id": str(world["ada"].id),
            "kind": "opening_balance",
            "lines": [{"description": "Balance from last session", "amount_kobo": 20_000 * NAIRA}],
        },
    )
    assert old.status_code == 201, old.text
    token = await staff_link(client, world)
    names = {
        c["first_name"]: [o["name"] for o in c["plan_offers"]] for c in (await page(client, token)).json()["children"]
    }
    assert names == {"Ada": [], "Bayo": ["Clean slate"]}  # Ada owes from before; nobody sees the staff-only plan

    account = (await client.get(f"/api/v1/students/{world['ada'].id}/account", headers=world["bursar"])).json()
    term_bill = next(i for i in account["invoices"] if i["kind"] == "term")
    staff_sees = (await client.get(f"/api/v1/invoices/{term_bill['id']}/plan-offers", headers=world["bursar"])).json()
    assert {o["name"] for o in staff_sees} == {"Clean slate", "By arrangement"}
    put_on = await client.post(
        f"/api/v1/invoices/{term_bill['id']}/plan/school-plan",
        headers=world["bursar"],
        json={"template_id": staff_only["id"]},
    )
    assert put_on.status_code == 201 and put_on.json()["plan"]["note"] == "By arrangement"


async def test_the_parent_portal_offers_and_chooses(client, world, term_running):
    await add_plan(client, world)
    await generate(client, world)
    me = bearer(await sign_in(client))
    child = (await client.get(f"/api/v1/parent/children/{world['ada'].id}", headers=me)).json()
    bill = child["invoices"][0]
    [offer] = bill["plan_offers"]
    chosen = await client.post(
        f"/api/v1/parent/invoices/{bill['id']}/plan", headers=me, json={"template_id": offer["template_id"]}
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["plan"] is not None and chosen.json()["balance_kobo"] == ADA_OWES + 1_500 * NAIRA


async def test_no_plans_once_the_term_is_over_or_closed_and_queries_are_fixed(client, world, db, connection):
    await add_plan(client, world)
    await generate(client, world)
    token = await staff_link(client, world)
    with QueryCounter(connection) as one:
        await page(client, token)
    await add_plan(
        client, world, name="Pay in 2", parts=[{"percent_bp": 6000, "day": 0}, {"percent_bp": 4000, "day": 45}]
    )
    with QueryCounter(connection) as many:
        await page(client, token)
    assert len(many.statements) == len(one.statements)
    scope_platform_wide(db)
    db.execute(
        text("UPDATE terms SET end_date = :e WHERE id = :id"),
        {"e": lagos_today() - timedelta(days=1), "id": world["terms"][0].id},
    )
    db.commit()
    assert all(c["plan_offers"] == [] for c in (await page(client, token)).json()["children"])


async def test_wardpay_charges_the_school_per_plan_once(client, world, db, term_running):
    await add_plan(client, world)
    await generate(client, world)
    token = await staff_link(client, world)
    for child in (await page(client, token)).json()["children"]:
        offer = child["plan_offers"][0]
        await client.post(
            f"/api/v1/public/pay/{token}/plan",
            json={"invoice_id": offer["invoice_id"], "template_id": offer["template_id"]},
        )
    platform = headers_for(db, make_user(db, None, Role.PLATFORM_ADMIN))
    db.commit()
    billed = await client.post("/api/v1/platform/billing/generate", headers=platform, json={"dry_run": False})
    assert billed.status_code == 200, billed.text
    bill = next(b for b in billed.json()["bills"] if b["school_id"] == str(world["school"].id))
    line = next(ln for ln in bill["lines"] if ln["kind"] == "instalment_plans")
    assert (line["quantity"], line["unit_kobo"]) == (2, 200 * NAIRA)  # Ada and Bayo
    scope_platform_wide(db)
    charged = db.execute(text("SELECT count(*) FROM payment_plans WHERE subscription_invoice_id IS NOT NULL")).scalar()
    assert charged == 2


async def test_staff_put_a_whole_family_on_a_plan_and_tell_the_parent(client, world, db, connection, term_running):
    staff_only = await add_plan(client, world, name="By arrangement", eligibility="staff_only")
    await generate(client, world)
    url = f"/api/v1/guardians/{world['guardian'].id}"
    offered = (await client.get(f"{url}/plan-offers", headers=world["bursar"])).json()
    assert {c["first_name"]: [o["name"] for o in c["offers"]] for c in offered} == {
        "Ada": ["By arrangement"],
        "Bayo": ["By arrangement"],
    }
    with QueryCounter(connection) as two_children:
        done = await client.post(f"{url}/school-plan", headers=world["bursar"], json={"template_id": staff_only["id"]})
    assert done.status_code == 201, done.text
    assert done.json()["told"] is True and done.json()["messages"] == 2
    assert set(done.json()["applied"]) == {str(world["ada"].id), str(world["bayo"].id)}
    scope_platform_wide(db)
    kinds = db.execute(text("SELECT kind, dedupe_key FROM message_log WHERE dedupe_key LIKE 'plan-agreed:%'")).all()
    assert len(kinds) == 2 and {k for k, _ in kinds} == {"reminder"}
    db.rollback()
    again = await client.post(f"{url}/school-plan", headers=world["bursar"], json={"template_id": staff_only["id"]})
    assert again.status_code == 409  # both already on a plan

    # Mr Okafor has one child and has turned reminders off: put on the plan, but not messaged
    other = f"/api/v1/guardians/{world['other'].id}"
    opted = await client.put(f"{other}/reminders", headers=world["admin"], json={"reminders": False})
    assert opted.status_code == 200, opted.text
    with QueryCounter(connection) as one_child:
        quiet = await client.post(
            f"{other}/school-plan", headers=world["bursar"], json={"template_id": staff_only["id"]}
        )
    assert quiet.status_code == 201 and quiet.json()["told"] is False and quiet.json()["messages"] == 0
    assert len(two_children.statements) >= len(one_child.statements)  # no statements per child


async def test_another_school_cant_put_our_families_on_plans(client, world, db, term_running):
    plan = await add_plan(client, world)
    await generate(client, world)
    stranger = headers_for(db, make_user(db, make_school(db, "Beta Academy"), Role.BURSAR))
    db.commit()
    url = f"/api/v1/guardians/{world['guardian'].id}"
    assert (await client.get(f"{url}/plan-offers", headers=stranger)).status_code == 404
    assert (
        await client.post(f"{url}/school-plan", headers=stranger, json={"template_id": plan["id"]})
    ).status_code == 404


async def test_wardpays_standard_charge_is_the_floor(client, world, db, term_running):
    # No charge set by the school: WardPay's standard, ₦300 a payment plus ₦200
    await add_plan(client, world, charge_kind="none", charge_value=0)
    # Too little for three payments (the standard is ₦1,100)
    low = await client.post(
        "/api/v1/plan-templates",
        headers=world["bursar"],
        json={**PAY_IN_3, "name": "Cheap", "charge_value": 1_000 * NAIRA},
    )
    assert low.status_code == 400 and "₦1,100" in low.json()["error"]["message"]
    # A percentage that works out below the standard is raised to it
    await add_plan(client, world, name="Tiny percent", charge_kind="percent", charge_value=1)
    await generate(client, world)
    token = await staff_link(client, world)
    offers = {o["name"]: o for o in ada((await page(client, token)).json(), world)["plan_offers"]}
    assert offers["Pay in 3"]["charge_kobo"] == 1_100 * NAIRA
    assert offers["Tiny percent"]["charge_kobo"] == 1_100 * NAIRA  # 0.01% of ₦155,000 is ₦15.50


async def test_payments_already_due_count_once(client, world, db):
    """Joining late: the parts already due merge into one payment, and the standard counts what's left."""
    today = lagos_today()
    scope_platform_wide(db)
    db.execute(
        text("UPDATE terms SET start_date = :s, end_date = :e WHERE id = :id"),
        {"s": today - timedelta(days=50), "e": today + timedelta(days=60), "id": world["terms"][0].id},
    )
    db.commit()
    await add_plan(client, world, charge_kind="none", charge_value=0)  # days 0, 40, 70: two have passed
    await generate(client, world)
    token = await staff_link(client, world)
    [offer] = ada((await page(client, token)).json(), world)["plan_offers"]
    assert len(offer["instalments"]) == 2 and offer["charge_kobo"] == 800 * NAIRA  # 2 x ₦300 + ₦200
    assert offer["instalments"][0]["due_date"] == today.isoformat()


async def test_the_rates_come_from_wardpays_pricing_plan(client, world, db, term_running):
    platform = headers_for(db, make_user(db, None, Role.PLATFORM_ADMIN))
    db.commit()
    default = next(p for p in (await client.get("/api/v1/platform/plans", headers=platform)).json() if p["is_default"])
    changed = await client.put(
        f"/api/v1/platform/plans/{default['id']}",
        headers=platform,
        json={
            "name": default["name"],
            "setup_fee_kobo": default["setup_fee_kobo"],
            "per_pupil_year_kobo": default["per_pupil_year_kobo"],
            "instalment_plan_fee_kobo": 0,
            "plan_charge_per_payment_kobo": 500 * NAIRA,
            "is_default": True,
        },
    )
    assert changed.status_code == 200, changed.text
    taken = await client.post(
        "/api/v1/platform/plans",
        headers=platform,
        json={"name": default["name"], "setup_fee_kobo": 0, "per_pupil_year_kobo": 0, "is_default": True},
    )
    assert taken.status_code == 409  # a clear answer, not a server error
    rates = (await client.get("/api/v1/plan-templates/rates", headers=world["bursar"])).json()
    assert rates == {"per_payment_kobo": 500 * NAIRA, "plan_fee_kobo": 0}
    await add_plan(client, world, charge_kind="none", charge_value=0)
    await generate(client, world)
    token = await staff_link(client, world)
    [offer] = ada((await page(client, token)).json(), world)["plan_offers"]
    assert offer["charge_kobo"] == 1_500 * NAIRA  # 3 x ₦500 + ₦0


async def test_the_school_sees_what_its_wardpay_bill_is_made_of(client, world, db, term_running):
    await add_plan(client, world)
    await generate(client, world)
    token = await staff_link(client, world)
    for child in (await page(client, token)).json()["children"]:
        offer = child["plan_offers"][0]
        await client.post(
            f"/api/v1/public/pay/{token}/plan",
            json={"invoice_id": offer["invoice_id"], "template_id": offer["template_id"]},
        )
    proprietor = headers_for(db, make_user(db, world["school"], Role.PROPRIETOR))
    platform = headers_for(db, make_user(db, None, Role.PLATFORM_ADMIN))
    db.commit()
    before = (await client.get("/api/v1/subscription", headers=proprietor)).json()
    assert before["plan"]["instalment_plan_fee_kobo"] == 200 * NAIRA
    assert (before["plans_since_last_bill"], before["plans_since_last_bill_kobo"]) == (2, 400 * NAIRA)

    assert (
        await client.post("/api/v1/platform/billing/generate", headers=platform, json={"dry_run": False})
    ).status_code == 200
    after = (await client.get("/api/v1/subscription", headers=proprietor)).json()
    assert after["plans_since_last_bill"] == 0
    [bill] = after["invoices"]
    kinds = [ln["kind"] for ln in bill["lines"]]
    assert kinds == sorted(kinds, key=["setup", "licence", "pupils", "instalment_plans", "discount"].index)
    assert kinds[-1] == "instalment_plans" and sum(ln["amount_kobo"] for ln in bill["lines"]) == bill["total_kobo"]
