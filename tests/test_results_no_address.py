"""The breakdown's "N had no address" must be a set the user can open.

Owner report, 2026-10-03: an Island probate run read "Of 71 records found: 71 No
address, 0 New leads" over a table saying "No records found." The 71 rows were kept
in ``results`` (lead_actionability.py) but every list filtered them out, so the
count could not be checked. ``GET /jobs/{id}/results?category=no_address`` lists
them and ``no_address_count`` counts them.

Asserted here, against the real DB and the real endpoints (conftest fixtures, no
mocks):
  - the list is EXACTLY the run breakdown's no_address bucket (the partition's first
    branch), including duplicates and superseded rows without an address;
  - the owner's 18-month tax cap still applies, so a capped row is in the breakdown
    but not the list (the UI says so when the two differ);
  - search and paging run inside the category;
  - the CSV, export URL and contact lookups refuse the category: these are not leads;
  - another account can never list or count these rows.
"""
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from src.db.models import Job, Result, ScraperConfig, User

NOW = datetime.now(UTC)


async def _job(db, user: User, config: ScraperConfig, *, records_found: int | None,
               created_at: datetime = NOW) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(
        id=job_id, user_id=user.id, scraper_config_id=config.id, status="done",
        trigger="manual", record_count=0, billed_count=0, records_found=records_found,
        retry_count=0, created_at=created_at, export_key=f"exports/{job_id}.csv",
    ))
    await db.commit()
    return job_id


async def _row(db, job_id: str, user_id: str, *, tag: str, prop: str | None = None,
               mail: str | None = None, parcel: str | None = None, duplicate: bool = False,
               reason: str | None = None, bill_year: int | None = None) -> str:
    row_id = str(uuid.uuid4())
    db.add(Result(
        id=row_id, job_id=job_id, user_id=user_id, party_name=f"OWNER {tag}",
        parcel_id=parcel, property_address=prop, mailing_address=mail,
        dedup_hash=uuid.uuid4().hex, is_duplicate=duplicate, duplicate_reason=reason,
        delinquent_bill_year=bill_year, doc_type="Transfer on Death Deed",
        enrichment_data={"instrument_number": tag},
    ))
    await db.commit()
    return row_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _results(client: AsyncClient, job_id: str, token: str, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_the_island_run_lists_all_71_records_it_could_not_address(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    """The reported run, reproduced: 71 parcel-less probate records, none addressed."""
    job = await _job(db, starter_user, scraper_config, records_found=71)
    ids = {await _row(db, job, starter_user.id, tag=f"I{i:02d}") for i in range(71)}

    lead_view = await _results(client, job, starter_token)
    assert lead_view["total"] == 0 and lead_view["new_count"] == 0
    assert lead_view["no_address_count"] == 71
    assert lead_view["breakdown"] == {
        "dropped_before_save": 0, "no_address": 71, "same_run_merged": 0,
        "already_delivered": 0, "over_quota": 0, "new": 0,
    }

    seen: set[str] = set()
    for page in (1, 2):
        body = await _results(client, job, starter_token, category="no_address",
                              page=page, page_size=50)
        assert body["total"] == 71
        seen |= {r["id"] for r in body["items"]}
    assert seen == ids


async def test_the_list_is_exactly_the_breakdown_bucket(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    """Every kind of row, so the list cannot drift from the partition's first branch."""
    job = await _job(db, starter_user, scraper_config, records_found=12)
    uid = starter_user.id
    no_addr = {
        await _row(db, job, uid, tag="BARE"),
        await _row(db, job, uid, tag="PARCEL-ONLY", parcel="R1234567"),
        await _row(db, job, uid, tag="BLANK", prop="   ", mail=""),
        await _row(db, job, uid, tag="PLACEHOLDER", prop="(enrichment unavailable)",
                   mail="(enrichment unavailable)"),
        # A duplicate or superseded row with no address is no_address first.
        await _row(db, job, uid, tag="DUP", duplicate=True, reason="prior_run"),
        await _row(db, job, uid, tag="SAME", duplicate=True, reason="same_run"),
        await _row(db, job, uid, tag="SUPER", duplicate=True, reason="superseded"),
    }
    leads = {
        await _row(db, job, uid, tag="PROP", prop="1 FIR LN"),
        await _row(db, job, uid, tag="MAIL", mail="PO BOX 1"),
    }
    delivered = await _row(db, job, uid, tag="DLV", prop="2 OAK AVE", duplicate=True,
                           reason="prior_run")

    body = await _results(client, job, starter_token, category="no_address")
    assert {r["id"] for r in body["items"]} == no_addr
    assert body["total"] == body["no_address_count"] == body["breakdown"]["no_address"] == 7
    # The other views never see these rows.
    assert {r["id"] for r in (await _results(client, job, starter_token))["items"]} == leads
    assert {r["id"] for r in (await _results(
        client, job, starter_token, category="already_delivered"))["items"]} == {delivered}
    # And the breakdown still reconciles to records_found (12 found, 10 saved).
    b = body["breakdown"]
    assert sum(b.values()) == 12 and b["dropped_before_save"] == 2


async def test_the_tax_cap_still_hides_old_rows_and_the_counts_say_so(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    """Owner rule: a tax row past the 18-month cap is never shown. The breakdown is
    RAW, so it counts it; no_address_count follows the list. The page names the gap."""
    job = await _job(db, starter_user, scraper_config, records_found=2)
    await _row(db, job, starter_user.id, tag="OLDTAX", bill_year=2000)
    recent = await _row(db, job, starter_user.id, tag="NEWTAX", bill_year=NOW.year)

    body = await _results(client, job, starter_token, category="no_address")
    assert body["breakdown"]["no_address"] == 2
    assert body["no_address_count"] == body["total"] == 1
    assert [r["id"] for r in body["items"]] == [recent]


async def test_search_runs_inside_the_category(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job = await _job(db, starter_user, scraper_config, records_found=3)
    want = await _row(db, job, starter_user.id, tag="NEEDLE")
    await _row(db, job, starter_user.id, tag="HAY")
    await _row(db, job, starter_user.id, tag="NEEDLE-LEAD", prop="3 ELM ST")

    body = await _results(client, job, starter_token, category="no_address", q="NEEDLE")
    assert [r["id"] for r in body["items"]] == [want]
    assert body["no_address_count"] == 2  # the tab number ignores the search


async def test_not_a_lead_so_never_exported_or_looked_up(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job = await _job(db, starter_user, scraper_config, records_found=1)
    await _row(db, job, starter_user.id, tag="X")
    for method, path in (("GET", "export-url"), ("GET", "download"),
                         ("POST", "contact-lookups/quote")):
        if method == "GET":
            resp = await client.get(f"/jobs/{job}/{path}", params={"category": "no_address"},
                                    headers=_auth(starter_token))
        else:
            resp = await client.post(f"/jobs/{job}/{path}", json={"category": "no_address"},
                                     headers=_auth(starter_token))
        assert resp.status_code == 422, (path, resp.status_code, resp.text)


async def test_another_account_cannot_list_or_count_my_unaddressed_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_token: str, scraper_config: ScraperConfig, db,
):
    job = await _job(db, starter_user, scraper_config, records_found=1,
                     created_at=NOW - timedelta(minutes=1))
    await _row(db, job, starter_user.id, tag="MINE")
    resp = await client.get(f"/jobs/{job}/results", params={"category": "no_address"},
                            headers=_auth(business_token))
    assert resp.status_code == 404
