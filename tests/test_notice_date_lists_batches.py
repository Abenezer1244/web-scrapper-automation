"""The auction-date stand-in rule on the batch combined view and the Lists surfaces.

Same owner decision as tests/test_notice_date.py: a date_recorded that is the auction
date a scraper stood in for a missing notice date is flagged, shown blank, exported
blank, and never matches a filing-date window. Lists runs hand-written SQL, so it uses
the rule rendered as raw SQL (``auction_date_fallback_sql``); the parity test below
proves that rendering agrees with the Python rule on the same inputs.

Real DB + real endpoints, no mocks.
"""
import csv
import io
import uuid

import pytest_asyncio
from sqlalchemy import text

import src.db.session as _db_session
from src.api.results_sort import auction_date_fallback_sql
from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig, User
from src.utils.source_dates import is_auction_date_fallback
from tests.test_notice_date import TRUSTEE, VECTORS


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _config_and_job(db, user: User, record_type: str, batch_id: str | None = None) -> Job:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, batch_id=batch_id, name=f"n-{record_type}",
        county="pierce", state="WA", record_type=record_type,
        fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(cfg)
    await db.flush()
    job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
              status="done", trigger="batch" if batch_id else "manual")
    db.add(job)
    await db.flush()
    return job


def _result(user: User, job: Job, party: str, recorded: str, enrichment: dict | None,
            property_key: str | None = None) -> Result:
    return Result(
        id=str(uuid.uuid4()), user_id=user.id, job_id=job.id, party_name=party,
        date_recorded=recorded, enrichment_data=enrichment, property_key=property_key,
        property_address=f"1 {party} ST", dedup_hash=uuid.uuid4().hex, is_duplicate=False,
    )


async def test_raw_sql_rendering_agrees_with_python_rule(db, starter_user):
    job = await _config_and_job(db, starter_user, "trustee_sale")
    ids = []
    for recorded, enrichment, _ in VECTORS:
        row = _result(starter_user, job, f"V{len(ids)}", recorded, enrichment)
        db.add(row)
        ids.append(row.id)
    await db.commit()

    async with _db_session.AsyncSessionLocal() as s:
        rows = (await s.execute(
            text(f"SELECT r.id, {auction_date_fallback_sql('r')} AS flag "
                 "FROM results r WHERE r.job_id = :job"),
            {"job": job.id},
        )).all()
    sql = {str(rid): flag for rid, flag in rows}
    for rid, (recorded, enrichment, expected) in zip(ids, VECTORS, strict=True):
        assert sql[rid] is expected, (recorded, enrichment)
        assert sql[rid] is is_auction_date_fallback(recorded, enrichment)


def test_raw_sql_rendering_refuses_a_non_alias():
    for bad in ("r; DROP TABLE results", "r.x", ""):
        try:
            auction_date_fallback_sql(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


@pytest_asyncio.fixture
async def auction_batch(db, starter_user):
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Auctions", state="WA",
        fields=[], enrichment=[], schedule={}, deliver={}, status="active",
        delivery_mode="everything",
    )
    db.add(batch)
    await db.flush()
    job = await _config_and_job(db, starter_user, "trustee_sale", batch_id=batch.id)
    db.add(_result(starter_user, job, "STAND IN", "10/9/2026", TRUSTEE("2026-10-09"), "WA|pierce|01"))
    db.add(_result(starter_user, job, "REAL NOTICE", "6/30/2026", TRUSTEE("2026-10-09"), "WA|pierce|02"))
    run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
                   status="done", child_job_ids=[job.id])
    db.add(run)
    await db.commit()
    return batch


async def test_batch_leads_flag_the_stand_in(client, starter_token, auction_batch):
    resp = await client.get(f"/batches/{auction_batch.id}/leads", headers=_auth(starter_token))

    assert resp.status_code == 200, resp.text
    flags = {lead["party_name"]: lead["date_is_auction_date"] for lead in resp.json()["leads"]}
    assert flags == {"STAND IN": True, "REAL NOTICE": False}


@pytest_asyncio.fixture
async def lists_rows(db, business_user):
    auctions = await _config_and_job(db, business_user, "trustee_sale")
    probate = await _config_and_job(db, business_user, "probate")
    db.add_all([
        _result(business_user, auctions, "STAND IN", "10/9/2026", TRUSTEE("2026-10-09"), "WA|pierce|11"),
        _result(business_user, auctions, "REAL NOTICE", "6/30/2026", TRUSTEE("2026-10-09"), "WA|pierce|12"),
        _result(business_user, probate, "PROBATE", "5/1/2026", None, "WA|pierce|13"),
        # Same properties on probate, so the date-windowed intersection has pairs.
        _result(business_user, probate, "PROBATE ON STAND IN", "5/2/2026", None, "WA|pierce|11"),
        _result(business_user, probate, "PROBATE ON NOTICE", "5/3/2026", None, "WA|pierce|12"),
    ])
    await db.commit()


_WINDOW = {"filing_from": "2026-01-01", "filing_to": "2026-12-31"}


async def test_lists_union_flags_the_stand_in(client, business_token, lists_rows):
    resp = await client.post("/segments/union", json={"record_types": ["trustee_sale"]},
                             headers=_auth(business_token))

    assert resp.status_code == 200, resp.text
    flags = {r["party_name"]: r["date_is_auction_date"] for r in resp.json()["rows"]}
    assert flags == {"STAND IN": True, "REAL NOTICE": False}


async def test_a_filing_window_never_matches_the_stand_in(client, business_token, lists_rows):
    resp = await client.post(
        "/segments/union", json={"record_types": ["trustee_sale", "probate"], **_WINDOW},
        headers=_auth(business_token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Union keeps one representative row per property, so assert what each property
    # matched inside the window rather than which of its rows was picked.
    matched = {r["party_name"]: set(r["matched_record_types"]) for r in body["rows"]}
    assert "STAND IN" not in matched
    # The stand-in's property is on probate only; the real notice's is on both.
    assert matched["PROBATE ON STAND IN"] == {"probate"}
    on_notice = matched.get("REAL NOTICE") or matched.get("PROBATE ON NOTICE")
    assert on_notice == {"probate", "trustee_sale"}
    assert matched["PROBATE"] == {"probate"}
    # It is reported with the leads skipped for having no filing date.
    assert body["excluded_no_date_count"] == 1


async def test_windowed_intersection_ignores_the_stand_in(client, business_token, lists_rows):
    resp = await client.post(
        "/segments/intersection",
        json={"record_types": ["trustee_sale", "probate"], **_WINDOW},
        headers=_auth(business_token),
    )

    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    # Only the property whose auction lead has a REAL notice date is on both lists
    # inside the window.
    assert len(rows) == 1
    assert rows[0]["party_name"] in {"REAL NOTICE", "PROBATE ON NOTICE"}
    assert rows[0]["date_is_auction_date"] is False


async def test_lists_csv_blanks_the_stand_in(client, business_token, lists_rows):
    resp = await client.post("/segments/union/export", json={"record_types": ["trustee_sale"]},
                             headers=_auth(business_token))

    assert resp.status_code == 200, resp.text
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    dates = {r["party_name"]: r["filed_date"] for r in rows}
    assert dates == {"STAND IN": "", "REAL NOTICE": "6/30/2026"}
