"""Platform billing: plans, termly subscription bills, payments, and what schools and admins can see."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.tenancy import scope_platform_wide
from app.modules.accounts.models import Role
from app.modules.platform.billing import termly
from app.modules.platform.models import Plan
from app.modules.schools.models import School, SchoolStatus, Term
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

NAIRA = 100
STANDARD = (50_000 * NAIRA, 1_500 * NAIRA)  # set-up, per pupil per year


def school_with_pupils(db, name, pupils, *, current_term=1):
    school = make_school(db, name)
    _, terms = make_calendar(school, current_term=current_term)
    level = make_levels(school)[0]
    for i in range(pupils):
        make_student(school, terms[max(current_term, 1) - 1], level.arms[0], first=f"P{i}")
    return school, terms


@pytest.fixture
def world(db):
    alpha, alpha_terms = school_with_pupils(db, "Alpha College", 3)
    beta, _ = school_with_pupils(db, "Beta Academy", 2)
    no_term, _ = school_with_pupils(db, "Gamma School", 0, current_term=0)
    admin = make_user(db, None, Role.PLATFORM_ADMIN)
    alpha_bursar, beta_bursar = make_user(db, alpha, Role.BURSAR), make_user(db, beta, Role.BURSAR)
    alpha_proprietor = make_user(db, alpha, Role.PROPRIETOR, email="owner@alpha.example.com")
    db.commit()
    return {
        "alpha": alpha,
        "alpha_terms": alpha_terms,
        "beta": beta,
        "gamma": no_term,
        "admin": headers_for(db, admin),
        "alpha_bursar": headers_for(db, alpha_bursar),
        "beta_bursar": headers_for(db, beta_bursar),
        "alpha_proprietor": alpha_proprietor,
    }


async def generate(client, world, dry_run=False):
    response = await client.post("/api/v1/platform/billing/generate", headers=world["admin"], json={"dry_run": dry_run})
    assert response.status_code == 200, response.text
    return response.json()


async def invoices(client, world, **query):
    response = await client.get("/api/v1/platform/billing/invoices", headers=world["admin"], params=query)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_a_third_of_the_yearly_fee_per_term():
    assert termly(150_000) == 50_000
    assert termly(100) == 33  # 33.33 kobo
    assert termly(200) == 67  # 66.67 rounds up
    assert termly(0) == 0


async def test_first_bill_has_the_setup_fee_and_pupils(client, world):
    preview = await generate(client, world, dry_run=True)
    assert preview["dry_run"] and preview["already_billed"] == 0
    assert "Gamma School" in preview["no_current_term"]
    alpha = next(b for b in preview["bills"] if b["school_name"] == "Alpha College")
    setup, per_year = STANDARD
    assert [(ln["kind"], ln["quantity"], ln["unit_kobo"]) for ln in alpha["lines"]] == [
        ("setup", 1, setup),
        ("pupils", 3, per_year // 3),
    ]
    assert alpha["total_kobo"] == setup + 3 * per_year // 3
    assert await invoices(client, world) == []  # a preview saves nothing

    done = await generate(client, world)
    numbers = sorted(i["number"] for i in await invoices(client, world))
    assert len(done["bills"]) == len(numbers) >= 2 and numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    again = await generate(client, world)
    assert again["bills"] == [] and again["already_billed"] == len(numbers)

    # The proprietor is emailed the bill
    from app.core import email

    sent = [m for m in email.sent_messages if m.to == "owner@alpha.example.com"]
    assert sent and "invoice" in sent[-1].subject and "₦" in sent[-1].body


async def test_next_term_only_bills_pupils(client, world, db):
    await generate(client, world)
    alpha, terms = world["alpha"], world["alpha_terms"]
    with tenant_writer() as w:
        w.execute(update(Term).where(Term.school_id == alpha.id).values(is_current=False))
        w.execute(update(Term).where(Term.id == terms[1].id).values(is_current=True))
    # Pupils move into the second term (one left)
    level = make_levels(alpha, {"JSS 9": ["A"]}, first_rank=50)[0]
    for i in range(2):
        make_student(alpha, terms[1], level.arms[0], first=f"T2-{i}")
    db.commit()
    second = next(b for b in (await generate(client, world))["bills"] if b["school_name"] == "Alpha College")
    assert [(ln["kind"], ln["quantity"]) for ln in second["lines"]] == [("pupils", 2)]
    assert second["period"] == "2026/2027 Second term"


async def test_a_schools_own_plan_is_used(client, world, db):
    created = await client.post(
        "/api/v1/platform/plans",
        headers=world["admin"],
        json={"name": "Small school", "setup_fee_kobo": 0, "per_pupil_year_kobo": 300 * NAIRA},
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["per_pupil_term_kobo"] == 100 * NAIRA and plan["is_default"] is False
    moved = await client.put(
        f"/api/v1/platform/schools/{world['beta'].id}/plan", headers=world["admin"], json={"plan_id": plan["id"]}
    )
    assert moved.status_code == 200 and moved.json()["school"]["plan_name"] == "Small school"
    beta = next(b for b in (await generate(client, world))["bills"] if b["school_name"] == "Beta Academy")
    assert [(ln["kind"], ln["quantity"], ln["unit_kobo"]) for ln in beta["lines"]] == [("pupils", 2, 100 * NAIRA)]


async def test_suspended_and_pending_schools_are_not_billed(client, world, db):
    scope_platform_wide(db)
    db.execute(update(School).where(School.id == world["beta"].id).values(status=SchoolStatus.SUSPENDED))
    db.commit()
    billed = {b["school_name"] for b in (await generate(client, world, dry_run=True))["bills"]}
    assert "Beta Academy" not in billed and "Alpha College" in billed


async def test_generation_is_a_fixed_number_of_queries(client, world, db, connection):
    with QueryCounter(connection) as small:
        await generate(client, world, dry_run=True)
    for i in range(6):
        school_with_pupils(db, f"More {i}", 2)
    db.commit()
    with QueryCounter(connection) as big:
        await generate(client, world, dry_run=True)
    assert big.count == small.count


# --- Payments ---------------------------------------------------------------------------------------


async def test_payments_up_to_the_balance_then_paid(client, world):
    await generate(client, world)
    [inv] = await invoices(client, world, school_id=str(world["alpha"].id))
    url = f"/api/v1/platform/billing/invoices/{inv['id']}"
    part = await client.post(
        f"{url}/payments", headers=world["admin"], json={"amount_kobo": 10_000 * NAIRA, "received_on": "2026-10-01"}
    )
    assert part.json()["invoice"]["balance_kobo"] == inv["total_kobo"] - 10_000 * NAIRA
    too_much = await client.post(
        f"{url}/payments", headers=world["admin"], json={"amount_kobo": inv["total_kobo"], "received_on": "2026-10-01"}
    )
    assert too_much.status_code == 400 and too_much.json()["error"]["field"] == "amount_kobo"
    future = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    later = await client.post(
        f"{url}/payments", headers=world["admin"], json={"amount_kobo": 100, "received_on": future}
    )
    assert later.status_code == 400
    rest = await client.post(
        f"{url}/payments",
        headers=world["admin"],
        json={"amount_kobo": inv["total_kobo"] - 10_000 * NAIRA, "received_on": "2026-10-02", "reference": "NIP/9"},
    )
    detail = rest.json()
    assert detail["invoice"]["status"] == "paid" and len(detail["payments"]) == 2
    assert [i["id"] for i in await invoices(client, world, status="paid")] == [inv["id"]]
    # A paid invoice can't be voided
    assert (await client.post(f"{url}/void", headers=world["admin"], json={"reason": "Oops"})).status_code == 409


async def test_void_lets_the_term_be_billed_again(client, world):
    await generate(client, world)
    [inv] = await invoices(client, world, school_id=str(world["alpha"].id))
    voided = await client.post(
        f"/api/v1/platform/billing/invoices/{inv['id']}/void", headers=world["admin"], json={"reason": "Wrong plan"}
    )
    assert voided.json()["invoice"]["status"] == "void"
    rebilled = next(b for b in (await generate(client, world))["bills"] if b["school_name"] == "Alpha College")
    assert [ln["kind"] for ln in rebilled["lines"]] == ["setup", "pupils"]  # nothing on the void one counts


async def test_subscription_lines_and_payments_are_append_only(client, world, db):
    await generate(client, world)
    for statement in ("UPDATE subscription_lines SET amount_kobo = 1", "DELETE FROM subscription_lines"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(statement))
        db.rollback()


# --- The school's view, and who can see what ----------------------------------------------------------


async def test_school_sees_its_plan_bills_and_pdf_only(client, world):
    await generate(client, world)
    mine = (await client.get("/api/v1/subscription", headers=world["alpha_bursar"])).json()
    assert mine["plan"]["name"] == "Standard" and mine["pupils_this_term"] == 3
    assert mine["next_term_estimate_kobo"] == 3 * STANDARD[1] // 3
    [invoice] = mine["invoices"]
    assert invoice["school_name"] == "Alpha College" and invoice["status"] == "unpaid"
    pdf = await client.get(f"/api/v1/subscription/invoices/{invoice['id']}/pdf", headers=world["alpha_bursar"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # Beta's bursar can't reach Alpha's invoice
    assert (
        await client.get(f"/api/v1/subscription/invoices/{invoice['id']}/pdf", headers=world["beta_bursar"])
    ).status_code == 404
    assert all(
        i["school_name"] == "Beta Academy"
        for i in (await client.get("/api/v1/subscription", headers=world["beta_bursar"])).json()["invoices"]
    )


async def test_platform_endpoints_are_for_platform_admins(client, world):
    for method, path in (
        ("get", "/api/v1/platform/overview"),
        ("get", "/api/v1/platform/plans"),
        ("get", "/api/v1/platform/billing/invoices"),
        ("get", "/api/v1/platform/lookup?q=alpha"),
        ("post", "/api/v1/platform/billing/generate"),
    ):
        response = await getattr(client, method)(
            path, headers=world["alpha_bursar"], **({"json": {}} if method == "post" else {})
        )
        assert response.status_code == 403, path
    assert (await client.get("/api/v1/platform/overview")).status_code == 401


# --- Plans --------------------------------------------------------------------------------------------


async def test_plans_one_default_and_audited(client, world, db):
    listed = (await client.get("/api/v1/platform/plans", headers=world["admin"])).json()
    standard = next(p for p in listed if p["name"] == "Standard")
    assert standard["is_default"] and standard["schools"] >= 3
    body = {k: standard[k] for k in ("name", "setup_fee_kobo", "per_pupil_year_kobo")}
    off = await client.put(
        f"/api/v1/platform/plans/{standard['id']}", headers=world["admin"], json={**body, "is_default": False}
    )
    assert off.status_code == 409  # another plan must become the default first
    new = await client.post(
        "/api/v1/platform/plans",
        headers=world["admin"],
        json={**body, "name": "Premium", "per_pupil_year_kobo": 3_000 * NAIRA, "is_default": True},
    )
    assert new.status_code == 201 and new.json()["is_default"] is True
    after = {
        p["name"]: p["is_default"] for p in (await client.get("/api/v1/platform/plans", headers=world["admin"])).json()
    }
    assert after == {"Premium": True, "Standard": False}
    duplicate = await client.post("/api/v1/platform/plans", headers=world["admin"], json={**body, "name": "Premium"})
    assert duplicate.status_code == 409
    negative = await client.post(
        "/api/v1/platform/plans", headers=world["admin"], json={**body, "name": "Bad", "setup_fee_kobo": -1}
    )
    assert negative.status_code == 422
    scope_platform_wide(db)
    assert db.scalar(select(Plan.name).where(Plan.is_default.is_(True))) == "Premium"
    trail = db.execute(text("SELECT count(*) FROM platform_audit_log WHERE action = 'platform.plan_saved'")).scalar()
    assert trail == 1  # only the save that happened (refused changes leave no entry)


# --- Console: overview, schools, lookup ------------------------------------------------------------------


async def test_overview_and_schools_list(client, world):
    await generate(client, world)
    o = (await client.get("/api/v1/platform/overview", headers=world["admin"])).json()
    assert o["schools_active"] >= 3 and o["pupils"] >= 5
    assert o["subscription_outstanding_kobo"] == o["subscription_billed_kobo"] > 0
    listing = (await client.get("/api/v1/platform/schools?search=alpha", headers=world["admin"])).json()
    [alpha] = listing["items"]
    assert alpha["pupils"] == 3 and alpha["plan_name"] == "Standard (default)"
    assert alpha["proprietor_email"] == "owner@alpha.example.com" and alpha["subscription_owed_kobo"] > 0
    detail = (await client.get(f"/api/v1/platform/schools/{world['alpha'].id}", headers=world["admin"])).json()
    assert detail["students_active"] == 3 and {s["role"] for s in detail["staff"]} == {"bursar", "proprietor"}


async def test_lookup_shows_initials_only_and_is_audited(client, world, db):
    from tests.test_parents import link_guardian
    from tests.test_payments import connect_gateway, pay_cash

    school = make_school(db, "Delta School")
    _, terms = make_calendar(school)
    student = make_student(school, terms[0], make_levels(school)[0].arms[0], first="Chiamaka", last="Okafor")
    link_guardian(school, [student])
    connect_gateway(school)
    bursar, admin = make_user(db, school, Role.BURSAR), make_user(db, school, Role.SCHOOL_ADMIN)
    db.commit()
    paid = await pay_cash(
        client,
        {"student": student, "admin": headers_for(db, admin), "bursar": headers_for(db, bursar)},
        5_000,
        reference="TLR-778899",
    )
    hits = (await client.get("/api/v1/platform/lookup", headers=world["admin"], params={"q": "TLR-778899"})).json()
    [hit] = [h for h in hits if h["kind"] == "payment"]
    assert hit["school_name"] == "Delta School" and "C.O." in hit["title"]
    assert "Chiamaka" not in str(hits) and "Okafor" not in str(hits)
    assert paid["payment_id"]
    scope_platform_wide(db)
    logged = db.execute(
        text("SELECT changes FROM platform_audit_log WHERE action = 'platform.support_lookup'")
    ).scalar()
    assert logged["query"] == "TLR-778899"
