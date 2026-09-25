"""A run that is not ``done`` delivers no rows (audit 2026-09-25, N-03).

Rows are committed while the job is ``enriching`` and only afterwards does the
quota reservation mark the ones past the plan's remaining allowance
OVER_QUOTA; billing commits with ``status = 'done'``, and a cancelled or failed
run is never billed. /results, /download and /export-url had no status gate,
so every addressed row was readable (and downloadable, since export_key is
written before the reservation) for as long as enrichment ran, and a Starter
account could read a whole county, cancel, and get the quota back.

Segments and the batch combined export already require ``status = 'done'``
(rule of 2026-09-08). These are the per-job paths catching up.
"""
from __future__ import annotations

import uuid

import pytest

from src.api.auth import create_secure_token, hash_password
from src.db.models import Job, Result, ScraperConfig, User


async def _run_with_one_addressed_row(db, status: str):
    user = User(
        id=str(uuid.uuid4()),
        email=f"undelivered_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="starter", records_used=0, records_limit=50,
    )
    db.add(user)
    await db.flush()
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Pierce tax", county="pierce",
        state="WA", record_type="tax_delinquent", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.flush()
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
        status=status, trigger="manual", export_key=f"exports/{user.id}/run.csv",
    )
    db.add(job)
    await db.flush()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job.id, user_id=user.id,
        party_name="UNBILLED OWNER", parcel_id="0123456789",
        property_address="1 Main St, Tacoma, WA 98402",
    ))
    await db.commit()
    return job.id, {"Authorization": f"Bearer {create_secure_token(user.id)}"}


@pytest.mark.asyncio
async def test_a_done_run_still_delivers_its_rows(db, client):
    # Positive control: without it, "0 rows" below could just mean the fixture is wrong.
    job_id, auth = await _run_with_one_addressed_row(db, "done")
    page = await client.get(f"/jobs/{job_id}/results", headers=auth)
    assert page.status_code == 200 and page.json()["total"] == 1
    dl = await client.get(f"/jobs/{job_id}/download", headers=auth)
    assert dl.status_code == 200 and "UNBILLED OWNER" in dl.text
    assert (await client.get(f"/jobs/{job_id}/export-url", headers=auth)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["enriching", "scraping", "failed", "cancelled"])
async def test_a_run_that_is_not_done_delivers_nothing(db, client, status):
    job_id, auth = await _run_with_one_addressed_row(db, status)

    page = await client.get(f"/jobs/{job_id}/results", headers=auth)
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 0 and body["items"] == []
    assert "UNBILLED OWNER" not in page.text

    dl = await client.get(f"/jobs/{job_id}/download", headers=auth)
    assert dl.status_code == 409 and "UNBILLED OWNER" not in dl.text

    url = await client.get(f"/jobs/{job_id}/export-url", headers=auth)
    assert url.status_code == 409
