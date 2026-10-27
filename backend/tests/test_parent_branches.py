"""Parents with children in several branches of one school group: one sign-in, every branch, nothing else."""

import uuid

import pytest
from sqlalchemy import select

from app.core.tenancy import scope_platform_wide
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.schools.models import School, SchoolGroup, SchoolStatus
from app.modules.students.models import Guardian
from tests.factories import QueryCounter, make_calendar, make_levels, make_school, make_student
from tests.test_parents import PHONE, bearer, last_code, link_guardian, sign_in
from tests.test_parents import world as world  # the fixture: Alpha College, Tolu and Bisi, bills not yet made


def school_with_child(db, name: str, first: str, phone: str = PHONE):
    school = make_school(db, name)
    db.commit()
    _, terms = make_calendar(school)
    child = make_student(school, terms[0], make_levels(school)[0].arms[0], first=first)
    return school, link_guardian(school, [child], phone=phone)


@pytest.fixture
async def branches_world(db, client, world):
    term = world["terms"][0]
    await client.post(f"/api/v1/terms/{term.id}/invoices/generate", headers=world["bursar"], json={"dry_run": False})
    lekki, lekki_guardian = school_with_child(db, "Beta Campus", "Kemi")
    unrelated, _ = school_with_child(db, "Gamma School", "Femi")  # same phone, not in the group
    scope_platform_wide(db)
    group = SchoolGroup(id=uuid.uuid4(), name=f"Alpha Group {uuid.uuid4().hex[:4]}")
    db.add(group)
    db.flush()
    for school_id in (world["school"].id, lekki.id):
        db.get(School, school_id).group_id = group.id
    db.commit()
    return {**world, "lekki": lekki, "lekki_guardian": lekki_guardian, "unrelated": unrelated}


async def test_branches_of_one_group_sign_in_without_choosing(client, db, world):
    lekki, _ = school_with_child(db, "Beta Campus", "Kemi")
    scope_platform_wide(db)
    group = SchoolGroup(id=uuid.uuid4(), name=f"Alpha Group {uuid.uuid4().hex[:4]}")
    db.add(group)
    db.flush()
    for school_id in (world["school"].id, lekki.id):
        db.get(School, school_id).group_id = group.id
    db.commit()
    body = await sign_in(client)
    assert body["status"] == "signed_in"  # no "choose a school" step
    home = (await client.get("/api/v1/parent/home", headers=bearer(body))).json()
    assert home["school_name"] == "Alpha College"  # the first branch by name


async def test_home_lists_every_branch_and_switches_between_them(client, db, branches_world):
    w = branches_world
    body = await client.post("/api/v1/parent/auth/request-code", json={"phone": PHONE})
    assert body.status_code == 202
    choice = (await client.post("/api/v1/parent/auth/verify", json={"phone": PHONE, "code": last_code()})).json()
    # Gamma School is outside the group, so there's still a choice to make; pick Alpha
    alpha = next(s for s in choice["schools"] if s["school_name"] == "Alpha College")
    signed = (
        await client.post(
            "/api/v1/parent/auth/choose-school", json={"ticket": choice["ticket"], "guardian_id": alpha["guardian_id"]}
        )
    ).json()
    headers = bearer(signed)
    listed = (await client.get("/api/v1/parent/branches", headers=headers)).json()
    assert [(b["school_name"], b["children"], b["current"]) for b in listed] == [
        ("Alpha College", ["Bisi", "Tolu"], True),
        ("Beta Campus", ["Kemi"], False),
    ]
    assert listed[0]["owed_kobo"] == (155_000 + 185_000) * 100 and listed[1]["owed_kobo"] == 0
    assert "Gamma School" not in str(listed)

    switched = await client.post(
        "/api/v1/parent/auth/switch-branch", headers=headers, json={"guardian_id": listed[1]["guardian_id"]}
    )
    assert switched.status_code == 200, switched.text
    home = (await client.get("/api/v1/parent/home", headers=bearer(switched.json()))).json()
    assert home["school_name"] == "Beta Campus" and [c["first_name"] for c in home["children"]] == ["Kemi"]
    # The token for the old branch stops working: it named the other record
    assert (await client.get("/api/v1/parent/home", headers=headers)).status_code == 401
    # The refresh cookie keeps working, in the new branch
    refreshed = await client.post("/api/v1/parent/auth/refresh")
    assert refreshed.status_code == 200
    home = (await client.get("/api/v1/parent/home", headers=bearer(refreshed.json()))).json()
    assert home["school_name"] == "Beta Campus"
    scope_platform_wide(db)
    assert db.scalar(
        select(AuditLog.id).where(
            AuditLog.school_id == w["lekki"].id, AuditLog.action == AuditAction.PARENT_SWITCHED_BRANCH.value
        )
    )


async def test_never_to_another_parent_or_a_school_outside_the_group(client, db, branches_world):
    w = branches_world
    _, other_parent = school_with_child(db, "Beta Campus Two", "Zainab", phone="+2348090000000")
    scope_platform_wide(db)
    other_parent_school = db.get(School, other_parent.school_id)
    other_parent_school.group_id = db.get(School, w["lekki"].id).group_id  # in the group, but not this parent
    db.commit()
    choice = await sign_in_alpha(client)
    headers = bearer(choice)
    # This parent's own record at Gamma School, which isn't in the group
    gamma_guardian = db.scalar(select(Guardian.id).where(Guardian.school_id == w["unrelated"].id))
    for guardian_id in (other_parent.id, gamma_guardian, uuid.uuid4(), w["guardian"].id):
        refused = await client.post(
            "/api/v1/parent/auth/switch-branch", headers=headers, json={"guardian_id": str(guardian_id)}
        )
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "branch_invalid"
    names = [b["school_name"] for b in (await client.get("/api/v1/parent/branches", headers=headers)).json()]
    assert names == ["Alpha College", "Beta Campus"]
    # A suspended branch drops off and can't be switched to
    scope_platform_wide(db)
    db.get(School, w["lekki"].id).status = SchoolStatus.SUSPENDED
    db.commit()
    assert (await client.get("/api/v1/parent/branches", headers=headers)).json() == []  # one branch left
    refused = await client.post(
        "/api/v1/parent/auth/switch-branch", headers=headers, json={"guardian_id": str(w["lekki_guardian"].id)}
    )
    assert refused.status_code == 400


async def test_a_school_outside_any_group_has_no_branches(client, world):
    headers = bearer(await sign_in(client))
    assert (await client.get("/api/v1/parent/branches", headers=headers)).json() == []
    refused = await client.post(
        "/api/v1/parent/auth/switch-branch", headers=headers, json={"guardian_id": str(world["guardian"].id)}
    )
    assert refused.status_code == 400


async def test_branches_is_a_fixed_number_of_queries(client, db, branches_world, connection):
    w = branches_world
    headers = bearer(await sign_in_alpha(client))
    with QueryCounter(connection) as small:
        assert len((await client.get("/api/v1/parent/branches", headers=headers)).json()) == 2
    group_id = db.get(School, w["lekki"].id).group_id
    for name in ("Delta Campus", "Echo Campus"):
        school, _ = school_with_child(db, name, f"Child {name}")
        scope_platform_wide(db)
        db.get(School, school.id).group_id = group_id
        db.commit()
    with QueryCounter(connection) as big:
        assert len((await client.get("/api/v1/parent/branches", headers=headers)).json()) == 4
    assert big.count == small.count


async def sign_in_alpha(client):
    await client.post("/api/v1/parent/auth/request-code", json={"phone": PHONE})
    choice = (await client.post("/api/v1/parent/auth/verify", json={"phone": PHONE, "code": last_code()})).json()
    alpha = next(s for s in choice["schools"] if s["school_name"] == "Alpha College")
    response = await client.post(
        "/api/v1/parent/auth/choose-school", json={"ticket": choice["ticket"], "guardian_id": alpha["guardian_id"]}
    )
    return response.json()
