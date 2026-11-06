"""The run breakdown names a privacy-withheld row once, and never to the customer.

Phase 6 P3a (Codex High): once actionable_sql refuses a withheld row, the partition
needs a bucket for it or the row falls to 'unclassified' and blocks the snapshot.
A withheld row is in privacy_withheld whatever else is true of it (a duplicate, no
address, held), so no customer-visible bucket can describe it; customer_view folds
the bucket into dropped_before_save, so the total still reconciles.

Real database and real endpoints (conftest fixtures). Synthetic rows only.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy import update

from src.api.run_breakdown import (
    SNAPSHOT_COLUMNS,
    RowPartition,
    breakdown_from_job,
    completion_message,
    customer_view,
    read_partition_async,
    snapshot_columns,
)
from src.db.models import Job, ScraperConfig, User
from tests.test_privacy_read_paths import _auth, _job, _row, _withhold

NOW = datetime.now(UTC)


async def _run(db, user: User, config: ScraperConfig) -> tuple[str, list[str]]:
    """A finished run: 2 new leads, 1 no-address row, and 4 withheld rows of every kind.
    records_found 8, so 1 record was dropped before save."""
    job = await _job(db, user, config)
    uid = user.id
    await _row(db, job, uid, tag="NEW1", prop="1 FIR LN")
    await _row(db, job, uid, tag="NEW2", prop="2 FIR LN")
    await _row(db, job, uid, tag="BARE")
    withheld = [
        await _row(db, job, uid, tag="XNEW", prop="11 FIR LN"),
        await _row(db, job, uid, tag="XDLV", prop="12 OAK AVE", duplicate=True,
                   reason="prior_run"),
        await _row(db, job, uid, tag="XBARE"),
        await _row(db, job, uid, tag="XHELD", prop="13 ELM ST", held=True),
    ]
    await _withhold(db, *withheld)
    await db.execute(update(Job).where(Job.id == job).values(records_found=8, retry_count=0))
    await db.commit()
    return job, withheld


async def test_every_withheld_row_is_in_its_own_bucket_and_none_is_unclassified(
    starter_user: User, scraper_config: ScraperConfig, db,
):
    job, _ = await _run(db, starter_user, scraper_config)
    part = await read_partition_async(db, job, starter_user.id)
    assert part == RowPartition(no_address=1, new=2, privacy_withheld=4)
    assert part.unclassified == 0 and part.persisted == 7


def test_the_worker_freezes_the_bucket_and_the_snapshot_reconciles():
    started = datetime(2026, 11, 6, tzinfo=UTC)
    cols, reason = snapshot_columns(
        billed_now=True, attempt_token=SimpleNamespace(started_at=started, retry_count=0),
        row_started_at=started, records_found=8, retry_count=0,
        partition=RowPartition(no_address=1, new=2, privacy_withheld=4),
    )
    assert reason is None
    assert cols["breakdown_privacy_withheld"] == 4
    assert cols["breakdown_dropped_before_save"] == 1
    assert sum(cols.values()) == 8


def _job_row(**cols) -> SimpleNamespace:
    base = dict.fromkeys(SNAPSHOT_COLUMNS.values())
    return SimpleNamespace(**(base | cols), records_found=8, record_count=2, billed_count=2)


def test_a_snapshot_frozen_before_126_reads_the_bucket_as_zero():
    old = _job_row(breakdown_dropped_before_save=5, breakdown_no_address=1,
                   breakdown_same_run_merged=0, breakdown_already_delivered=0,
                   breakdown_over_quota=0, breakdown_new=2)  # pre-121 AND pre-126
    stored, problem = breakdown_from_job(old)
    assert problem is None
    assert stored["privacy_withheld"] == 0 and stored["not_qualified"] == 0


def test_the_customer_sees_withheld_rows_as_filtered_out_and_never_by_name():
    stored = {"dropped_before_save": 1, "no_address": 1, "same_run_merged": 0,
              "already_delivered": 0, "not_qualified": 0, "over_quota": 0, "new": 2,
              "privacy_withheld": 4}
    shown = customer_view(stored)
    assert "privacy_withheld" not in shown
    assert shown["dropped_before_save"] == 5 and sum(shown.values()) == 8
    # Unknown stays unknown; it is never invented from the withheld count.
    assert customer_view(stored | {"dropped_before_save": None})["dropped_before_save"] is None
    line = completion_message(2, stored)
    assert "5 not saved" in line and "privacy" not in line.lower()


async def test_the_results_page_breakdown_reconciles_without_naming_the_bucket(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job, _ = await _run(db, starter_user, scraper_config)
    resp = await client.get(f"/jobs/{job}/results", headers=_auth(starter_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["breakdown_basis"] == "live"
    b = body["breakdown"]
    assert "privacy_withheld" not in b
    assert b == {"dropped_before_save": 5, "no_address": 1, "same_run_merged": 0,
                 "already_delivered": 0, "not_qualified": 0, "over_quota": 0, "new": 2}
    assert sum(b.values()) == 8
    assert "privacy" not in resp.text.lower()


async def test_a_frozen_snapshot_with_withheld_rows_is_folded_on_every_endpoint(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    """The worker froze privacy_withheld = 4. GET /jobs/{id} and its results page show
    the snapshot folded: 1 dropped + 4 withheld = 5 filtered out, no internal bucket."""
    job = await _job(db, starter_user, scraper_config)
    await db.execute(update(Job).where(Job.id == job).values(
        records_found=8, record_count=2, billed_count=2, retry_count=0,
        breakdown_dropped_before_save=1, breakdown_no_address=1,
        breakdown_same_run_merged=0, breakdown_already_delivered=0,
        breakdown_not_qualified=0, breakdown_over_quota=0, breakdown_new=2,
        breakdown_privacy_withheld=4,
    ))
    await db.commit()
    folded = {"dropped_before_save": 5, "no_address": 1, "same_run_merged": 0,
              "already_delivered": 0, "not_qualified": 0, "over_quota": 0, "new": 2}
    for path in (f"/jobs/{job}", f"/jobs/{job}/results"):
        resp = await client.get(path, headers=_auth(starter_token))
        assert resp.status_code == 200, (path, resp.text)
        body = resp.json()
        assert body["breakdown_basis"] == "snapshot", path
        assert body["breakdown"] == folded, path
        assert "privacy" not in resp.text.lower(), path
