"""School groups: head-office staff working across one owner's branches, and nothing beyond them."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.outbox import OutboxTask
from app.core.tenancy import scope_platform_wide
from app.modules.accounts.models import Role, User
from app.modules.accounts.security import hash_password
from app.modules.audit.models import AuditAction, PlatformAuditLog
from app.modules.schools.models import SchoolGroup, SchoolStatus
from tests.factories import PASSWORD, headers_for, make_calendar, make_levels, make_school, make_student, make_user

GROUPS = "/api/v1/platform/groups"


def head_office(db, group: SchoolGroup, role: Role = Role.PROPRIETOR) -> User:
    user = User(
        id=uuid.uuid4(),
        group_id=group.id,
        role=role,
        full_name=f"Head office {role.value}",
        email=f"ho-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password(PASSWORD),
        mfa_enabled=False,
    )
    db.add(user)
    db.flush()
    return user


def school_with_pupil(db, name: str):
    school = make_school(db, name)
    db.commit()
    _, terms = make_calendar(school)
    levels = make_levels(school, {"JSS 1": ["A"]})
    return school, make_student(school, terms[0], levels[0].arms[0], first=name.split()[0])


@pytest.fixture
def groups_world(db, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "enforce_mfa", False)
    scope_platform_wide(db)
    ikeja, ikeja_pupil = school_with_pupil(db, "Ikeja Campus")
    lekki, lekki_pupil = school_with_pupil(db, "Lekki Campus")
    other, other_pupil = school_with_pupil(db, "Other School")
    group = SchoolGroup(id=uuid.uuid4(), name=f"Bright Stars {uuid.uuid4().hex[:4]}")
    db.add(group)
    db.flush()
    ikeja.group_id = lekki.group_id = group.id
    owner = head_office(db, group)
    central_bursar = head_office(db, group, Role.BURSAR)
    ikeja_bursar = make_user(db, ikeja, Role.BURSAR, mfa=False)
    admin = make_user(db, None, Role.PLATFORM_ADMIN)
    db.commit()
    return {
        "group": group,
        "ikeja": ikeja,
        "lekki": lekki,
        "other": other,
        "pupils": {"ikeja": ikeja_pupil, "lekki": lekki_pupil, "other": other_pupil},
        "owner": owner,
        "central_bursar": central_bursar,
        "ikeja_bursar": ikeja_bursar,
        "admin": headers_for(db, admin),
    }


async def test_head_office_starts_in_the_first_branch_and_switches(client, db, groups_world):
    w = groups_world
    owner = headers_for(db, w["owner"])
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    assert me["group"]["name"] == w["group"].name
    assert [b["name"] for b in me["branches"]] == ["Ikeja Campus", "Lekki Campus"]
    assert me["school"]["name"] == "Ikeja Campus"
    assert "staff.manage" in me["capabilities"]  # the owner acts as each branch's proprietor
    pupil = f"/api/v1/students/{w['pupils']['ikeja'].id}"
    assert (await client.get(pupil, headers=owner)).status_code == 200
    assert (await client.get(f"/api/v1/students/{w['pupils']['lekki'].id}", headers=owner)).status_code == 404

    switched = await client.post("/api/v1/auth/branch", headers=owner, json={"school_id": str(w["lekki"].id)})
    assert switched.status_code == 200 and switched.json()["school"]["name"] == "Lekki Campus"
    assert (await client.get(f"/api/v1/students/{w['pupils']['lekki'].id}", headers=owner)).status_code == 200
    assert (await client.get(pupil, headers=owner)).status_code == 404  # one branch at a time
    scope_platform_wide(db)
    assert db.scalar(
        select(PlatformAuditLog.id).where(
            PlatformAuditLog.action == AuditAction.BRANCH_SWITCHED.value,
            PlatformAuditLog.actor_user_id == w["owner"].id,
        )
    )


async def test_never_into_a_school_outside_the_group(client, db, groups_world):
    w = groups_world
    owner = headers_for(db, w["owner"])
    for school_id in (w["other"].id, uuid.uuid4()):  # same answer for a real school and a made-up id
        refused = await client.post("/api/v1/auth/branch", headers=owner, json={"school_id": str(school_id)})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "branch_invalid"
    assert (await client.get(f"/api/v1/students/{w['pupils']['other'].id}", headers=owner)).status_code == 404
    # Branch staff stay in their branch: no switching, and the sister branch is invisible
    bursar = headers_for(db, w["ikeja_bursar"])
    me = (await client.get("/api/v1/auth/me", headers=bursar)).json()
    assert me["group"] is None and me["branches"] == []
    assert (
        await client.post("/api/v1/auth/branch", headers=bursar, json={"school_id": str(w["lekki"].id)})
    ).status_code == 403
    assert (await client.get(f"/api/v1/students/{w['pupils']['lekki'].id}", headers=bursar)).status_code == 404


async def test_central_bursar_has_a_bursars_permissions_only(client, db, groups_world):
    w = groups_world
    bursar = headers_for(db, w["central_bursar"])
    me = (await client.get("/api/v1/auth/me", headers=bursar)).json()
    assert "staff.manage" not in me["capabilities"] and "payments.view" in me["capabilities"]
    invite = {"email": "new@example.com", "full_name": "New Person", "role": "school_admin"}
    assert (await client.post("/api/v1/staff", headers=bursar, json=invite)).status_code == 403
    assert (await client.get(GROUPS, headers=bursar)).status_code == 403  # not the platform's


async def test_losing_the_branch_ends_access_to_it(client, db, groups_world):
    w = groups_world
    owner = headers_for(db, w["owner"])
    removed = await client.delete(f"{GROUPS}/{w['group'].id}/branches/{w['ikeja'].id}", headers=w["admin"])
    assert removed.status_code == 200 and [b["name"] for b in removed.json()["branches"]] == ["Lekki Campus"]
    blocked = await client.get(f"/api/v1/students/{w['pupils']['ikeja'].id}", headers=owner)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "branch_required"
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    assert me["school"] is None and [b["name"] for b in me["branches"]] == ["Lekki Campus"]
    # A suspended branch is off the menu too
    scope_platform_wide(db)
    db.get(type(w["lekki"]), w["lekki"].id).status = SchoolStatus.SUSPENDED
    db.commit()
    assert (await client.get("/api/v1/auth/me", headers=owner)).json()["branches"] == []
    refused = await client.post("/api/v1/auth/branch", headers=owner, json={"school_id": str(w["lekki"].id)})
    assert refused.status_code == 400


async def test_platform_admin_sets_up_a_group(client, db, groups_world):
    w = groups_world
    created = await client.post(GROUPS, headers=w["admin"], json={"name": "  Royal   Schools "})
    assert created.status_code == 201 and created.json()["name"] == "Royal Schools"
    group_id = created.json()["id"]
    assert (await client.post(GROUPS, headers=w["admin"], json={"name": "Royal Schools"})).status_code == 409
    added = await client.post(
        f"{GROUPS}/{group_id}/branches", headers=w["admin"], json={"school_id": str(w["other"].id)}
    )
    assert [b["name"] for b in added.json()["branches"]] == ["Other School"]
    taken = await client.post(
        f"{GROUPS}/{group_id}/branches", headers=w["admin"], json={"school_id": str(w["ikeja"].id)}
    )
    assert taken.status_code == 409  # already Bright Stars' branch
    invite = {"email": "Owner@Royal.example", "full_name": "Chief Royal", "role": "proprietor"}
    invited = await client.post(f"{GROUPS}/{group_id}/staff", headers=w["admin"], json=invite)
    assert invited.status_code == 201
    [staff] = invited.json()["staff"]
    assert staff["email"] == "owner@royal.example" and staff["invited"] is True
    bad_role = {**invite, "email": "x@royal.example", "role": "school_admin"}
    assert (await client.post(f"{GROUPS}/{group_id}/staff", headers=w["admin"], json=bad_role)).status_code == 422
    scope_platform_wide(db)
    queued = db.scalars(select(OutboxTask).where(OutboxTask.task_name == "accounts.send_password_link")).all()
    assert any(t.payload == {"user_id": staff["id"], "purpose": "invite"} for t in queued)
    listing = (await client.get(GROUPS, headers=w["admin"])).json()
    assert {"name": "Royal Schools", "branches": 1, "staff": 1} in [
        {k: g[k] for k in ("name", "branches", "staff")} for g in listing
    ]
    detail = (await client.get(f"/api/v1/platform/schools/{w['other'].id}", headers=w["admin"])).json()
    assert detail["group_name"] == "Royal Schools"


async def test_deactivating_head_office_signs_them_out(client, db, groups_world):
    w = groups_world
    owner = headers_for(db, w["owner"])
    path = f"{GROUPS}/{w['group'].id}/staff/{w['owner'].id}"
    assert (await client.post(f"{path}/deactivate", headers=w["admin"])).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=owner)).status_code == 401
    assert (await client.post(f"{path}/password-link", headers=w["admin"])).status_code == 409
    assert (await client.post(f"{path}/reactivate", headers=w["admin"])).json()["staff"][1]["is_active"] is True
    assert (await client.post(f"{path}/password-link", headers=w["admin"])).json() == {"sent": "reset"}
    # Another group's id in the path finds nobody
    stranger = f"{GROUPS}/{uuid.uuid4()}/staff/{w['owner'].id}"
    assert (await client.post(f"{stranger}/password-link", headers=w["admin"])).status_code == 404
    assert (await client.post(f"{stranger}/deactivate", headers=w["admin"])).status_code == 404


async def test_only_platform_admins_manage_groups(client, db, groups_world):
    w = groups_world
    for user in (w["owner"], w["ikeja_bursar"]):
        assert (await client.get(GROUPS, headers=headers_for(db, user))).status_code == 403


def test_head_office_is_an_owner_or_bursar_of_a_group_not_a_school(db, groups_world):
    w = groups_world
    for school_id, group_id, role in (
        (None, w["group"].id, Role.SCHOOL_ADMIN),  # head office is proprietor or bursar only
        (w["ikeja"].id, w["group"].id, Role.PROPRIETOR),  # not both a school and a group
        (None, w["group"].id, Role.PLATFORM_ADMIN),
    ):
        db.begin_nested()
        db.add(
            User(
                id=uuid.uuid4(),
                school_id=school_id,
                group_id=group_id,
                role=role,
                full_name="X",
                email=f"{uuid.uuid4().hex}@e.com",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
