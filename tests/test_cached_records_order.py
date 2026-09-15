"""GET /scrapers/{id}/records returns the cache in a total, date-aware order.

The cache is refreshed in batches that share one scraped_at (prod: Benton 2,574 rows
with one timestamp), and the endpoint ordered by scraped_at alone, so each batch came
back in heap order and OFFSET paging over the ties was not stable. scraped_at stays
the primary key (the "new since you last looked" feed); inside a batch rows now run
newest date first, undated last, then by id.

The junk values below are real shapes from prod Pierce cache rows (instrument
numbers and scraped UI text in date_recorded). They must sort as "no date", never
fail the page.

Real DB + real endpoint, no mocks. county_records has no owner column and is not
cleaned by the conftest teardown, so every test uses its own county and deletes it.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, literal, select

import src.db.session as _db_session
from src.api.results_sort import _text_date
from src.db.models import CountyRecord, ScraperConfig, User


@pytest_asyncio.fixture
async def cache_county():
    county = f"sortcache{uuid.uuid4().hex[:8]}"
    yield county
    async with _db_session.AsyncSessionLocal() as s:
        await s.execute(delete(CountyRecord).where(CountyRecord.county == county))
        await s.commit()


async def _config(user: User, county: str) -> str:
    config_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(ScraperConfig(
            id=config_id, user_id=user.id, name="Cache order", county=county, state="WA",
            record_type="probate", fields=["party_name"], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        ))
        await s.commit()
    return config_id


async def _cache(county: str, scraped_at: datetime, specs: list[dict]) -> list[str]:
    """One refresh batch: every row shares scraped_at, as the cache writer does."""
    ids = []
    async with _db_session.AsyncSessionLocal() as s:
        for spec in specs:
            rid = str(uuid.uuid4())
            ids.append(rid)
            s.add(CountyRecord(
                id=rid, county=county, state="WA",
                doc_type=spec.get("doc_type", "PROBATE"),
                date_recorded=spec.get("date"),
                party_name=spec.get("party", "ESTATE OF SOMEONE"),
                record_hash=uuid.uuid4().hex,
                scraped_at=scraped_at,
            ))
        await s.commit()
    return ids


async def _page(client: AsyncClient, config_id: str, token: str, **params) -> dict:
    resp = await client.get(
        f"/scrapers/{config_id}/records",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


_JUNK = ["200005310610", "8207220167", "#ImageItem SelectInstrument #Boo", "View", "", None]


async def test_rows_of_one_batch_run_newest_date_first_with_undated_last(
    client: AsyncClient, starter_user: User, starter_token: str, cache_county: str,
):
    config_id = await _config(starter_user, cache_county)
    batch = datetime(2026, 3, 20, 22, 52, tzinfo=UTC)
    # Junk first and dates out of order, so insertion order proves nothing.
    await _cache(cache_county, batch, [{"date": j} for j in _JUNK] + [
        {"date": "2/4/2026"}, {"date": "September 18, 2026"},
        {"date": "03/13/2026"}, {"date": "12/1/2025"},
    ])

    body = await _page(client, config_id, starter_token)
    dates = [row["date_recorded"] for row in body["items"]]

    assert dates[:4] == ["September 18, 2026", "03/13/2026", "2/4/2026", "12/1/2025"]
    assert sorted(dates[4:], key=str) == sorted(_JUNK, key=str)


async def test_pages_are_slices_of_one_total_order(
    client: AsyncClient, starter_user: User, starter_token: str, cache_county: str,
):
    config_id = await _config(starter_user, cache_county)
    batch = datetime(2026, 3, 20, 22, 52, tzinfo=UTC)
    # 20 rows share one date, so only the id tie-break can keep pages apart.
    ids = await _cache(cache_county, batch, [{"date": "3/1/2026"}] * 20 + [{"date": "4/1/2026"}] * 3)

    full = await _page(client, config_id, starter_token, page_size=100)
    paged = []
    for page in range(1, 6):
        paged += (await _page(client, config_id, starter_token, page=page, page_size=5))["items"]

    assert [r["id"] for r in paged] == [r["id"] for r in full["items"]]
    assert sorted(r["id"] for r in paged) == sorted(ids)
    assert [r["date_recorded"] for r in paged[:3]] == ["4/1/2026"] * 3
    same_date_ids = [r["id"] for r in paged[3:]]
    assert same_date_ids == sorted(same_date_ids)


async def test_a_newer_batch_stays_above_an_older_batch_with_later_dates(
    client: AsyncClient, starter_user: User, starter_token: str, cache_county: str,
):
    """scraped_at is still the primary key: the date only orders rows WITHIN a batch."""
    config_id = await _config(starter_user, cache_county)
    older = datetime(2026, 3, 1, tzinfo=UTC)
    await _cache(cache_county, older, [{"date": "12/31/2026", "party": "OLD BATCH"}])
    await _cache(cache_county, older + timedelta(days=1), [{"date": "1/1/2020", "party": "NEW BATCH"}])

    body = await _page(client, config_id, starter_token)

    assert [r["party_name"] for r in body["items"]] == ["NEW BATCH", "OLD BATCH"]
    # First view: nothing has been seen yet, so both are new.
    assert [r["is_new"] for r in body["items"]] == [True, True]


async def test_search_and_doc_type_filters_still_apply(
    client: AsyncClient, starter_user: User, starter_token: str, cache_county: str,
):
    config_id = await _config(starter_user, cache_county)
    batch = datetime(2026, 3, 20, tzinfo=UTC)
    await _cache(cache_county, batch, [
        {"date": "1/1/2026", "party": "SMITH A"},
        {"date": "6/1/2026", "party": "SMITH B"},
        {"date": "9/1/2026", "party": "JONES"},
        {"date": "12/1/2026", "party": "SMITH C", "doc_type": "WARRANTY DEED"},
    ])

    body = await _page(client, config_id, starter_token, q="smith")

    assert body["total"] == 2
    assert [r["party_name"] for r in body["items"]] == ["SMITH B", "SMITH A"]


@pytest.mark.parametrize(("text", "expected"), [
    ("3/20/2026", "2026-03-20"),
    (" 03/13/2026 ", "2026-03-13"),
    ("2/29/2028", "2028-02-29"),
    ("September 18, 2026", "2026-09-18"),
    ("Sept. 5 2026", "2026-09-05"),
    ("2/29/2027", None),
    ("13/1/2026", None),
    ("0/10/2026", None),
    ("4/31/2026", None),
    ("1/0/2026", None),
    ("12/31/0000", None),
    ("99/99/9999", None),
    ("200005310610", None),
    ("#ImageItem SelectInstrument #Boo", None),
    ("View", None),
    ("", None),
    (None, None),
])
async def test_text_date_parser_is_total(db, text: str | None, expected: str | None):
    """Every input yields a date or NULL. Evaluated by PostgreSQL, not Python."""
    value = (await db.execute(select(_text_date(literal(text))))).scalar_one()
    assert (value.isoformat() if value else None) == expected
