"""Late delivery, the API side (Phase C1): what the user can read about a late-delivered lead.

A code violation held at its run and delivered later by src/workers/cv_late_delivery.py is
an ordinary lead of its run now; the Results list says when it was delivered
(delivered_late_at, from the row's stamp) and the page counts them (late_delivered_count),
while the run's own frozen counts never move. The notification type and its settings toggle
exist so the frontend can ship before the worker starts emitting (C3). Real DB, real endpoints.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.api.routes.jobs import LATE_DELIVERY_KEY, _delivered_late_at
from src.config.constants import NotificationType
from src.db.models import Job, Result, ScraperConfig
from tests.test_lead_qualification import seattle

STAMP = {"delivered_at": "2026-10-31T02:25:00.250000+00:00", "policy": "code_violation_v1"}


async def _done_job(db, user) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type="code_violation", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(cfg)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done", trigger="manual",
               record_count=1, billed_count=1, export_key=f"exports/{uuid.uuid4()}.csv"))
    await db.commit()
    await db.execute(text("UPDATE jobs SET billing_applied_at = now() WHERE id = :j"), {"j": job_id})
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, *, late=None, **kw) -> str:
    spec = seattle(**kw)
    ed = dict(spec.pop("enrichment_data"))
    if late is not None:
        ed[LATE_DELIVERY_KEY] = late
    rid = str(uuid.uuid4())
    spec = {k: v for k, v in spec.items() if k not in ("phone", "email")}
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, enrichment_data=ed, is_duplicate=False,
                  dedup_hash=uuid.uuid4().hex, **spec))
    await db.commit()
    return rid


async def _page(client, job_id, token, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params,
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_key_is_the_workers_key():
    from src.workers.cv_late_delivery import LATE_KEY

    assert LATE_DELIVERY_KEY == LATE_KEY
    assert NotificationType.LATE_DELIVERY.value == "late_delivery"


@pytest.mark.parametrize("stamp,expected", [
    (STAMP, datetime(2026, 10, 31, 2, 25, 0, 250000, tzinfo=UTC)),
    ({"delivered_at": "2026-10-31T02:25:00"}, datetime(2026, 10, 31, 2, 25, tzinfo=UTC)),
    ({"delivered_at": "not a date"}, None),
    ({"delivered_at": 12345}, None),
    ("a string", None),
    (None, None),
])
def test_the_delivery_time_is_read_defensively(stamp, expected):
    ed = {} if stamp is None else {LATE_DELIVERY_KEY: stamp}
    assert _delivered_late_at(ed) == expected
    assert _delivered_late_at(None) is None and _delivered_late_at("x") is None


@pytest.mark.asyncio
async def test_a_late_lead_says_when_and_the_page_counts_them(client, db, business_user, business_token):
    job = await _done_job(db, business_user)
    late = await _row(db, job, business_user.id, late=STAMP)
    normal = await _row(db, job, business_user.id, property_address="2 OTHER ST")
    await _row(db, job, business_user.id, late=STAMP, property_address="3 COPY ST")   # a duplicate copy
    await db.execute(text("UPDATE results SET is_duplicate = true, duplicate_reason = 'same_run' "
                          "WHERE property_address = '3 COPY ST' AND job_id = :j"), {"j": job})
    await db.commit()
    numbers = (await db.execute(text("SELECT record_count, billed_count FROM jobs WHERE id = :j"),
                                {"j": job})).one()

    page = await _page(client, job, business_token, category="new")

    by_id = {item["id"]: item for item in page["items"]}
    assert by_id[late]["delivered_late_at"].startswith("2026-10-31T02:25:00")
    assert by_id[normal]["delivered_late_at"] is None
    assert page["late_delivered_count"] == 1                       # duplicates never counted
    assert tuple((await db.execute(text("SELECT record_count, billed_count FROM jobs WHERE id = :j"),
                                   {"j": job})).one()) == tuple(numbers)


@pytest.mark.asyncio
async def test_another_accounts_late_leads_never_count(client, db, business_user, starter_user, starter_token):
    theirs = await _done_job(db, business_user)
    await _row(db, theirs, business_user.id, late=STAMP)
    mine = await _done_job(db, starter_user)
    await _row(db, mine, starter_user.id)
    assert (await _page(client, mine, starter_token, category="new"))["late_delivered_count"] == 0


@pytest.mark.asyncio
async def test_a_null_or_scalar_stamp_and_another_users_row_never_count(
        client, db, business_user, starter_user, business_token):
    """Codex P2/P3: only an object stamp counts, and only this account's rows of the job."""
    job = await _done_job(db, business_user)
    await _row(db, job, business_user.id, late=STAMP)
    await _row(db, job, business_user.id, property_address="2 NULL ST")
    await _row(db, job, business_user.id, property_address="3 SCALAR ST")
    await db.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                          "'{\"late_delivery\": null}')::json WHERE property_address = '2 NULL ST'"))
    await db.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                          "'{\"late_delivery\": \"yes\"}')::json WHERE property_address = '3 SCALAR ST'"))
    await db.commit()
    await _row(db, job, starter_user.id, late=STAMP, property_address="4 FOREIGN ST")   # same job, other account
    page = await _page(client, job, business_token, category="new")
    assert page["late_delivered_count"] == 1
    assert sum(1 for i in page["items"] if i["delivered_late_at"]) == 1


@pytest.mark.asyncio
async def test_the_late_delivery_notice_can_be_turned_off(client, business_token):
    resp = await client.put("/auth/notification-preferences", json={"late_delivery": False},
                            headers={"Authorization": f"Bearer {business_token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_prefs"]["late_delivery"] is False
