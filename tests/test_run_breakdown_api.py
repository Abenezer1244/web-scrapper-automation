"""The run-count breakdown on the API: GET /jobs, GET /jobs/{id}, GET /jobs/{id}/results.

Through the real HTTP client and the request's RLS session (get_rls_db). The last
test drops to the production app role inside a rolled-back transaction to prove the
live partition is bounded by row-level security itself, not only by its own
user filter.
"""
import logging
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from src.api.run_breakdown import read_partition_async
from src.db.models import Job, Result, ScraperConfig, User

ADDR = "5006 61ST STREET CT E"
SNAPSHOT = {
    "breakdown_dropped_before_save": 1, "breakdown_no_address": 2,
    "breakdown_same_run_merged": 0, "breakdown_already_delivered": 3,
    "breakdown_over_quota": 0, "breakdown_new": 4,
}
SNAPSHOT_FIELDS = {
    "dropped_before_save": 1, "no_address": 2, "same_run_merged": 0,
    "already_delivered": 3, "over_quota": 0, "new": 4,
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _job(db, user: User, config: ScraperConfig, **kw) -> str:
    job_id = str(uuid.uuid4())
    kw.setdefault("status", "done")
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               trigger="manual", **kw))
    await db.commit()
    return job_id


async def _rows(db, job_id, user_id, specs) -> None:
    for spec in specs:
        spec = dict(spec)
        spec.setdefault("is_duplicate", False)
        db.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=user_id, **spec))
    await db.commit()


def _snapshotted(**kw) -> dict:
    return {"records_found": 10, "record_count": 4, "billed_count": 4, **SNAPSHOT, **kw}


# Rows that partition to no_address 1, already_delivered 1, new 2 (persisted 4).
LIVE_ROWS = [
    {},
    {"property_address": ADDR, "is_duplicate": True, "duplicate_reason": "prior_run"},
    {"property_address": ADDR},
    {"mailing_address": "PO BOX 1"},
]
LIVE = {"no_address": 1, "same_run_merged": 0, "already_delivered": 1,
        "over_quota": 0, "new": 2}


# ── JobResponse: snapshot only ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_and_detail_carry_the_snapshot(
    client: AsyncClient, db, starter_user, starter_token, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config, **_snapshotted())

    detail = (await client.get(f"/jobs/{job_id}", headers=_auth(starter_token))).json()
    listed = next(j for j in (await client.get("/jobs", headers=_auth(starter_token))).json()
                  if j["id"] == job_id)
    for body in (detail, listed):
        assert body["breakdown"] == SNAPSHOT_FIELDS
        assert body["breakdown_basis"] == "snapshot"
    assert detail["stage_label"] == "Complete: 4 new leads"


@pytest.mark.asyncio
async def test_job_without_a_snapshot_has_no_breakdown_and_runs_no_query(
    client: AsyncClient, db, starter_user, starter_token, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config, records_found=4, record_count=2,
                        billed_count=2)
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)

    body = (await client.get(f"/jobs/{job_id}", headers=_auth(starter_token))).json()
    assert (body["breakdown"], body["breakdown_basis"]) == (None, None)


@pytest.mark.asyncio
async def test_a_partial_snapshot_is_not_shown_and_is_logged(
    client: AsyncClient, db, starter_user, starter_token, scraper_config, caplog,
):
    job_id = await _job(db, starter_user, scraper_config,
                        **_snapshotted(breakdown_over_quota=None))
    with caplog.at_level(logging.WARNING):
        body = (await client.get(f"/jobs/{job_id}", headers=_auth(starter_token))).json()
    assert (body["breakdown"], body["breakdown_basis"]) == (None, None)
    assert any("partial snapshot" in r.getMessage() for r in caplog.records)


# ── ResultsPage: snapshot, else live for a DONE run ──────────────────────────

async def _results(client, job_id, token) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_results_prefers_the_snapshot_over_live_rows(
    client: AsyncClient, db, starter_user, starter_token, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config, **_snapshotted())
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)   # a backfill moved the live view

    body = await _results(client, job_id, starter_token)
    assert body["breakdown"] == SNAPSHOT_FIELDS
    assert body["breakdown_basis"] == "snapshot"


@pytest.mark.asyncio
async def test_results_reads_live_for_a_done_run_without_a_snapshot(
    client: AsyncClient, db, starter_user, starter_token, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config, records_found=5, retry_count=0)
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)

    body = await _results(client, job_id, starter_token)
    assert body["breakdown_basis"] == "live"
    assert body["breakdown"] == {"dropped_before_save": 1, **LIVE}


@pytest.mark.asyncio
@pytest.mark.parametrize("job_kw", [
    {"records_found": None, "retry_count": 0},   # 92/101 recent prod runs
    {"records_found": 5, "retry_count": 1},      # saved rows span attempts
])
async def test_live_drop_count_is_unknown(
    client: AsyncClient, db, starter_user, starter_token, scraper_config, job_kw,
):
    job_id = await _job(db, starter_user, scraper_config, **job_kw)
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)

    body = await _results(client, job_id, starter_token)
    assert body["breakdown_basis"] == "live"
    assert body["breakdown"] == {"dropped_before_save": None, **LIVE}


@pytest.mark.asyncio
@pytest.mark.parametrize("job_kw", [
    {"status": "enriching", "records_found": 5},   # not finished
    {"records_found": 3},                          # 4 saved, only 3 found
])
async def test_results_has_no_breakdown_when_it_cannot_be_trusted(
    client: AsyncClient, db, starter_user, starter_token, scraper_config, job_kw,
):
    job_id = await _job(db, starter_user, scraper_config, **job_kw)
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)

    body = await _results(client, job_id, starter_token)
    assert (body["breakdown"], body["breakdown_basis"]) == (None, None)


@pytest.mark.asyncio
async def test_breakdown_is_raw_while_the_tab_applies_the_tax_cap(
    client: AsyncClient, db, starter_user, starter_token, scraper_config,
):
    """A tax row delinquent since long before the cap window is hidden from the
    already-delivered tab, but the breakdown partitions every saved row."""
    job_id = await _job(db, starter_user, scraper_config, records_found=2, retry_count=0)
    await _rows(db, job_id, starter_user.id, [
        {"property_address": ADDR, "is_duplicate": True, "duplicate_reason": "prior_run",
         "delinquent_bill_year": 2001},
        {"property_address": ADDR, "is_duplicate": True, "duplicate_reason": "prior_run"},
    ])

    body = await _results(client, job_id, starter_token)
    assert body["already_delivered_count"] == 1
    assert body["breakdown"]["already_delivered"] == 2


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_the_job_or_skew_its_counts(
    client: AsyncClient, db, starter_user, starter_token, business_user, business_token,
    scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config, records_found=5, retry_count=0)
    await _rows(db, job_id, starter_user.id, LIVE_ROWS)
    await _rows(db, job_id, business_user.id, [{"property_address": ADDR}] * 7)

    assert (await client.get(f"/jobs/{job_id}/results",
                             headers=_auth(business_token))).status_code == 404
    assert (await client.get(f"/jobs/{job_id}",
                             headers=_auth(business_token))).status_code == 404
    body = await _results(client, job_id, starter_token)
    assert body["breakdown"] == {"dropped_before_save": 1, **LIVE}


@pytest.mark.asyncio
async def test_row_level_security_bounds_the_live_partition_as_the_app_role(
    db, starter_user, business_user, scraper_config,
):
    """As bridgeleads_app with the tenant set to A, asking for B's rows returns
    nothing: the policy, not only the statement's own user filter, keeps them out."""
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [{"property_address": ADDR}] * 2)
    await _rows(db, job_id, business_user.id, [{"property_address": ADDR}] * 3)

    try:
        await db.execute(text(
            "DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
            "WHERE rolname = 'bridgeleads_app') THEN "
            "CREATE ROLE bridgeleads_app NOLOGIN NOBYPASSRLS; END IF; END $r$"
        ))
        await db.execute(text("GRANT USAGE ON SCHEMA public TO bridgeleads_app"))
        await db.execute(text("GRANT SELECT ON public.results TO bridgeleads_app"))
        await db.execute(text("SET LOCAL ROLE bridgeleads_app"))
        await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                         {"u": str(starter_user.id)})

        mine = await read_partition_async(db, job_id, starter_user.id)
        theirs = await read_partition_async(db, job_id, business_user.id)
    finally:
        await db.rollback()

    assert mine.new == 2
    assert theirs.persisted == 0
