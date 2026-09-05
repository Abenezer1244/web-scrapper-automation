"""Piece 2 Phase 2A.4 — batch read + download endpoints.

Pure tests lock the response-schema shape + router wiring. DB-backed tests
(client + real Postgres) verify the list/detail/download contract and tenant
isolation — ScraperBatch / BatchRun are system-written + NOT RLS-granted, so the
explicit user_id filter is the only tenant boundary and MUST be proven. These
run in CI like the rest of the suite.
"""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    BatchChildSummary,
    BatchDetailResponse,
    BatchSummaryResponse,
)
from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig, User


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Pure: schema shape + router wiring ──────────────────────────────────────

class TestSchemas:
    def test_summary_defaults(self):
        s = BatchSummaryResponse(id="b", state="WA", created_at=datetime.now(UTC))
        assert s.run_status == "pending"
        assert s.child_count == 0
        assert s.combined_export_ready is False
        assert s.completed_at is None

    def test_detail_inherits_summary_and_adds_children(self):
        d = BatchDetailResponse(id="b", state="WA", created_at=datetime.now(UTC))
        assert d.children == []
        assert d.failed_children is None
        assert d.run_status == "pending"  # inherited from BatchSummaryResponse

    def test_child_summary_defaults(self):
        c = BatchChildSummary(config_id="c", county="king", record_type="probate")
        assert c.job_id is None
        assert c.status == "pending"
        assert c.record_count == 0

def test_read_routes_registered():
    from src.api import batches_router

    paths = {getattr(rt, "path", None) for rt in batches_router.routes}
    assert "/batches" in paths
    assert "/batches/{batch_id}" in paths
    assert "/batches/{batch_id}/download" in paths


# ─── DB-backed: a finished batch owned by starter_user ───────────────────────

@pytest_asyncio.fixture
async def starter_batch(db: AsyncSession, starter_user: User) -> SimpleNamespace:
    """A done batch: 1 child (king/probate, job done, 42 records) + a done run
    with a combined export key. Returns plain-string ids (no expired-attr access)."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        name="My Batch",
        state="WA",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={"emails": []},
        status="active",
    )
    db.add(batch)
    # Flush parents before the FK-dependent rows: batch_runs/scraper_configs carry
    # a composite FK (batch_id, user_id) -> scraper_batches, so the batch row must
    # exist in the txn first (in prod these are separate transactions).
    await db.flush()
    cfg = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        batch_id=batch.id,
        name="King probate (batch)",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={},
    )
    db.add(cfg)
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=cfg.id,
        status="done",
        trigger="batch",
        record_count=42,
    )
    db.add(job)
    await db.flush()  # job must exist before the run references its id
    run = BatchRun(
        id=str(uuid.uuid4()),
        batch_id=batch.id,
        user_id=starter_user.id,
        status="done",
        child_job_ids=[job.id],
        combined_export_key=f"exports/{starter_user.id}/batch/{uuid.uuid4()}/combined.csv",
    )
    db.add(run)
    await db.commit()
    return SimpleNamespace(batch_id=batch.id, config_id=cfg.id, job_id=job.id)


# ─── GET /batches ─────────────────────────────────────────────────────────────

async def test_list_empty(client: AsyncClient, starter_user: User, starter_token: str):
    resp = await client.get("/batches", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_returns_own_batch(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get("/batches", headers=_auth(starter_token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == starter_batch.batch_id
    assert item["run_status"] == "done"
    assert item["child_count"] == 1
    assert item["record_types"] == ["probate"]  # collapsed-row label source
    assert item["combined_export_ready"] is True


async def test_list_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    # business_user must not see starter_user's batch.
    resp = await client.get("/batches", headers=_auth(business_token))
    assert resp.status_code == 200
    assert resp.json() == []


# ─── GET /batches/{id} ────────────────────────────────────────────────────────

async def test_detail_per_child_summary(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}", headers=_auth(starter_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_status"] == "done"
    assert body["combined_export_ready"] is True
    assert len(body["children"]) == 1
    child = body["children"][0]
    assert child["county"] == "king"
    assert child["record_type"] == "probate"
    assert child["status"] == "done"
    assert child["record_count"] == 42
    assert child["job_id"] == starter_batch.job_id


async def test_detail_not_found(client: AsyncClient, starter_token: str):
    resp = await client.get(f"/batches/{uuid.uuid4()}", headers=_auth(starter_token))
    assert resp.status_code == 404


async def test_detail_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}", headers=_auth(business_token)
    )
    assert resp.status_code == 404


# ─── GET /batches/{id}/download ───────────────────────────────────────────────

async def test_download_ready_branch_not_404(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    # combined_export_key IS set -> we rebuild the CSV from the DB, never 'not
    # ready'. 200 with the (possibly header-only) CSV in CI, or 503 if the sync
    # session is unavailable — both prove the key was found, neither is 404.
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}/download", headers=_auth(starter_token)
    )
    assert resp.status_code in (200, 503)


async def test_download_not_ready_404(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    # A batch whose run has NO combined_export_key yet -> 404 not ready.
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        name="Running batch",
        state="WA",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
    )
    db.add(batch)
    await db.flush()  # batch must exist before the run's composite FK references it
    run = BatchRun(
        id=str(uuid.uuid4()),
        batch_id=batch.id,
        user_id=starter_user.id,
        status="running",
        child_job_ids=[],
        combined_export_key=None,
    )
    db.add(run)
    await db.commit()
    resp = await client.get(f"/batches/{batch.id}/download", headers=_auth(starter_token))
    assert resp.status_code == 404


async def test_download_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}/download", headers=_auth(business_token)
    )
    assert resp.status_code == 404


# ─── Status-based readiness (Bug B) ────────────────────────────────────────────
#
# Readiness/downloadability is now `run.status in _DOWNLOADABLE_STATUSES`, not
# `bool(run.combined_export_key)`. A 'done' zero-row overlaps_only run never
# gets a combined_export_key written (nothing to upload) but must still read
# ready and download a headers-only CSV — the old key-presence gate 404'd
# exactly that honest-empty case.

class TestStatusBasedReadiness:
    async def test_done_run_without_key_is_ready_and_downloadable(
        self, client: AsyncClient, starter_token: str, db: AsyncSession, starter_user: User
    ):
        """Bug B end-to-end: a 'done' run with NO combined_export_key (zero-row
        overlaps_only) must read ready and stream a headers-only CSV."""
        batch = ScraperBatch(
            id=str(uuid.uuid4()),
            user_id=starter_user.id,
            name="Empty",
            state="WA",
            fields=[],
            enrichment=[],
            schedule={},
            deliver={},
            status="active",
            delivery_mode="overlaps_only",
        )
        db.add(batch)
        await db.flush()
        run = BatchRun(
            id=str(uuid.uuid4()),
            batch_id=batch.id,
            user_id=starter_user.id,
            status="done",
            child_job_ids=[],
            combined_export_key=None,
            delivery_counts={
                "leads_total": 0,
                "overlaps_delivered": 0,
                "singletons_suppressed": 0,
                "unmatchable_no_parcel": 0,
            },
        )
        db.add(run)
        await db.commit()

        detail = await client.get(f"/batches/{batch.id}", headers=_auth(starter_token))
        assert detail.status_code == 200
        body = detail.json()
        assert body["combined_export_ready"] is True
        assert body["delivery_mode"] == "overlaps_only"
        assert body["delivery_counts"]["leads_total"] == 0

        dl = await client.get(f"/batches/{batch.id}/download", headers=_auth(starter_token))
        assert dl.status_code == 200
        lines = [ln for ln in dl.text.splitlines() if ln.strip()]
        assert len(lines) == 1  # header only

    async def test_running_run_not_ready(
        self, client: AsyncClient, starter_token: str, db: AsyncSession, starter_user: User
    ):
        batch = ScraperBatch(
            id=str(uuid.uuid4()),
            user_id=starter_user.id,
            name="Running",
            state="WA",
            fields=[],
            enrichment=[],
            schedule={},
            deliver={},
            status="active",
        )
        db.add(batch)
        await db.flush()
        db.add(
            BatchRun(
                id=str(uuid.uuid4()),
                batch_id=batch.id,
                user_id=starter_user.id,
                status="running",
                child_job_ids=[],
            )
        )
        await db.commit()

        dl = await client.get(f"/batches/{batch.id}/download", headers=_auth(starter_token))
        assert dl.status_code == 404


# ─── combined_record_count (Results-page batch collapse) ─────────────────────
# The Results page shows a batch as ONE row with the deduped combined count
# (never the sum of child record_counts). combined_record_count is mode-aware,
# from the run's finalized delivery_counts snapshot, and NULL until finalize.

class TestCombinedRecordCount:
    async def _mk(
        self, db: AsyncSession, user: User, *, mode: str, counts: dict | None
    ) -> str:
        batch = ScraperBatch(
            id=str(uuid.uuid4()),
            user_id=user.id,
            name="Counted",
            state="WA",
            fields=[],
            enrichment=[],
            schedule={},
            deliver={},
            status="active",
            delivery_mode=mode,
        )
        db.add(batch)
        await db.flush()
        db.add(
            BatchRun(
                id=str(uuid.uuid4()),
                batch_id=batch.id,
                user_id=user.id,
                status="done",
                child_job_ids=[],
                delivery_counts=counts,
            )
        )
        await db.commit()
        return batch.id

    async def test_overlaps_only_uses_overlaps_delivered(
        self, client: AsyncClient, starter_token: str, db: AsyncSession, starter_user: User
    ):
        bid = await self._mk(
            db, starter_user, mode="overlaps_only",
            counts={"leads_total": 30, "overlaps_delivered": 7,
                    "singletons_suppressed": 21, "unmatchable_no_parcel": 2},
        )
        body = (await client.get(f"/batches/{bid}", headers=_auth(starter_token))).json()
        assert body["combined_record_count"] == 7  # overlaps_delivered, not leads_total

    async def test_everything_uses_leads_total(
        self, client: AsyncClient, starter_token: str, db: AsyncSession, starter_user: User
    ):
        bid = await self._mk(
            db, starter_user, mode="everything",
            counts={"leads_total": 30, "overlaps_delivered": 7,
                    "singletons_suppressed": 21, "unmatchable_no_parcel": 2},
        )
        body = (await client.get(f"/batches/{bid}", headers=_auth(starter_token))).json()
        assert body["combined_record_count"] == 30  # leads_total

    async def test_null_until_finalized(
        self, client: AsyncClient, starter_token: str, db: AsyncSession, starter_user: User
    ):
        bid = await self._mk(db, starter_user, mode="overlaps_only", counts=None)
        body = (await client.get(f"/batches/{bid}", headers=_auth(starter_token))).json()
        assert body["combined_record_count"] is None


# ─── a PARTIAL run: a failed child must not report in-flight scrape progress ──

@pytest_asyncio.fixture
async def partial_batch(db: AsyncSession, starter_user: User) -> SimpleNamespace:
    """Test 11's shape: one done child (0 leads) + one FAILED child whose
    jobs.record_count still holds the mid-scrape counter (210) even though it
    persisted, exported and billed nothing. The run is 'partial'."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Test 11", state="WA",
        fields=["party_name"], enrichment=[], schedule={}, deliver={"emails": []},
        status="active",
    )
    db.add(batch)
    await db.flush()
    jobs = {}
    for record_type, status, record_count in (
        ("probate", "done", 0),
        ("pre_foreclosure", "failed", 210),
    ):
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=starter_user.id, batch_id=batch.id,
            name=f"Test 11 - {record_type}", county="pierce", state="WA",
            record_type=record_type, fields=["party_name"], enrichment=[],
            schedule={}, deliver={},
        )
        db.add(cfg)
        job = Job(
            id=str(uuid.uuid4()), user_id=starter_user.id, scraper_config_id=cfg.id,
            status=status, trigger="batch", record_count=record_count,
        )
        db.add(job)
        jobs[record_type] = job
    await db.flush()
    db.add(BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
        status="partial", child_job_ids=[j.id for j in jobs.values()],
    ))
    await db.commit()
    return SimpleNamespace(batch_id=batch.id)


async def test_failed_child_reports_zero_not_its_in_flight_scrape_counter(
    client: AsyncClient, starter_token: str, partial_batch: SimpleNamespace
):
    """jobs.record_count is written mid-scrape by the progress callback, so a
    FAILED child keeps the raw counter from the page it died on. Surfacing it
    made Test 11's UI print "210 leads" for a job that delivered zero, and
    summed it into the batch total. A non-done child reports 0."""
    resp = await client.get(
        f"/batches/{partial_batch.batch_id}", headers=_auth(starter_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_status"] == "partial"
    by_type = {c["record_type"]: c for c in body["children"]}
    assert by_type["pre_foreclosure"]["status"] == "failed"
    assert by_type["pre_foreclosure"]["record_count"] == 0
    assert by_type["probate"]["status"] == "done"
    assert by_type["probate"]["record_count"] == 0
    # what the batch header sums
    assert sum(c["record_count"] for c in body["children"]) == 0


async def test_failed_child_that_did_persist_rows_still_reports_them(
    db: AsyncSession, client: AsyncClient, starter_user: User, starter_token: str,
    partial_batch: SimpleNamespace,
):
    """Do NOT key the count on status (Codex P1). finalize_batch_run builds the
    combined CSV from every child_job_id with no status filter, and force-finalize
    CANCELS still-active children after they may have persisted rows — so a
    failed/cancelled child's rows can be in the delivered CSV. Those must stay
    visible; only the count that outran persistence is capped."""
    job_id = (
        await db.execute(
            select(Job.id).join(
                ScraperConfig, ScraperConfig.id == Job.scraper_config_id
            ).where(
                ScraperConfig.batch_id == partial_batch.batch_id,
                Job.status == "failed",
            )
        )
    ).scalar_one()
    for _ in range(3):
        db.add(Result(
            id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
            party_name="SMITH JANE", property_address="1 MAIN ST",
        ))
    await db.commit()

    resp = await client.get(
        f"/batches/{partial_batch.batch_id}", headers=_auth(starter_token)
    )
    assert resp.status_code == 200
    child = {c["record_type"]: c for c in resp.json()["children"]}["pre_foreclosure"]
    assert child["status"] == "failed"
    # 3 rows exist, so they are reported — not zeroed away, and not the mid-scrape
    # 210 either.
    assert child["record_count"] == 3


async def test_unactionable_rows_are_not_counted_as_leads(
    db: AsyncSession, client: AsyncClient, starter_user: User, starter_token: str,
    partial_batch: SimpleNamespace,
):
    """The count must use the same per-row rules the combined export applies:
    a row with neither a property nor a mailing address is not a lead, so it must
    not be reported as one (Codex round 3)."""
    job_id = (
        await db.execute(
            select(Job.id).join(
                ScraperConfig, ScraperConfig.id == Job.scraper_config_id
            ).where(
                ScraperConfig.batch_id == partial_batch.batch_id,
                Job.status == "failed",
            )
        )
    ).scalar_one()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
        party_name="HAS AN ADDRESS", property_address="5 PINE ST",
    ))
    db.add(Result(  # no property AND no mailing address -> not a lead
        id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
        party_name="NO ADDRESS AT ALL",
    ))
    await db.commit()

    resp = await client.get(
        f"/batches/{partial_batch.batch_id}", headers=_auth(starter_token)
    )
    child = {c["record_type"]: c for c in resp.json()["children"]}["pre_foreclosure"]
    assert child["record_count"] == 1


async def test_failed_child_rows_survive_a_retry_that_reset_record_count(
    db: AsyncSession, client: AsyncClient, starter_user: User, starter_token: str,
    partial_batch: SimpleNamespace,
):
    """_retry_scrape_job sets record_count=0 on a re-queue, so the counter also runs
    BEHIND rows that were already saved. Counting the rows keeps them visible where
    min(record_count, rows) would have hidden them (Codex round 2)."""
    job = (
        await db.execute(
            select(Job).join(
                ScraperConfig, ScraperConfig.id == Job.scraper_config_id
            ).where(
                ScraperConfig.batch_id == partial_batch.batch_id,
                Job.status == "failed",
            )
        )
    ).scalar_one()
    job.record_count = 0  # what a watchdog re-queue leaves behind
    for _ in range(2):
        db.add(Result(
            id=str(uuid.uuid4()), job_id=job.id, user_id=starter_user.id,
            party_name="DOE JOHN", property_address="2 OAK AVE",
        ))
    await db.commit()

    resp = await client.get(
        f"/batches/{partial_batch.batch_id}", headers=_auth(starter_token)
    )
    child = {c["record_type"]: c for c in resp.json()["children"]}["pre_foreclosure"]
    assert child["record_count"] == 2


async def test_duplicate_rows_are_not_counted_as_leads(
    db: AsyncSession, client: AsyncClient, starter_user: User, starter_token: str,
    partial_batch: SimpleNamespace,
):
    """record_count counts NEW non-duplicate rows, so counting rows must too."""
    job_id = (
        await db.execute(
            select(Job.id).join(
                ScraperConfig, ScraperConfig.id == Job.scraper_config_id
            ).where(
                ScraperConfig.batch_id == partial_batch.batch_id,
                Job.status == "failed",
            )
        )
    ).scalar_one()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
        party_name="NEW LEAD", property_address="3 ELM ST",
    ))
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
        party_name="SEEN BEFORE", property_address="4 ELM ST", is_duplicate=True,
    ))
    await db.commit()

    resp = await client.get(
        f"/batches/{partial_batch.batch_id}", headers=_auth(starter_token)
    )
    child = {c["record_type"]: c for c in resp.json()["children"]}["pre_foreclosure"]
    assert child["record_count"] == 1
