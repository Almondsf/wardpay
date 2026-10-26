"""Term billing in the background: started by the request, finished by the worker, read back by the screen."""

import uuid

import pytest

from app.core import outbox
from app.modules.accounts.models import Role
from app.modules.fees import billing_runs
from tests.factories import headers_for, make_school, make_user
from tests.test_payments import world as world  # the fixture


def url(world):
    return f"/api/v1/terms/{world['terms'][0].id}/invoices/generate"


async def test_where_tasks_run_at_once_the_answer_is_the_result(client, world):
    response = await client.post(url(world), headers=world["bursar"], json={"dry_run": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending"] is False and body["invoices"] == 1 and body["run_id"]
    again = await client.get(f"/api/v1/billing-runs/{body['run_id']}", headers=world["bursar"])
    assert again.status_code == 200 and again.json()["invoices"] == 1


async def test_on_the_live_site_the_screen_waits_for_the_worker(client, world, monkeypatch):
    monkeypatch.setattr(outbox, "dispatch", lambda row_ids: None)  # the worker hasn't picked it up yet
    started = await client.post(url(world), headers=world["bursar"], json={"dry_run": False, "notify_parents": False})
    assert started.status_code == 202 and started.json()["pending"] is True
    run_id = started.json()["run_id"]
    waiting = await client.get(f"/api/v1/billing-runs/{run_id}", headers=world["bursar"])
    assert waiting.status_code == 202
    assert billing_runs.run_school(world["school"].id, uuid.UUID(run_id)) == "done"
    done = await client.get(f"/api/v1/billing-runs/{run_id}", headers=world["bursar"])
    assert done.status_code == 200 and done.json()["invoices"] == 1 and done.json()["pending"] is False
    assert billing_runs.run_school(world["school"].id, uuid.UUID(run_id)) == "done"  # twice: harmless


async def test_a_failure_is_reported_not_left_waiting(client, world, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database fell over")

    monkeypatch.setattr(billing_runs, "generate_term_invoices", broken)
    failed = await client.post(url(world), headers=world["bursar"], json={"dry_run": False})
    assert failed.status_code == 400 and failed.json()["error"]["code"] == "billing_failed"
    assert "try again" in failed.json()["error"]["message"]


async def test_previews_are_instant_and_runs_stay_in_their_school(client, world, db):
    preview = await client.post(url(world), headers=world["bursar"], json={"dry_run": True})
    assert preview.status_code == 200 and preview.json()["dry_run"] is True and preview.json()["run_id"] is None
    run = (await client.post(url(world), headers=world["bursar"], json={"dry_run": False})).json()
    stranger = headers_for(db, make_user(db, make_school(db, "Beta Academy"), Role.BURSAR))
    db.commit()
    assert (await client.get(f"/api/v1/billing-runs/{run['run_id']}", headers=stranger)).status_code == 404


@pytest.mark.parametrize("tables", [("students", "invoices"), ("not_a_table; DROP TABLE x",)])
def test_statistics_refresh_takes_known_tables_only(db, tables):
    from app.core.db import refresh_statistics

    refresh_statistics(db, tables)  # unknown names are dropped, never put into the SQL
