"""`ResultsPage.enriching` must go false once a job is over.

The flag was derived only from log text ("Enrichment complete%" / "No records with
parcel%"). Two completion lines added later matched neither: "Address enrichment
partly complete..." (mailing lookups deferred to the background sweep) and
"Address enrichment failed...". A finished job with deferred mailing therefore
reported enriching=true forever, and the results page polled every 5 seconds for as
long as it stayed open. Real DB, real rows, real endpoint.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from src.db.models import Job, JobLog, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio


async def _job_with_parcel(db, user: User, config: ScraperConfig, *, status: str,
                           last_log: str) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status=status, trigger="manual", record_count=1))
    await db.commit()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=user.id, party_name="DOE JANE",
        parcel_id="00522400008900", property_address="22801 64TH PL W",
        mailing_address=None, is_duplicate=False,
        enrichment_data={"mailing_lookup_deferred": True},
    ))
    db.add(JobLog(job_id=job_id, level="info", message=last_log))
    await db.commit()
    return job_id


async def _enriching(client: AsyncClient, job_id: str, token: str) -> bool:
    resp = await client.get(f"/jobs/{job_id}/results",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["enriching"]


@pytest.mark.parametrize("last_log", [
    "Address enrichment partly complete. Property addresses were added, "
    "and 12 mailing address lookups are still pending.",
    "Address enrichment failed. Leads were delivered without enriched fields.",
])
async def test_a_done_job_is_not_enriching_whatever_its_completion_line(
    client, db, starter_user, starter_token, scraper_config, last_log,
):
    job_id = await _job_with_parcel(db, starter_user, scraper_config,
                                    status="done", last_log=last_log)
    assert await _enriching(client, job_id, starter_token) is False


async def test_a_job_still_in_enrichment_reports_enriching(
    client, db, starter_user, starter_token, scraper_config,
):
    job_id = await _job_with_parcel(db, starter_user, scraper_config, status="enriching",
                                    last_log="Looking up property and mailing addresses...")
    assert await _enriching(client, job_id, starter_token) is True
