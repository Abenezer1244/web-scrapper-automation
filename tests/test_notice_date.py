"""An auction date a scraper stood in for a missing notice date is never shown as a Date.

Owner decision (2026-09-15): on auction leads (trustee_sale) and Snohomish
pre_foreclosure, Date shows the real notice date when there is one, otherwise blank.
The stored ``date_recorded`` is unchanged (it feeds dedup_hash / source_fingerprint).

The rule compares against the auction date the SCRAPER recorded in enrichment_data,
not ``results.auction_date``: the NTS matcher moves that column when a sale is
postponed, and a moved column must not turn the old stand-in back into a "notice
date" (Codex P1). The Python rule (display, exports) and its SQL twin (sorting) are
checked against each other on one shared set of inputs, evaluated by PostgreSQL.

Real DB + real endpoints, no mocks.
"""
import csv
import io
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import src.db.session as _db_session
from src.api.results_sort import auction_date_fallback_condition
from src.db.models import Job, Result, ScraperConfig, User
from src.utils.lead_export import (
    LEAD_CSV_COLUMNS,
    build_lead_export_row,
    write_lead_csv,
    write_lead_csv_with_overlap,
)
from src.utils.source_dates import is_auction_date_fallback, parse_source_date

SNOHO = lambda auction: {"source": "snohomish_tribune", "auction_date": auction}  # noqa: E731
TRUSTEE = lambda auction: {"nts_source": {"notice_id": "n1", "auction_date": auction}}  # noqa: E731

# (date_recorded, enrichment_data, expected is-a-stand-in)
VECTORS = [
    # Snohomish pre_foreclosure: fell back to the auction date, in both forms it writes.
    ("9/18/2026", SNOHO("9/18/2026"), True),
    ("September 18, 2026", SNOHO("September 18, 2026"), True),
    ("09/18/2026", SNOHO("Sept. 18 2026"), True),
    (" 9/18/2026 ", SNOHO("SEPTEMBER 18, 2026"), True),
    # Snohomish with a real notice (mailing) date.
    ("2/4/2026", SNOHO("10/9/2026"), False),
    # trustee_sale: fell back (M/D/YYYY vs the ISO origin), and a real notice date.
    ("10/9/2026", TRUSTEE("2026-10-09"), True),
    ("6/30/2026", TRUSTEE("2026-10-09"), False),
    # Fails closed: nothing readable on one side is never a stand-in.
    ("9/18/2026", None, False),
    ("9/18/2026", {}, False),
    ("9/18/2026", {"nts": {"auction_date": "2026-09-18"}}, False),  # matcher key, not origin
    ("9/18/2026", SNOHO("not a date"), False),
    ("9/18/2026", TRUSTEE("09/18/2026"), False),  # origin must be ISO
    ("9/18/2026", TRUSTEE("2026-02-30"), False),
    (None, SNOHO("9/18/2026"), False),
    ("", SNOHO("9/18/2026"), False),
    ("N/A", SNOHO("9/18/2026"), False),
    ("2/29/2027", SNOHO("2/29/2027"), False),  # both impossible: no date, no stand-in
    ("2/29/2028", SNOHO("February 29, 2028"), True),
    ("9/18/2026 10:00", SNOHO("9/18/2026"), False),  # timestamps are not the grammar
]


@pytest.mark.parametrize(("recorded", "enrichment", "expected"), VECTORS)
def test_python_rule(recorded, enrichment, expected):
    assert is_auction_date_fallback(recorded, enrichment) is expected


def test_parser_grammar_is_exact():
    assert parse_source_date("3/20/2026") == date(2026, 3, 20)
    assert parse_source_date("sept. 5, 2026") == date(2026, 9, 5)
    assert parse_source_date("Sep 5 2026") == date(2026, 9, 5)
    for junk in ("2026-03-20", "20 March 2026", "Smarch 3, 2026", "4/31/2026", "3/20/26", None, 5):
        assert parse_source_date(junk) is None


async def _job(user: User, record_type: str, county: str = "snohomish") -> str:
    config_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(ScraperConfig(
            id=config_id, user_id=user.id, name=f"Notice {record_type}", county=county,
            state="WA", record_type=record_type, fields=["party_name"], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        ))
        await s.flush()
        s.add(Job(id=job_id, user_id=user.id, scraper_config_id=config_id, status="done",
                  trigger="manual", export_key=f"exports/{job_id}.csv"))
        await s.commit()
    return job_id


async def _rows(job_id: str, user_id: str, specs: list[dict]) -> list[str]:
    ids = []
    async with _db_session.AsyncSessionLocal() as s:
        for i, spec in enumerate(specs):
            rid = str(uuid.uuid4())
            ids.append(rid)
            s.add(Result(
                id=rid, job_id=job_id, user_id=user_id,
                date_recorded=spec.get("date"), enrichment_data=spec.get("enrichment"),
                auction_date=spec.get("auction"), party_name=spec.get("party", f"OWNER {i}"),
                property_address=f"{i} MAIN ST", dedup_hash=uuid.uuid4().hex, is_duplicate=False,
            ))
        await s.commit()
    return ids


async def test_sql_twin_agrees_with_python_rule(starter_user: User):
    job_id = await _job(starter_user, "pre_foreclosure")
    ids = await _rows(job_id, starter_user.id, [
        {"date": recorded, "enrichment": enrichment} for recorded, enrichment, _ in VECTORS
    ])
    async with _db_session.AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Result.id, auction_date_fallback_condition()).where(Result.job_id == job_id)
        )).all()
    sql = {str(rid): bool(flag) for rid, flag in rows}  # NULL means "not a stand-in"
    for rid, (recorded, enrichment, expected) in zip(ids, VECTORS, strict=True):
        assert sql[rid] is expected, (recorded, enrichment)
        assert sql[rid] is is_auction_date_fallback(recorded, enrichment)


async def _results(client: AsyncClient, job_id: str, token: str, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params,
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("sort", ["date_desc", "date_asc"])
async def test_auction_leads_flag_stand_ins_and_sort_them_with_undated_rows(
    client: AsyncClient, starter_user: User, starter_token: str, sort: str,
):
    job_id = await _job(starter_user, "trustee_sale", county="pierce")
    await _rows(job_id, starter_user.id, [
        {"date": "12/1/2026", "enrichment": TRUSTEE("2026-12-01"),
         "auction": date(2026, 12, 1), "party": "STAND IN LATEST"},
        {"date": "3/5/2026", "enrichment": TRUSTEE("2026-11-20"),
         "auction": date(2026, 11, 20), "party": "NOTICE MARCH"},
        {"date": "10/1/2026", "enrichment": TRUSTEE("2026-10-01"),
         "auction": date(2026, 10, 1), "party": "STAND IN OCT"},
        {"date": "6/9/2026", "enrichment": TRUSTEE("2026-12-15"),
         "auction": date(2026, 12, 15), "party": "NOTICE JUNE"},
    ])

    body = await _results(client, job_id, starter_token, sort=sort)
    items = body["items"]

    real = ["NOTICE JUNE", "NOTICE MARCH"] if sort == "date_desc" else ["NOTICE MARCH", "NOTICE JUNE"]
    assert [r["party_name"] for r in items[:2]] == real
    assert {r["party_name"] for r in items[2:]} == {"STAND IN LATEST", "STAND IN OCT"}
    flags = {r["party_name"]: r["date_is_auction_date"] for r in items}
    assert flags == {"NOTICE JUNE": False, "NOTICE MARCH": False,
                     "STAND IN LATEST": True, "STAND IN OCT": True}
    # The stored value still travels unchanged; only its meaning is flagged.
    assert {r["party_name"]: r["date_recorded"] for r in items}["STAND IN OCT"] == "10/1/2026"


async def test_a_postponed_sale_does_not_turn_the_stand_in_into_a_notice_date(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """The matcher moved results.auction_date to the new sale; the scraper's recorded
    auction date (what date_recorded stood in for) did not move."""
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "September 18, 2026", "enrichment": SNOHO("September 18, 2026"),
         "auction": date(2026, 11, 6)},
    ])

    body = await _results(client, job_id, starter_token)

    assert body["items"][0]["date_is_auction_date"] is True


async def test_recorder_pre_foreclosure_dates_are_never_flagged(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """King/Pierce/Clark recorder dates are real recording dates. Even one that happens
    to equal the matched sale date carries no scraper origin, so it is shown."""
    job_id = await _job(starter_user, "pre_foreclosure", county="king")
    await _rows(job_id, starter_user.id, [
        {"date": "10/9/2026", "auction": date(2026, 10, 9),
         "enrichment": {"nts": {"auction_date": "2026-10-09"}}},
    ])

    body = await _results(client, job_id, starter_token)

    assert body["items"][0]["date_is_auction_date"] is False


def _record(recorded, enrichment) -> dict:
    return {"date_recorded": recorded, "enrichment_data": enrichment, "party_name": "A B",
            "property_address": "1 MAIN ST, EVERETT, WA 98201"}


def test_export_blanks_the_stand_in_and_keeps_a_real_notice_date():
    assert build_lead_export_row(_record("9/18/2026", SNOHO("9/18/2026")))["date_recorded"] == ""
    assert build_lead_export_row(_record("10/9/2026", TRUSTEE("2026-10-09")))["date_recorded"] == ""
    assert build_lead_export_row(_record("2/4/2026", SNOHO("10/9/2026")))["date_recorded"] == "2/4/2026"
    assert build_lead_export_row(_record("2/4/2026", None))["date_recorded"] == "2/4/2026"


def test_both_csv_writers_keep_their_columns_and_blank_the_stand_in():
    records = [_record("9/18/2026", SNOHO("9/18/2026")), _record("2/4/2026", SNOHO("10/9/2026"))]

    plain = io.StringIO()
    write_lead_csv(records, plain)
    plain_rows = list(csv.DictReader(io.StringIO(plain.getvalue())))
    assert list(plain_rows[0].keys()) == LEAD_CSV_COLUMNS
    assert [r["date_recorded"] for r in plain_rows] == ["", "2/4/2026"]

    overlap = io.StringIO()
    write_lead_csv_with_overlap(
        [(r, {"lists_count": 1, "lists": "Pre-foreclosure", "counties": "Snohomish"}) for r in records],
        overlap,
    )
    overlap_rows = list(csv.DictReader(io.StringIO(overlap.getvalue())))
    assert "filed_date" in overlap_rows[0]
    assert [r["filed_date"] for r in overlap_rows] == ["", "2/4/2026"]
