"""Every per-job delivery surface reports the SAME rule: new, actionable leads.

Regression for the "Test 5" report: the results list showed `Records = 0` for a job
whose detail view listed 4 leads and whose CSV download had 4 rows. All four were
duplicates of an earlier run.

Standing rule (owner, 2026-09-04): **a duplicate is never delivered.** It was already
delivered — and paid for — on an earlier run. So the results list, the CSV download
and both worker exports all exclude `is_duplicate`, which is the same predicate
`workers/tasks.py` bills on. `total`, `new_count` and `jobs.record_count` therefore
agree by construction, and there is no longer a surface that can show a duplicate.

The rows stay in `results` as dedup bookkeeping and are still counted in
`duplicate_count` / `total_scraped`, so the "all N were duplicates" banner can still
explain an empty run. Lists/segments and the batch combined export deliberately KEEP
duplicates — a lead whose only contactable row is a duplicate must not vanish there —
so this rule is per-job delivery only.

`new_count` is the same RULE as `record_count`, not a guaranteed-equal value —
`record_count` is a billing-time snapshot and `new_count` a live count, so they
legitimately differ while a watchdog re-run has zeroed `record_count`, and after a
post-finalization repair or address backfill (Codex). For a normally finalized,
untouched job the two agree, which is what is asserted here.

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

async def test_all_duplicate_job_delivers_nothing_but_still_explains_itself(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The exact Test 5 case. Not one duplicate is delivered, and the run is still
    explainable: duplicate_count / total_scraped survive so the UI can say why the
    page is empty."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": True}] * 4)

    body = await _results(client, job_id, starter_token)

    # Nothing is delivered — every surface agrees on zero.
    assert body["total"] == 0
    assert body["items"] == []
    assert body["new_count"] == 0
    # ...but the scrape is still described, which is what the banner reads.
    assert body["duplicate_count"] == 4
    assert body["total_scraped"] == 4


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
    """3 new + 2 duplicates: only the 3 new leads are delivered."""
    job_id = await _done_job(starter_user, scraper_config, record_count=3)
    await _add_rows(
        job_id, starter_user.id,
        [{"duplicate": False}] * 3 + [{"duplicate": True}] * 2,
    )
    body = await _results(client, job_id, starter_token)
    assert body["total"] == 3            # the 2 duplicates are not listed
    assert len(body["items"]) == 3
    assert all(not it["is_duplicate"] for it in body["items"])
    assert body["new_count"] == 3
    # The scrape stats still describe everything that was scraped.
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


# ─── The CSV download follows the SAME rule ─────────────────────────────────────

async def _download(client: AsyncClient, job_id: str, token: str):
    return await client.get(
        f"/jobs/{job_id}/download", headers={"Authorization": f"Bearer {token}"}
    )


def _data_rows(csv_text: str) -> list[str]:
    lines = [ln for ln in csv_text.replace("\r\n", "\n").split("\n") if ln.strip()]
    return lines[1:]   # drop the header


async def test_download_excludes_duplicates(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The CSV is what was counted and billed — 3 new leads, not 5 rows."""
    job_id = await _done_job(starter_user, scraper_config, record_count=3)
    await _add_rows(
        job_id, starter_user.id,
        [{"duplicate": False}] * 3 + [{"duplicate": True}] * 2,
    )
    resp = await _download(client, job_id, starter_token)
    assert resp.status_code == 200, resp.text
    assert len(_data_rows(resp.text)) == 3


async def test_all_duplicate_job_downloads_a_header_only_csv_not_a_404(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The completion email for an all-duplicate job still links to this endpoint,
    so it must not 404 — that would hand the user a dead download for a job the
    product legitimately reports as "0 records" (Codex). The job HAS rows; they are
    simply not deliverable, which is exactly the header-only case."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": True}] * 4)

    resp = await _download(client, job_id, starter_token)
    assert resp.status_code == 200, resp.text
    assert _data_rows(resp.text) == []
    assert resp.text.strip(), "a header row must still be present"


async def test_a_genuinely_empty_job_still_404s(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The existing contract: no rows at all is still a 404, not an empty file.
    Only a job that persisted rows gets the header-only treatment."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    resp = await _download(client, job_id, starter_token)
    assert resp.status_code == 404


async def test_a_job_whose_only_rows_are_unactionable_still_404s(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """Quarantined rows are not "rows this job produced" for the purposes of the
    header-only branch — the probe deliberately applies the actionable rule."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": False, "address": None}])
    resp = await _download(client, job_id, starter_token)
    assert resp.status_code == 404


# ─── new_count is the same RULE as record_count, not a guaranteed-equal value ────

async def test_new_count_may_exceed_a_watchdog_zeroed_record_count(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """`record_count` is a billing-time SNAPSHOT; `new_count` is a live count. The
    watchdog zeroes `record_count` when it re-queues a stuck job
    (scheduler_helpers/health.py) while the previous run's rows are still persisted,
    so the two legitimately diverge until the retry finalizes.

    Pinned so nobody later "fixes" the divergence by making one derive from the other:
    forcing them equal would either resurrect a stale billed number or silently
    re-bill the retry (Codex review)."""
    job_id = await _done_job(starter_user, scraper_config, record_count=3)
    await _add_rows(job_id, starter_user.id, [{"duplicate": False}] * 3)

    # Simulate the watchdog re-queue: status back to queued, counters reset.
    async with _db_session.AsyncSessionLocal() as s:
        job = await s.get(Job, job_id)
        job.status = "queued"
        job.record_count = 0
        await s.commit()

    body = await _results(client, job_id, starter_token)
    assert body["new_count"] == 3      # the rows really are there
    assert body["total"] == 3
    job_resp = await client.get(
        f"/jobs/{job_id}", headers={"Authorization": f"Bearer {starter_token}"}
    )
    assert job_resp.json()["record_count"] == 0   # ...and the snapshot really is 0


async def test_cancelled_job_still_reports_its_persisted_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """A cancelled job can hold rows it never billed for. The results surface
    describes what is persisted; it does not claim they were charged."""
    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    await _add_rows(job_id, starter_user.id, [{"duplicate": False}] * 2)
    async with _db_session.AsyncSessionLocal() as s:
        job = await s.get(Job, job_id)
        job.status = "cancelled"
        await s.commit()

    body = await _results(client, job_id, starter_token)
    assert body["new_count"] == 2
    assert body["total"] == 2


async def test_all_over_quota_job_downloads_a_header_only_csv_not_a_404(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """Same class as the all-duplicate case (Codex): a job whose rows were all
    excluded by the plan cap still completed, still uploaded a header-only export and
    still emailed a link here. The empty-result probe therefore asks only whether the
    job persisted an ADDRESSABLE row — every other rule (duplicate, over-quota, tax
    cap) is a reason a row is not DELIVERABLE, not evidence the job scraped nothing."""
    from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA

    job_id = await _done_job(starter_user, scraper_config, record_count=0)
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Result(
            id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
            party_name="OVER QUOTA OWNER", property_address="9 CAPPED ST",
            dedup_hash=uuid.uuid4().hex, is_duplicate=False,
            enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA},
        ))
        await s.commit()

    resp = await _download(client, job_id, starter_token)
    assert resp.status_code == 200, resp.text
    assert _data_rows(resp.text) == []      # nothing deliverable...
    assert resp.text.strip()                # ...but a real CSV, not a dead link
