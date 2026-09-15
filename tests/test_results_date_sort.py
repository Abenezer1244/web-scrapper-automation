"""GET /jobs/{id}/results orders the whole filtered set by the first column's date.

Regression for a completed Snohomish pre_foreclosure run whose page read
Sep 18, Feb 4, Sep 18, Sep 18. The endpoint ordered by ``created_at``, and every row
of a job shares one ``created_at`` (they are inserted in one transaction), so the
page came back in heap order and OFFSET paging over the ties was not stable.

The fixtures below insert rows in one transaction on purpose: that is the production
shape, and it is exactly what made ``created_at`` useless as a sort key.

Real DB + real endpoints (conftest ``db``/``client``/token fixtures), no mocks.
"""
import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient

import src.db.session as _db_session
from src.api.tax_filters import tax_cap_min_year
from src.db.models import Job, Result, ScraperConfig, User


async def _job(user: User, record_type: str = "probate") -> str:
    """A done job under a config of ``record_type`` owned by ``user``."""
    config_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(ScraperConfig(
            id=config_id,
            user_id=user.id,
            name=f"Sort {record_type}",
            county="snohomish",
            state="WA",
            record_type=record_type,
            fields=["party_name", "parcel_id"],
            enrichment=[],
            schedule={"frequency": "manual"},
            deliver={"format": "csv", "emails": []},
        ))
        await s.flush()
        s.add(Job(
            id=job_id,
            user_id=user.id,
            scraper_config_id=config_id,
            status="done",
            trigger="manual",
            export_key=f"exports/{job_id}.csv",
        ))
        await s.commit()
    return job_id


async def _rows(job_id: str, user_id: str, specs: list[dict]) -> list[str]:
    """Insert rows in ONE transaction, so they share created_at as in production.

    Each spec may set: date (date_recorded text), party, bill_year, amount,
    auction (date), absentee (bool).
    """
    ids = []
    async with _db_session.AsyncSessionLocal() as s:
        for i, spec in enumerate(specs):
            rid = str(uuid.uuid4())
            ids.append(rid)
            s.add(Result(
                id=rid,
                job_id=job_id,
                user_id=user_id,
                date_recorded=spec.get("date"),
                party_name=spec.get("party", f"OWNER {i}"),
                property_address=f"{i} MAIN ST",
                delinquent_bill_year=spec.get("bill_year"),
                delinquent_amount=spec.get("amount"),
                auction_date=spec.get("auction"),
                absentee_owner=spec.get("absentee"),
                dedup_hash=uuid.uuid4().hex,
                is_duplicate=False,
            ))
        await s.commit()
    return ids


async def _get(client: AsyncClient, job_id: str, token: str, **params) -> dict:
    resp = await client.get(
        f"/jobs/{job_id}/results",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _dates(body: dict) -> list[str | None]:
    return [row["date_recorded"] for row in body["items"]]


def _as_date(text: str) -> date:
    """Test-side parse of the formats the fixtures use, for ordering assertions."""
    for fmt in ("%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise AssertionError(f"fixture date not parseable: {text!r}")


# ─── A / B: the reported case, both directions ──────────────────────────────────

async def test_default_order_is_newest_date_first(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "9/18/2026"}, {"date": "2/4/2026"},
        {"date": "09/18/2026"}, {"date": "8/13/2026"},
    ])

    body = await _get(client, job_id, starter_token)

    assert [_as_date(d) for d in _dates(body)] == [
        date(2026, 9, 18), date(2026, 9, 18), date(2026, 8, 13), date(2026, 2, 4),
    ]


async def test_date_asc_is_oldest_first(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "9/18/2026"}, {"date": "2/4/2026"},
        {"date": "09/18/2026"}, {"date": "8/13/2026"},
    ])

    body = await _get(client, job_id, starter_token, sort="date_asc")

    assert [_as_date(d) for d in _dates(body)] == [
        date(2026, 2, 4), date(2026, 8, 13), date(2026, 9, 18), date(2026, 9, 18),
    ]


# ─── C: same-date ties are deterministic ────────────────────────────────────────

async def test_same_date_rows_break_ties_by_id_in_both_directions(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user)
    ids = await _rows(job_id, starter_user.id, [{"date": "5/1/2026"}] * 6)

    desc = await _get(client, job_id, starter_token)
    desc_again = await _get(client, job_id, starter_token)
    asc = await _get(client, job_id, starter_token, sort="date_asc")

    expected = sorted(ids)
    assert [r["id"] for r in desc["items"]] == expected
    assert [r["id"] for r in desc_again["items"]] == expected
    # Only the date direction flips; a same-date group reads the same both ways.
    assert [r["id"] for r in asc["items"]] == expected


# ─── D: rows with no usable date go last, never first ───────────────────────────

@pytest.mark.parametrize("sort", ["date_desc", "date_asc"])
async def test_undated_rows_sort_after_every_dated_row(
    client: AsyncClient, starter_user: User, starter_token: str, sort: str,
):
    job_id = await _job(starter_user)
    await _rows(job_id, starter_user.id, [
        {"date": None}, {"date": "3/3/2026"}, {"date": ""},
        {"date": "N/A"}, {"date": "7/7/2025"},
    ])

    dates = _dates(await _get(client, job_id, starter_token, sort=sort))

    dated = ["3/3/2026", "7/7/2025"] if sort == "date_desc" else ["7/7/2025", "3/3/2026"]
    assert dates[:2] == dated
    # Nothing was invented for them: the stored text comes back untouched.
    assert sorted(dates[2:], key=str) == sorted([None, "", "N/A"], key=str)


# ─── E / H: ordering spans pages, including a date that straddles a boundary ────

async def test_pages_are_slices_of_one_global_order(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user)
    # 130 rows over 3 pages of 50: 44 in Dec 2026, 12 sharing 6/15/2026, 74 in
    # Jan 2025. Old and new are interleaved, so insertion order is useless, and
    # page 1 ends six rows into the shared 6/15/2026 group.
    specs = []
    for i in range(74):
        specs.append({"date": f"1/{(i % 28) + 1}/2025"})
        if i < 44:
            specs.append({"date": f"12/{(i % 28) + 1}/2026"})
    specs[40:40] = [{"date": "6/15/2026"}] * 12
    ids = await _rows(job_id, starter_user.id, specs)
    assert len(ids) == 130

    pages = [
        await _get(client, job_id, starter_token, page=p, page_size=50)
        for p in (1, 2, 3)
    ]
    items = [row for body in pages for row in body["items"]]

    assert pages[0]["total"] == 130
    assert [len(body["items"]) for body in pages] == [50, 50, 30]
    # No row repeated or skipped across the page boundaries.
    assert sorted(row["id"] for row in items) == sorted(ids)
    # Concatenated pages are one newest-first order, ties by id.
    keys = [(_as_date(row["date_recorded"]), row["id"]) for row in items]
    assert keys == sorted(keys, key=lambda k: (-k[0].toordinal(), k[1]))
    # Oldest on page 1 is not older than the newest on page 2, and the shared date
    # really does straddle that boundary.
    assert pages[0]["items"][-1]["date_recorded"] == "6/15/2026"
    assert pages[1]["items"][0]["date_recorded"] == "6/15/2026"
    assert pages[0]["items"][-1]["id"] < pages[1]["items"][0]["id"]


# ─── F / G: search and filters narrow first, then the narrowed set is sorted ────

async def test_search_results_are_sorted_and_paginated_server_side(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user)
    specs = [{"date": f"{m}/1/2026", "party": f"SMITH {m}"} for m in range(1, 13)]
    specs += [{"date": f"{m}/2/2026", "party": f"JONES {m}"} for m in range(1, 13)]
    await _rows(job_id, starter_user.id, specs)

    page1 = await _get(client, job_id, starter_token, q="smith", page=1, page_size=5)
    page2 = await _get(client, job_id, starter_token, q="smith", page=2, page_size=5)

    assert page1["total"] == 12
    names = [r["party_name"] for r in page1["items"] + page2["items"]]
    assert names == [f"SMITH {m}" for m in range(12, 2, -1)]


async def test_filtered_results_are_sorted(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user)
    await _rows(job_id, starter_user.id, [
        {"date": "11/5/2026", "absentee": True},
        {"date": "9/5/2026", "absentee": False},
        {"date": "1/5/2026", "absentee": True},
        {"date": "4/5/2026", "absentee": True},
    ])

    body = await _get(client, job_id, starter_token, absentee="true", sort="date_asc")

    assert _dates(body) == ["1/5/2026", "4/5/2026", "11/5/2026"]


# ─── I: date-only values are compared as calendar dates, not text or instants ───

async def test_dates_compare_as_calendar_days_not_strings(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """As text, "9/18/2026" > "12/1/2026" and "09/18" < "9/18". As dates, neither
    holds. The month-name form the Snohomish scraper writes is the same day as its
    numeric twin, and both come back exactly as stored (no timezone shift)."""
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "9/18/2026"},
        {"date": "12/1/2026"},
        {"date": "September 18, 2026"},
        {"date": "09/17/2026"},
        {"date": "Sept. 19, 2026"},
    ])

    dates = _dates(await _get(client, job_id, starter_token))

    assert dates[0] == "12/1/2026"
    assert dates[1] == "Sept. 19, 2026"
    assert sorted(dates[2:4]) == sorted(["9/18/2026", "September 18, 2026"])
    assert dates[4] == "09/17/2026"


async def test_malformed_month_name_dates_never_fail_the_page(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """Impossible or unrecognised dates must parse to "no date", not a 500."""
    job_id = await _job(starter_user, "pre_foreclosure")
    junk = [
        "February 30, 2026", "April 31, 2026", "Smarch 3, 2026",
        "January 1, 0000", "May 0, 2026", "June 99, 2026", "18 September 2026",
    ]
    # The one real date is inserted LAST, so it only leads the page if it is sorted.
    await _rows(job_id, starter_user.id, [{"date": j} for j in junk] + [{"date": "3/1/2024"}])

    for sort in ("date_desc", "date_asc"):
        dates = _dates(await _get(client, job_id, starter_token, sort=sort))
        assert dates[0] == "3/1/2024"
        assert sorted(dates[1:]) == sorted(junk)


async def test_leap_day_month_name_date_is_a_real_date(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "February 29, 2028"}, {"date": "3/1/2028"}, {"date": "2/28/2028"},
    ])

    dates = _dates(await _get(client, job_id, starter_token, sort="date_asc"))

    assert dates == ["2/28/2028", "February 29, 2028", "3/1/2028"]


# ─── J: the sort parameter is an allowlist ──────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "date", "DESC", "created_at", "party_name", "date_desc; DROP TABLE results", "",
])
async def test_unknown_sort_values_are_rejected(
    client: AsyncClient, starter_user: User, starter_token: str, bad: str,
):
    job_id = await _job(starter_user)
    resp = await client.get(
        f"/jobs/{job_id}/results",
        params={"sort": bad},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 422


# ─── K-P: every record type sorts by what its first column shows ────────────────

@pytest.mark.parametrize("record_type", [
    "probate", "pre_foreclosure", "code_violation", "death_certificate",
    "divorce", "trustee_sale",
])
async def test_dated_record_types_sort_by_date_recorded(
    client: AsyncClient, starter_user: User, starter_token: str, record_type: str,
):
    job_id = await _job(starter_user, record_type)
    await _rows(job_id, starter_user.id, [
        {"date": "2/4/2026"}, {"date": "10/9/2026"}, {"date": "6/30/2026"},
    ])

    body = await _get(client, job_id, starter_token)

    assert _dates(body) == ["10/9/2026", "6/30/2026", "2/4/2026"]


async def test_pre_foreclosure_sorts_by_date_not_auction_date(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """Auction Date is its own column. The row with the LATEST auction has the
    OLDEST Date, so sorting on the wrong column would invert this order."""
    job_id = await _job(starter_user, "pre_foreclosure")
    await _rows(job_id, starter_user.id, [
        {"date": "2/4/2026", "auction": date(2026, 12, 1)},
        {"date": "8/20/2026", "auction": date(2026, 9, 25)},
        {"date": "5/11/2026", "auction": date(2026, 10, 9)},
    ])

    body = await _get(client, job_id, starter_token)

    assert _dates(body) == ["8/20/2026", "5/11/2026", "2/4/2026"]


async def test_tax_delinquent_sorts_by_oldest_tax_year_not_synthetic_date(
    client: AsyncClient, starter_user: User, starter_token: str,
):
    """Tax jobs show the oldest delinquent tax year in the first column. Some rows
    still hold a synthetic 01/01/YYYY date_recorded the UI hides; these fixtures
    order it OPPOSITE to the year so sorting on it would fail."""
    year = tax_cap_min_year(datetime.now(UTC).date())
    job_id = await _job(starter_user, "tax_delinquent")
    await _rows(job_id, starter_user.id, [
        {"bill_year": year, "amount": 100, "date": "01/01/2030"},
        {"bill_year": year + 2, "amount": 100, "date": "01/01/2001"},
        {"bill_year": None, "amount": 100, "date": "01/01/2099"},
        {"bill_year": year + 1, "amount": 100, "date": None},
    ])

    desc = await _get(client, job_id, starter_token)
    asc = await _get(client, job_id, starter_token, sort="date_asc")

    assert [r["delinquent_bill_year"] for r in desc["items"]] == [year + 2, year + 1, year, None]
    assert [r["delinquent_bill_year"] for r in asc["items"]] == [year, year + 1, year + 2, None]


# ─── Scope: sorting never widens the job / tenant boundary ──────────────────────

async def test_sorting_stays_inside_the_job_and_the_account(
    client: AsyncClient, starter_user: User, starter_token: str, business_user: User,
):
    mine = await _job(starter_user)
    my_other_job = await _job(starter_user)
    theirs = await _job(business_user)
    mine_ids = await _rows(mine, starter_user.id, [{"date": "1/1/2026"}, {"date": "2/1/2026"}])
    # Newer dates elsewhere must not be pulled onto this page by the sort.
    await _rows(my_other_job, starter_user.id, [{"date": "12/31/2026"}])
    await _rows(theirs, business_user.id, [{"date": "12/30/2026"}])

    for sort in ("date_desc", "date_asc"):
        body = await _get(client, mine, starter_token, sort=sort)
        assert body["total"] == 2
        assert sorted(r["id"] for r in body["items"]) == sorted(mine_ids)

    resp = await client.get(
        f"/jobs/{theirs}/results",
        params={"sort": "date_asc"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 404
