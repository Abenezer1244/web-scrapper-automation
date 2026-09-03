"""Hard 18-month tax-delinquent cap on the per-job results surfaces.

The cap (`src/api/tax_filters.tax_cap_condition`) is a STANDING product rule —
independent of the optional amount/months view filters — applied to the rows the
user SEES (results list + its `total` count) and EXPORTS (CSV download). Tax rows
whose oldest unpaid bill year falls outside the 18-month window are hidden/dropped
everywhere; non-tax rows (NULL `delinquent_bill_year`) always pass.

Real DB + real endpoints (conftest `db`/`client`/token fixtures) — no mocks.
"""
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient

import src.db.session as _db_session
from src.api.tax_filters import tax_cap_min_year
from src.db.models import Job, Result, ScraperConfig, User

# Frozen "today" for the assertions below. tax_cap_min_year() is the same math
# the endpoints use, so the boundary year is whatever the cap resolves to right
# now — we pick rows clearly inside (cap year) and clearly outside (year 2010).
_TODAY = datetime.now(UTC).date()
_IN_WINDOW_YEAR = tax_cap_min_year(_TODAY)      # oldest year still visible
_OUT_OF_WINDOW_YEAR = 2010                       # decades stale -> always dropped


async def _make_done_job(user: User, config: ScraperConfig) -> str:
    """A done job with an export_key (download endpoint requires one)."""
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(
            id=job_id,
            user_id=user.id,
            scraper_config_id=config.id,
            status="done",
            trigger="manual",
            export_key=f"exports/{job_id}.csv",
        ))
        await s.commit()
    return job_id


async def _seed_results(job_id: str, user_id: str) -> dict[str, str]:
    """Three rows: in-window tax, out-of-window tax, and a non-tax (NULL) row."""
    ids = {
        "in_window": str(uuid.uuid4()),
        "out_of_window": str(uuid.uuid4()),
        "non_tax": str(uuid.uuid4()),
    }
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Result(
            id=ids["in_window"],
            job_id=job_id,
            user_id=user_id,
            party_name="IN WINDOW OWNER",
            # Leads carry an address (lead_actionability, 2026-09-02): address-less
            # rows are quarantined from list/export, so the fixtures have one.
            property_address="1 IN WINDOW ST",
            delinquent_amount=5000,
            delinquent_bill_year=_IN_WINDOW_YEAR,
        ))
        s.add(Result(
            id=ids["out_of_window"],
            job_id=job_id,
            user_id=user_id,
            party_name="STALE DEBT OWNER",
            property_address="2 STALE DEBT ST",
            delinquent_amount=5000,
            delinquent_bill_year=_OUT_OF_WINDOW_YEAR,
        ))
        s.add(Result(
            id=ids["non_tax"],
            job_id=job_id,
            user_id=user_id,
            party_name="PROBATE OWNER",
            property_address="3 PROBATE LN",
            delinquent_amount=None,
            delinquent_bill_year=None,
        ))
        await s.commit()
    return ids


# ─── List + count surface (/jobs/{id}/results) ──────────────────────────────────

async def test_results_list_and_count_exclude_out_of_window(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = await _make_done_job(starter_user, scraper_config)
    ids = await _seed_results(job_id, starter_user.id)

    resp = await client.get(
        f"/jobs/{job_id}/results",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()

    returned_ids = {row["id"] for row in body["items"]}
    # In-window tax row + non-tax row survive; stale tax row is capped out.
    assert ids["in_window"] in returned_ids
    assert ids["non_tax"] in returned_ids
    assert ids["out_of_window"] not in returned_ids

    # `total` is the filtered count off the SAME query — it must also exclude
    # the capped row (2 of the 3 seeded rows).
    assert body["total"] == 2
    # The unfiltered scrape stat still describes the whole scrape (all 3 rows).
    assert body["total_scraped"] == 3


# ─── Download surface (/jobs/{id}/download) ─────────────────────────────────────

async def test_download_excludes_out_of_window(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = await _make_done_job(starter_user, scraper_config)
    await _seed_results(job_id, starter_user.id)

    # Mint a short-lived download URL (carries the token), then fetch the CSV.
    url_resp = await client.get(
        f"/jobs/{job_id}/export-url",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert url_resp.status_code == 200
    download_path = url_resp.json()["url"]

    dl_resp = await client.get(download_path)
    assert dl_resp.status_code == 200
    csv_text = dl_resp.text

    # In-window tax + non-tax owners are exported; the stale-debt owner is not.
    assert "IN WINDOW OWNER" in csv_text
    assert "PROBATE OWNER" in csv_text
    assert "STALE DEBT OWNER" not in csv_text


# ─── Cap is independent of the optional months filter ───────────────────────────

async def test_cap_applies_even_with_no_filter_params(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """No amount/months query params -> cap STILL drops the stale tax row."""
    job_id = await _make_done_job(starter_user, scraper_config)
    ids = await _seed_results(job_id, starter_user.id)

    resp = await client.get(
        f"/jobs/{job_id}/results",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200
    returned_ids = {row["id"] for row in resp.json()["items"]}
    assert ids["out_of_window"] not in returned_ids
