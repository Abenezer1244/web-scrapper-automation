"""GET /batches/{id}/leads search + sort (Phase 4, owner decision D3, 2026-10-19).

DB-backed: search and sort run in the same combined SQL as the CSV, so the page,
its total, tenant scope, hidden fields and the untouched CSV order are all proven
against real Postgres.
"""
import uuid
from datetime import date

import pytest_asyncio
from sqlalchemy import text

from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year
from src.db.models import BatchRun, Result, ScraperBatch
from src.workers.batch_export import _COMBINED_SQL, view_filter_params
from tests.test_batch_leads_endpoint import _auth, _batch, _child


@pytest_asyncio.fixture
async def search_batch(db, starter_user, business_user):
    """Six leads (one stacked) with every date shape the table shows, plus another
    tenant's GREER EMI on the same property, forged into this run's child_job_ids."""
    batch = await _batch(db, starter_user.id, "Search")
    probate = await _child(db, starter_user.id, batch.id, "probate")
    prefc = await _child(db, starter_user.id, batch.id, "pre_foreclosure")
    tax = await _child(db, starter_user.id, batch.id, "tax_delinquent")
    trustee = await _child(db, starter_user.id, batch.id, "trustee_sale")

    def row(job, party, pk, date_recorded, user_id=starter_user.id, **kw):
        db.add(Result(
            id=str(uuid.uuid4()), user_id=user_id, job_id=job.id, party_name=party,
            property_key=pk, date_recorded=date_recorded,
            property_address=kw.pop("property_address", f"1 {party} ST"), **kw,
        ))

    # Stacked: the probate row carries the contact, so it represents the lead; the
    # pre-foreclosure sibling holds the borrower's name and the auction date.
    row(probate, "GREER EMI", "pk-1", "09/15/2026", parcel_id="0419123456",
        property_address="24515 57TH AVE E, GRAHAM, WA 98338",
        mailing_address="PO BOX 2210, PUYALLUP, WA 98371", phone="2535550144")
    row(prefc, "BORROWER NAME", "pk-1", "09/01/2026", parcel_id="0419123456",
        property_address="24515 57TH AVE E, GRAHAM, WA 98338",
        auction_date=date(2026, 11, 6))
    row(probate, "100% PURE_LLC", "pk-2", "10/01/2026")
    # Synthetic tax date: the table shows none, so it sorts with the undated.
    # The newest year the 18-month cap keeps, so the lead is never capped out.
    year = tax_cap_min_year(date.today())
    row(tax, "TAX OWNER", "pk-3", f"01/01/{year}", delinquent_bill_year=year,
        delinquent_amount=500)
    row(prefc, "AUCTION LATE", "pk-4", "08/01/2026", auction_date=date(2026, 12, 1))
    row(probate, "UNDATED", "pk-5", None)
    # Auction-date stand-in for a missing notice date: undated for the date sort.
    row(trustee, "STANDIN", "pk-6", "11/20/2026", auction_date=date(2026, 11, 20),
        enrichment_data={"nts_source": {"auction_date": "2026-11-20"}})

    foreign_batch = await _batch(db, business_user.id, "Foreign")
    foreign_job = await _child(db, business_user.id, foreign_batch.id, "probate")
    row(foreign_job, "GREER EMI", "pk-1", "09/15/2026", user_id=business_user.id,
        parcel_id="0419123456")
    run = BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id, status="done",
        child_job_ids=[probate.id, prefc.id, tax.id, trustee.id, foreign_job.id],
    )
    db.add(run)
    await db.commit()
    return batch, run


async def _names(client, token, batch, **params):
    resp = await client.get(f"/batches/{batch.id}/leads", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return [lead["party_name"] for lead in body["leads"]], body["total"]


class TestSearch:
    async def test_owner_name_case_insensitive(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="  greer ") == (["GREER EMI"], 1)

    async def test_parcel_ignores_punctuation(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="0419-12-3456") == (["GREER EMI"], 1)
        assert await _names(client, starter_token, batch, q="3456") == (["GREER EMI"], 1)

    async def test_property_and_mailing(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="57th ave") == (["GREER EMI"], 1)
        assert await _names(client, starter_token, batch, q="po box 2210") == (["GREER EMI"], 1)

    async def test_hidden_mailing_neither_finds_nor_counts(
        self, client, starter_token, db, search_batch
    ):
        batch, _ = search_batch
        (await db.get(ScraperBatch, batch.id)).fields = {"mailing_address": False}
        await db.commit()
        assert await _names(client, starter_token, batch, q="po box 2210") == ([], 0)
        # Still found by what the batch does show.
        assert await _names(client, starter_token, batch, q="57th ave") == (["GREER EMI"], 1)

    async def test_wildcards_are_literal(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="%") == (["100% PURE_LLC"], 1)
        assert await _names(client, starter_token, batch, q="_") == (["100% PURE_LLC"], 1)

    async def test_punctuation_only_is_not_match_all(self, client, starter_token, search_batch):
        """'---' normalizes to an empty parcel needle, which strpos matches everywhere
        (Codex P1): it must find nothing, not everything."""
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="---") == ([], 0)

    async def test_blank_query_is_no_search(self, client, starter_token, search_batch):
        batch, _ = search_batch
        names, total = await _names(client, starter_token, batch, q="   ")
        assert total == 6 and len(names) == 6

    async def test_matches_the_representative_row_only(self, client, starter_token, search_batch):
        """Policy: search reads what the table row shows. The borrower's name sits on
        the pre-foreclosure sibling the probate row represents, so it is not a match."""
        batch, _ = search_batch
        assert await _names(client, starter_token, batch, q="borrower") == ([], 0)

    async def test_other_tenant_never_matches(self, client, starter_token, search_batch):
        batch, _ = search_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", params={"q": "greer"}, headers=_auth(starter_token)
        )
        leads = resp.json()["leads"]
        assert len(leads) == 1 and resp.json()["total"] == 1
        assert leads[0]["overlap_count"] == 2  # ours: probate + pre-fc, the foreign row never joins

    async def test_search_combines_with_filters(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(
            client, starter_token, batch, q="greer", record_type="pre_foreclosure"
        ) == (["GREER EMI"], 1)
        assert await _names(
            client, starter_token, batch, q="greer", record_type="tax_delinquent"
        ) == ([], 0)

    async def test_query_too_long_is_rejected(self, client, starter_token, search_batch):
        batch, _ = search_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", params={"q": "x" * 101}, headers=_auth(starter_token)
        )
        assert resp.status_code == 422


UNDATED = {"TAX OWNER", "UNDATED", "STANDIN"}


class TestSort:
    async def test_date_desc_shown_dates_then_undated(self, client, starter_token, search_batch):
        batch, _ = search_batch
        names, _ = await _names(client, starter_token, batch, sort="date_desc")
        assert names[:3] == ["100% PURE_LLC", "GREER EMI", "AUCTION LATE"]
        assert set(names[3:]) == UNDATED

    async def test_date_asc_undated_still_last(self, client, starter_token, search_batch):
        batch, _ = search_batch
        names, _ = await _names(client, starter_token, batch, sort="date_asc")
        assert names[:3] == ["AUCTION LATE", "GREER EMI", "100% PURE_LLC"]
        assert set(names[3:]) == UNDATED

    async def test_auction_sort_uses_the_auction_group(self, client, starter_token, search_batch):
        """GREER's auction date lives on its pre-foreclosure sibling (the group source),
        not its probate representative row."""
        batch, _ = search_batch
        names, _ = await _names(client, starter_token, batch, sort="auction_asc")
        assert names[:3] == ["GREER EMI", "STANDIN", "AUCTION LATE"]
        names, _ = await _names(client, starter_token, batch, sort="auction_desc")
        assert names[:3] == ["AUCTION LATE", "STANDIN", "GREER EMI"]

    async def test_relevance_is_the_default(self, client, starter_token, search_batch):
        batch, _ = search_batch
        assert await _names(client, starter_token, batch) == await _names(
            client, starter_token, batch, sort="relevance"
        )

    async def test_pages_partition_the_sorted_set(self, client, starter_token, search_batch):
        """Total == the rows the pages render, with no row repeated or lost."""
        batch, _ = search_batch
        for params in ({"sort": "date_desc"}, {"sort": "auction_asc"}, {"q": "st", "sort": "date_asc"}):
            full, total = await _names(client, starter_token, batch, page_size=100, **params)
            paged = []
            for page in (1, 2, 3, 4):
                names, page_total = await _names(
                    client, starter_token, batch, page=page, page_size=2, **params
                )
                assert page_total == total
                paged += names
            assert paged == full and len(full) == total

    async def test_run_scoped_endpoint(self, client, starter_token, search_batch):
        batch, run = search_batch
        resp = await client.get(
            f"/batches/{batch.id}/runs/{run.id}/leads",
            params={"q": "greer", "sort": "date_desc"}, headers=_auth(starter_token),
        )
        assert resp.status_code == 200
        assert [lead["party_name"] for lead in resp.json()["leads"]] == ["GREER EMI"]

    async def test_unknown_sort_is_rejected(self, client, starter_token, search_batch):
        batch, _ = search_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", params={"sort": "party_name"}, headers=_auth(starter_token)
        )
        assert resp.status_code == 422

    async def test_csv_binds_keep_the_relevance_order(
        self, client, starter_token, db, starter_user, search_batch
    ):
        """The combined CSV binds view_filter_params() (no search, no sort): its row
        order must be exactly the relevance page's."""
        batch, run = search_batch
        api, _ = await _names(client, starter_token, batch, page_size=100)
        rows = (await db.execute(text(_COMBINED_SQL), {
            "uid": starter_user.id, "job_ids": [str(j) for j in run.child_job_ids],
            "limit": None, "offset": 0, "overlaps_only": False,
            **view_filter_params(), TAX_CAP_BIND: tax_cap_min_year(date.today()),
        })).fetchall()
        assert [r._mapping["party_name"] for r in rows] == api


class TestRunScopedTwin:
    """The run-scoped endpoint shares _leads_page; prove its own params wire through."""

    async def test_hidden_mailing_and_validation(self, client, starter_token, db, search_batch):
        batch, run = search_batch
        url = f"/batches/{batch.id}/runs/{run.id}/leads"
        (await db.get(ScraperBatch, batch.id)).fields = {"mailing_address": False}
        await db.commit()
        body = (await client.get(url, params={"q": "po box 2210"}, headers=_auth(starter_token))).json()
        assert body["leads"] == [] and body["total"] == 0
        for params in ({"q": "x" * 101}, {"sort": "party_name"}):
            resp = await client.get(url, params=params, headers=_auth(starter_token))
            assert resp.status_code == 422
