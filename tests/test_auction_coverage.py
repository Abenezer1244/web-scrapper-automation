"""A run with no auction dates must say WHICH kind of "no".

The Auction Date / Principal Owing columns render for EVERY pre_foreclosure run (the
record type is the rule, so they cannot vanish on a run that matched zero). That is a
deliberate choice, and its cost is that a run over recent recordings shows two full
columns of blanks with no way to tell "we failed to find it" from "the notice cannot
legally have been published yet".

Owner report, 2026-09-19: a King pre_foreclosure run over 08/20-09/18 recordings showed
Auction Date and Principal Owing as N/A on all 51 rows. Measured against prod, that was
correct: RCW 61.24.040 records a notice >= 90 days before the sale and publishes it
7-35 days before, so first publication lands ~55 days AFTER recording, and every one of
those leads was younger than that. 585 of King's 1,098 unmatched leads were in exactly
that state.

``auction_coverage`` splits the run so the page can say so. Asserted here against the
real DB and the real endpoint (conftest fixtures, no mocks).
"""
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from src.config.constants import AUCTION_PUBLICATION_LAG_DAYS
from src.db.models import Job, Result, ScraperConfig, User

NOW = datetime.now(UTC)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _prefc_config(db, user: User, county: str = "king") -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} pre-foreclosure",
        county=county, state="WA", record_type="pre_foreclosure",
        fields=["party_name", "parcel_id"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    return config


async def _job(db, user: User, config: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=str(job_id), user_id=user.id, scraper_config_id=config.id,
               status="done", created_at=NOW))
    await db.commit()
    return job_id


async def _row(db, job_id: str, user_id: str, *, recorded_days_ago: int | None,
               auction_in_days: int | None = None) -> str:
    rid = str(uuid.uuid4())
    recorded = (
        None if recorded_days_ago is None
        else (NOW - timedelta(days=recorded_days_ago)).strftime("%m/%d/%Y")
    )
    db.add(Result(
        id=rid, job_id=job_id, user_id=user_id,
        date_recorded=recorded, party_name="OWNER NAME",
        parcel_id=f"PARCEL-{uuid.uuid4().hex[:8]}",
        property_address="1 TEST ST", doc_type="NOTICE OF TRUSTEE SALE",
        auction_date=(None if auction_in_days is None
                      else (NOW + timedelta(days=auction_in_days)).date()),
    ))
    await db.commit()
    return rid


async def _coverage(client: AsyncClient, job_id: str, token: str) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_recent_recordings_are_reported_as_awaiting_publication(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    """The reported case: every lead recorded inside the statutory publication
    window. Blank is correct and the run must not read as a failure."""
    config = await _prefc_config(db, starter_user)
    job_id = await _job(db, starter_user, config)
    for days in (1, 10, AUCTION_PUBLICATION_LAG_DAYS - 1):
        await _row(db, job_id, starter_user.id, recorded_days_ago=days)

    page = await _coverage(client, job_id, starter_token)
    cov = page["auction_coverage"]
    assert cov == {"matched": 0, "awaiting_publication": 3, "no_notice_found": 0}
    # The columns still render, which is why the split has to exist.
    assert page["has_auction_data"] is True


async def test_old_recordings_with_nothing_found_are_reported_separately(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    config = await _prefc_config(db, starter_user)
    job_id = await _job(db, starter_user, config)
    for days in (AUCTION_PUBLICATION_LAG_DAYS, 120, 400):
        await _row(db, job_id, starter_user.id, recorded_days_ago=days)

    cov = (await _coverage(client, job_id, starter_token))["auction_coverage"]
    assert cov == {"matched": 0, "awaiting_publication": 0, "no_notice_found": 3}


async def test_a_mixed_run_counts_each_lead_exactly_once(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    config = await _prefc_config(db, starter_user)
    job_id = await _job(db, starter_user, config)
    await _row(db, job_id, starter_user.id, recorded_days_ago=130, auction_in_days=20)
    await _row(db, job_id, starter_user.id, recorded_days_ago=5)
    await _row(db, job_id, starter_user.id, recorded_days_ago=200)
    # No parseable recording date: cannot be called "not published yet", so it
    # falls to the honest bucket rather than flattering the run.
    await _row(db, job_id, starter_user.id, recorded_days_ago=None)

    page = await _coverage(client, job_id, starter_token)
    cov = page["auction_coverage"]
    assert cov == {"matched": 1, "awaiting_publication": 1, "no_notice_found": 2}
    assert sum(cov.values()) == page["total_scraped"]


async def test_the_boundary_day_belongs_to_no_notice_found(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    """Exactly at the lag, publication COULD have happened, so a blank is a real
    absence. One day younger and it could not have."""
    config = await _prefc_config(db, starter_user)
    job_id = await _job(db, starter_user, config)
    await _row(db, job_id, starter_user.id, recorded_days_ago=AUCTION_PUBLICATION_LAG_DAYS)
    await _row(db, job_id, starter_user.id,
               recorded_days_ago=AUCTION_PUBLICATION_LAG_DAYS - 1)

    cov = (await _coverage(client, job_id, starter_token))["auction_coverage"]
    assert cov["no_notice_found"] == 1
    assert cov["awaiting_publication"] == 1


async def test_non_preforeclosure_runs_get_no_coverage_block(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    """Auction data never applies to probate, so the page must not imply it does."""
    job_id = await _job(db, starter_user, scraper_config)  # record_type=probate
    await _row(db, job_id, starter_user.id, recorded_days_ago=3)

    page = await _coverage(client, job_id, starter_token)
    assert page["auction_coverage"] is None
    assert page["has_auction_data"] is False


async def test_coverage_is_scoped_to_the_caller(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    """Another account's rows can never be counted into this run's coverage."""
    config = await _prefc_config(db, starter_user)
    job_id = await _job(db, starter_user, config)
    await _row(db, job_id, starter_user.id, recorded_days_ago=5)

    other = User(
        id=str(uuid.uuid4()), email=f"other_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash="x" * 60, plan="pro", records_used=0, records_limit=1000,
    )
    db.add(other)
    await db.commit()
    # A row on the SAME job owned by someone else (defence in depth over RLS).
    await _row(db, job_id, other.id, recorded_days_ago=200)

    cov = (await _coverage(client, job_id, starter_token))["auction_coverage"]
    assert cov == {"matched": 0, "awaiting_publication": 1, "no_notice_found": 0}
