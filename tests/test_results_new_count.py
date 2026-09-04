"""The results page must report the SAME counting rule the jobs list reports.

Regression for the "Test 5" report: the results list showed `Records = 0` for a job
whose detail view listed 4 leads. Neither number was wrong on its own —
`jobs.record_count` is the BILLED count (non-duplicate + actionable, set by
`workers/tasks.py`), while `ResultsPage.total` deliberately INCLUDES duplicate rows
so a user can see what was scraped. The defect was that the two surfaces exposed two
different rules under the same label, and the results page carried no field holding
the list's rule — so the frontend rendered `total` and the two disagreed with nothing
explaining the gap.

`ResultsPage.new_count` closes that: it uses the SAME predicate as `billable_count`
in tasks.py (is_duplicate = false AND actionable). It is the same RULE, not a
guaranteed-equal value — `record_count` is a billing-time snapshot and `new_count` a
live count, so they legitimately differ while a watchdog re-run has zeroed
`record_count`, and after a post-finalization repair or address backfill (Codex).
For a normally finalized, untouched job the two agree, which is what is asserted here.

Real DB + real endpoints (conftest `db`/`client`/token fixtures) — no mocks.
"""
import uuid

from httpx import AsyncClient

import src.db.session as _db_session
from src.db.models import Job, Result, ScraperConfig, User


async def _done_job(user: User, config: ScraperConfig, record_count: int) -> str:
    """A done job whose record_count is what the worker would have billed."""
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(
            id=job_id,
            user_id=user.id,
            scraper_config_id=config.id,
            status="done",
            trigger="manual",
            record_count=record_count,
            billed_count=record_count,
            export_key=f"exports/{job_id}.csv",
        ))
        await s.commit()
    return job_id


async def _add_rows(job_id: str, user_id: str, specs: list[dict]) -> list[str]:
    """Seed result rows. Each spec: {"duplicate": bool, "address": str | None}."""
    ids = []
    async with _db_session.AsyncSessionLocal() as s:
        for i, spec in enumerate(specs):
            rid = str(uuid.uuid4())
            ids.append(rid)
            s.add(Result(
                id=rid,
                job_id=job_id,
                user_id=user_id,
                party_name=f"OWNER {i}",
                # An address-less row is quarantined by lead_actionability and must
                # count toward NEITHER new_count nor record_count.
                property_address=spec.get("address", f"{i} MAIN ST"),
                dedup_hash=uuid.uuid4().hex,
                is_duplicate=spec["duplicate"],
            ))
        await s.commit()
    return ids


async def _results(client: AsyncClient, job_id: str, token: str) -> dict:
    resp = await client.get(
        f"/jobs/{job_id}/results", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── The Test 5 shape: every row a duplicate ────────────────────────────────────

async def test_all_duplicate_job_reports_zero_new_but_lists_the_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The exact Test 5 case. The rows stay visible; the NEW count is 0."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": True}] * 4)

    body = await _results(client, job_id, starter_token)

    # Visible rows are unchanged — duplicates are shown, greyed, for transparency.
    assert body["total"] == 4
    assert len(body["items"]) == 4
    assert body["duplicate_count"] == 4
    assert body["total_scraped"] == 4
    # ...but the NEW-lead count matches the jobs list / the bill, not `total`.
    assert body["new_count"] == 0


async def test_new_count_matches_job_record_count_for_all_duplicate_job(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The invariant that was missing: new_count == the list's Records column."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": True}] * 4)

    body = await _results(client, job_id, starter_token)
    job = await client.get(
        f"/jobs/{job_id}", headers={"Authorization": f"Bearer {starter_token}"}
    )
    assert job.status_code == 200
    assert body["new_count"] == job.json()["record_count"] == 0


# ─── The counting rule across the range the report asked for ────────────────────

async def test_zero_result_scrape_reports_zero(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    body = await _results(client, job_id, starter_token)
    assert body["new_count"] == 0
    assert body["total"] == 0
    assert body["duplicate_count"] == 0


async def test_single_new_lead_reports_one(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = await _done_job(starter_user, scraper_config, record_count=1)
    await _add_rows(job_id, starter_user.id, [{"duplicate": False}])
    body = await _results(client, job_id, starter_token)
    assert body["new_count"] == 1
    assert body["total"] == 1
    assert body["duplicate_count"] == 0


async def test_mixed_job_counts_only_the_new_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """3 new + 2 duplicates: total shows all 5, new_count shows 3."""
    job_id = await _done_job(starter_user, scraper_config, record_count=3)
    await _add_rows(
        job_id, starter_user.id,
        [{"duplicate": False}] * 3 + [{"duplicate": True}] * 2,
    )
    body = await _results(client, job_id, starter_token)
    assert body["total"] == 5
    assert body["new_count"] == 3
    assert body["duplicate_count"] == 2
    assert body["total_scraped"] == 5


async def test_unactionable_rows_are_excluded_from_new_count(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """A row with no property AND no mailing address is not a lead — it is not
    billed (tasks.py filters on the same actionable predicate), so it must not
    inflate new_count either."""
    job_id = await _done_job(starter_user, scraper_config, record_count=1)
    await _add_rows(job_id, starter_user.id, [
        {"duplicate": False},
        {"duplicate": False, "address": None},   # quarantined: not a lead
    ])
    body = await _results(client, job_id, starter_token)
    assert body["new_count"] == 1
    # The quarantined row is not listed either.
    assert body["total"] == 1


async def test_new_count_is_not_narrowed_by_a_view_filter(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """new_count describes the SCRAPE (like total_scraped/duplicate_count), not the
    current view — it has to keep matching jobs.record_count, which no view filter
    can change. Only `total`/`items` follow the filter."""
    job_id = await _done_job(starter_user, scraper_config, record_count=2)
    await _add_rows(job_id, starter_user.id, [{"duplicate": False}] * 2)

    resp = await client.get(
        f"/jobs/{job_id}/results",
        params={"absentee": "true"},   # matches none of the seeded rows
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0          # the view is filtered empty
    assert body["new_count"] == 2      # the scrape still produced 2 new leads
