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


# ─── Lists windows place real "Month D, YYYY" filing dates in time ───────────────

async def test_filing_date_sql_matches_the_results_sort_date(db, starter_user):
    """filing_date_sql is the date Results sorts on: M/D/YYYY or Month D, YYYY."""
    from datetime import date

    from src.api.results_sort import filing_date_sql
    job = await _config_and_job(db, starter_user, "probate")
    cases = {
        "3/20/2026": date(2026, 3, 20), " September 18, 2026": date(2026, 9, 18),
        "Sept. 5 2026": date(2026, 9, 5), "February 30, 2026": None,
        "View": None, "200005310610": None, "": None,
    }
    ids = {}
    for text_value in cases:
        row = _result(starter_user, job, f"F{len(ids)}", text_value, None)
        db.add(row)
        ids[row.id] = text_value
    await db.commit()
    async with _db_session.AsyncSessionLocal() as s:
        rows = (await s.execute(
            text(f"SELECT r.id, {filing_date_sql('r')} FROM results r WHERE r.job_id = :job"),
            {"job": job.id},
        )).all()
    assert {ids[str(rid)]: value for rid, value in rows} == cases


@pytest_asyncio.fixture
async def month_name_rows(db, business_user):
    probate = await _config_and_job(db, business_user, "probate")
    auctions = await _config_and_job(db, business_user, "trustee_sale")
    db.add_all([
        _result(business_user, probate, "MONTH NAME IN", "June 12, 2026", None, "WA|pierce|21"),
        _result(business_user, probate, "MONTH NAME OUT", "January 3, 2026", None, "WA|pierce|22"),
        _result(business_user, probate, "NUMERIC IN", "7/1/2026", None, "WA|pierce|23"),
        # A month-name auction stand-in is still no filing date.
        _result(business_user, auctions, "STAND IN MONTH", "October 9, 2026",
                {"source": "snohomish_tribune", "auction_date": "October 9, 2026"}, "WA|pierce|24"),
    ])
    await db.commit()


async def test_a_window_places_a_real_month_name_date(client, business_token, month_name_rows):
    resp = await client.post(
        "/segments/union",
        json={"record_types": ["probate", "trustee_sale"], "filing_from": "2026-06-01", "filing_to": "2026-12-31"},
        headers=_auth(business_token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {r["party_name"] for r in body["rows"]} == {"MONTH NAME IN", "NUMERIC IN"}
    # Only the stand-in has no filing date; the out-of-window month-name row is dated.
    assert body["excluded_no_date_count"] == 1


async def test_a_windowed_intersection_places_a_real_month_name_date(
    client, db, business_user, business_token,
):
    probate = await _config_and_job(db, business_user, "probate")
    prefc = await _config_and_job(db, business_user, "pre_foreclosure")
    db.add_all([
        _result(business_user, probate, "PROBATE MONTH", "June 12, 2026", None, "WA|pierce|31"),
        _result(business_user, prefc, "PREFC NUMERIC", "6/20/2026", None, "WA|pierce|31"),
    ])
    await db.commit()

    resp = await client.post(
        "/segments/intersection",
        json={"record_types": ["probate", "pre_foreclosure"], "filing_from": "2026-06-01"},
        headers=_auth(business_token),
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rows"]) == 1


async def test_filing_date_sql_equals_the_results_sort_key(db, starter_user):
    """Lists windows and the Results sort must place every row on the same date."""
    from sqlalchemy import func, select

    from src.api.results_sort import _month_name_date, filing_date_sql
    job = await _config_and_job(db, starter_user, "probate")
    texts = ["3/20/2026", " 3/20/2026 ", "03/13/2026", "September 18, 2026", "  Sep 18 2026  ",
             "sept. 5, 2026", "February 30, 2026", "Smarch 3, 2026", "12 June 2026", "View", "", None]
    for i, t in enumerate(texts):
        db.add(_result(starter_user, job, f"K{i}", t, None))
    await db.commit()
    async with _db_session.AsyncSessionLocal() as s:
        lists = dict((await s.execute(
            text(f"SELECT r.id, {filing_date_sql('r')} FROM results r WHERE r.job_id = :job"),
            {"job": job.id},
        )).all())
        results_key = dict((await s.execute(
            select(Result.id, func.coalesce(Result.date_recorded_parsed, _month_name_date(Result.date_recorded)))
            .where(Result.job_id == job.id)
        )).all())
    assert len(lists) == len(texts)
    assert {str(k): v for k, v in lists.items()} == {str(k): v for k, v in results_key.items()}


async def test_bounds_alone_keep_a_stand_in_out_of_the_window(db, business_user):
    """Even if a caller ever sent a bound with require_date false, a stand-in whose
    auction date falls inside the window must not match it."""
    from datetime import date

    from src.api.routes import segments
    from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year
    auctions = await _config_and_job(db, business_user, "trustee_sale")
    db.add(_result(business_user, auctions, "STAND IN", "10/9/2026", TRUSTEE("2026-10-09"), "WA|pierce|41"))
    await db.commit()
    async with _db_session.AsyncSessionLocal() as s:
        rows = (await s.execute(text(segments._UNION_SQL.format(county_clause="")), {
            "uid": business_user.id, "types": ["trustee_sale"], "limit": 100,
            "filing_from": date(2026, 1, 1), "filing_to": date(2026, 12, 31), "require_date": False,
            TAX_CAP_BIND: tax_cap_min_year(date.today()),
        })).all()
    assert rows == []
